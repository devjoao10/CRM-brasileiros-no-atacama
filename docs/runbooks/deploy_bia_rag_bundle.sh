#!/usr/bin/env bash
# =============================================================================
# BIA-RAG-EMBEDDINGS-01 — bundle ÚNICO de redeploy do serviço RAG
# (substitui o bundle de BIA-RAG-CONTEXT-01, que falhou no ingest com
#  text-embedding-004 → 404). Migra para google-genai + gemini-embedding-001.
#
# Executar UMA VEZ, como root, na VPS, APÓS mergear o PR do fix:
#   tmux new -s ragredeploy
#   bash deploy_bia_rag_bundle.sh 2>&1 | tee /root/bia_rag_redeploy_$(date -u +%Y%m%dT%H%M%SZ).log
#
# Sobe SOMENTE bia_rag. NÃO toca em crm / postgres / n8n / conversas.
# Pré-requisitos no /opt/crm/.env: GEMINI_API_KEY e BIA_RAG_INTERNAL_SECRET.
#
# Saída: 0=SUCCESS · 1=HARD STOP (nada subiu) · 2=ROLLBACK (serviço removido,
#         índice anterior restaurado quando existia)
# =============================================================================
set -Eeuo pipefail
umask 077

REPO_DIR="/opt/crm"
SVC="bia_rag"
UTC="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="/root/crm_operational_backups"
VOL="bia_rag_data"
STAGE="PREFLIGHT"
INDEX_BACKUP=""

log(){ printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
hard_stop(){ printf '\nHARD STOP: %s\nEstágio: %s. O serviço RAG não foi (re)iniciado; nenhum outro serviço foi tocado.\n' "$*" "$STAGE"; exit 1; }
started_of(){ docker inspect -f '{{.State.StartedAt}}' "$1" 2>/dev/null || echo "ausente"; }

log "=== BIA-RAG-EMBEDDINGS-01 redeploy | UTC=${UTC} ==="
[ "$(id -u)" = "0" ] || hard_stop "execute como root"
cd "$REPO_DIR" 2>/dev/null || hard_stop "diretório ${REPO_DIR} inexistente"
docker compose version >/dev/null 2>&1 || hard_stop "docker compose v2 indisponível"

# --- git: sincronizar /opt/crm com a main mergeada (ff-only, sem reset/clean) ---
[ "$(git rev-parse --abbrev-ref HEAD)" = "main" ] || hard_stop "branch não é main"
[ -z "$(git status --porcelain)" ] || hard_stop "working tree suja em ${REPO_DIR} — resolva antes"
git fetch origin --quiet || hard_stop "git fetch falhou"
git merge --ff-only origin/main || hard_stop "ff-only para origin/main falhou (histórico divergente)"
NEW_SHA="$(git rev-parse HEAD)"
log "git sincronizado: main @ ${NEW_SHA}"

# --- marcadores do fix presentes (garante que estamos com o código migrado) ---
grep -q "gemini-embedding-001" rag/app/config.py || hard_stop "config sem gemini-embedding-001 (fix ausente)"
grep -q "google-genai" rag/requirements.txt || hard_stop "requirements sem google-genai (fix ausente)"
grep -q "google-generativeai" rag/requirements.txt && hard_stop "requirements ainda tem google-generativeai (fix incompleto)" || true

docker compose config --quiet || hard_stop ".env incompleto ou compose inválido"
grep -qE '^GEMINI_API_KEY=.+$' .env || hard_stop "GEMINI_API_KEY ausente/vazia no .env"
grep -qE '^BIA_RAG_INTERNAL_SECRET=.+$' .env || hard_stop "BIA_RAG_INTERNAL_SECRET ausente/vazia no .env"

# --- vizinhos ativos + StartedAt (não podem reiniciar) ---
for c in crm_app crm_postgres n8n_server conversas_app; do
    st="$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || echo ausente)"
    [ "$st" = "running" ] || hard_stop "vizinho ${c} não está running (${st})"
done
PRE_CRM="$(started_of crm_app)"; PRE_PG="$(started_of crm_postgres)"
PRE_N8N="$(started_of n8n_server)"; PRE_CONV="$(started_of conversas_app)"
log "vizinhos running; StartedAt registrados"

# --- detectar volume/índice da tentativa anterior + backup (mesmo vazio/parcial) ---
STAGE="BACKUP"
if docker volume inspect "$VOL" >/dev/null 2>&1; then
    mkdir -p "$BACKUP_DIR"
    INDEX_BACKUP="${BACKUP_DIR}/bia_rag_index_pre_${UTC}.sqlite3"
    docker run --rm -v "${VOL}":/data -v "${BACKUP_DIR}":/bkp alpine \
        sh -c "cp -f /data/bna_bia_context.sqlite3 /bkp/$(basename "$INDEX_BACKUP") 2>/dev/null || true" || true
    if [ -s "$INDEX_BACKUP" ]; then log "backup do índice anterior: $(basename "$INDEX_BACKUP")"; else INDEX_BACKUP=""; log "volume existe mas sem índice legível (1ª tentativa vazia/parcial) — ok"; fi
else
    log "sem volume anterior — primeiro índice"
fi

restore_index(){
    [ -n "$INDEX_BACKUP" ] && [ -s "$INDEX_BACKUP" ] || return 0
    docker run --rm -v "${VOL}":/data -v "${BACKUP_DIR}":/bkp alpine \
        sh -c "cp -f /bkp/$(basename "$INDEX_BACKUP") /data/bna_bia_context.sqlite3" 2>/dev/null || true
    log "índice anterior restaurado do backup"
}
rollback(){ STAGE="ROLLBACK"; docker compose logs --tail 40 "$SVC" 2>/dev/null || true; docker compose rm -sf "$SVC" >/dev/null 2>&1 || true; restore_index; printf '\nROLLBACK: %s. Serviço removido; índice anterior restaurado quando existia. Vizinhos intocados.\n' "$1"; exit 2; }

# --- build + up SOMENTE do bia_rag ---
STAGE="BUILD"
log "--- build ${SVC} ---"; docker compose build "$SVC" || hard_stop "build do ${SVC} falhou"
STAGE="UP"
log "--- up ${SVC} (--no-deps) ---"; docker compose up -d --no-deps "$SVC" || rollback "up do ${SVC} falhou"

STAGE="HEALTH"
ok=0; for i in $(seq 1 40); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$SVC" 2>/dev/null || echo starting)" = "healthy" ] && { ok=1; break; }; sleep 3; done
[ "$ok" = "1" ] || rollback "${SVC} não ficou healthy"
log "${SVC} healthy"

# --- SMOKE REAL do novo SDK/modelo ANTES da ingestão ---
STAGE="EMB_SMOKE"
log "--- smoke real de embeddings (google-genai + gemini-embedding-001) ---"
docker compose exec -T "$SVC" python -m rag.scripts.embedding_smoke || rollback "smoke real de embeddings falhou (SDK/modelo/credencial)"

# --- ingest FULL atômico ---
STAGE="INGEST"
log "--- ingest-full atômico ---"
docker compose exec -T "$SVC" python -m rag.scripts.rag_cli ingest-full || rollback "ingestão full falhou"

STAGE="STATUS"
docker compose exec -T "$SVC" python -m rag.scripts.rag_cli status || rollback "status falhou"

# --- buscas reais: validado / sem base / dependente de pendência ---
STAGE="RETRIEVAL_SMOKE"
log "--- buscas reais (sem efeito externo) ---"
docker compose exec -T "$SVC" python - <<'PYSMOKE' || rollback "smokes de retrieval falharam"
import sys
from rag.app.retrieve import retrieve
a = retrieve("como a Bia faz o handoff para atendimento humano")   # validado
b = retrieve("voces vendem passagem aerea nonexistenttopicxyz para marte")  # sem base
c = retrieve("qual o percentual do desconto no pix")               # depende de pendência
inv = all("[PENDENTE_VALIDACAO]" not in ch["text"] for r in (a,b,c) for ch in r["context_chunks"])
ok = a["grounded"] and a["sources"] and (b["answerable"] is False) and inv
print(f"validado_grounded={a['grounded']} tem_fontes={bool(a['sources'])} "
      f"no_answer={not b['answerable']} filtro_pendencia_ok={inv}")
sys.exit(0 if ok else 1)
PYSMOKE

# --- restart do bia_rag + persistência do índice ---
STAGE="RESTART_PERSIST"
log "--- restart ${SVC} + verificação de persistência ---"
BEFORE="$(docker compose exec -T "$SVC" python -m rag.scripts.rag_cli health 2>/dev/null | tr -d '\r')"
docker compose restart "$SVC" >/dev/null 2>&1 || rollback "restart do ${SVC} falhou"
okp=0; for i in $(seq 1 40); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$SVC" 2>/dev/null || echo starting)" = "healthy" ] && { okp=1; break; }; sleep 3; done
[ "$okp" = "1" ] || rollback "${SVC} não voltou healthy após restart"
docker compose exec -T "$SVC" python -c "import sys; from rag.app.store import SqliteVectorStore as S; s=S(); n=s.count(); s.close(); print('chunks_apos_restart=%d'%n); sys.exit(0 if n>0 else 1)" || rollback "índice não persistiu após restart"

# --- vizinhos NÃO reiniciaram ---
STAGE="VERIFY"
for pair in "crm_app:$PRE_CRM" "crm_postgres:$PRE_PG" "n8n_server:$PRE_N8N" "conversas_app:$PRE_CONV"; do
    name="${pair%%:*}"; before="${pair#*:}"; now="$(started_of "$name")"
    [ "$now" = "$before" ] || { printf '\nANOMALIA: %s reiniciou (antes=%s agora=%s).\n' "$name" "$before" "$now"; exit 2; }
done
log "vizinhos preservados (nenhum reinício)"

printf '\n===== RESUMO =====\n'
printf 'git: main @ %s\n' "$NEW_SHA"
printf 'servico: %s healthy (embeddings: google-genai / gemini-embedding-001 / 768d)\n' "$SVC"
docker compose exec -T "$SVC" python -m rag.scripts.rag_cli status 2>/dev/null | grep -E 'model|dimensions|chunks|index_version' || true
printf 'indice anterior: %s\n' "${INDEX_BACKUP:-nenhum}"
printf 'vizinhos preservados: crm/postgres/n8n/conversas sem reinício\n'
printf '\nBIA-RAG REDEPLOY: SUCCESS\n'
exit 0
