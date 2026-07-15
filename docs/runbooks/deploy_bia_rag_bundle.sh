#!/usr/bin/env bash
# =============================================================================
# BIA-RAG-CONTEXT-01 — bundle de deploy do serviço RAG (operator-assisted)
#
# Executar UMA VEZ, como root, na VPS. Sobe SOMENTE o serviço bia_rag.
# NÃO toca em crm / postgres / n8n / conversas.
#   tmux new -s ragdeploy
#   bash deploy_bia_rag_bundle.sh 2>&1 | tee /root/bia_rag_deploy_$(date -u +%Y%m%dT%H%M%SZ).log
#
# Pré-requisitos no /opt/crm/.env: GEMINI_API_KEY e BIA_RAG_INTERNAL_SECRET.
# O código (rag/, docker-compose.yml, bna_agent_context/) precisa estar em
# /opt/crm no commit deste pacote (git já sincronizado pelo fluxo normal).
#
# Saída: 0=SUCCESS · 1=HARD STOP (nada subiu) · 2=ROLLBACK (serviço removido)
# =============================================================================
set -Eeuo pipefail
umask 077

REPO_DIR="/opt/crm"
SVC="bia_rag"
UTC="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_DIR="/root/crm_operational_backups"
STAGE="PREFLIGHT"

log(){ printf '[%s] %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }
hard_stop(){ printf '\nHARD STOP: %s\nEstágio: %s. O serviço RAG não foi iniciado; nenhum outro serviço foi tocado.\n' "$*" "$STAGE"; exit 1; }

started_of(){ docker inspect -f '{{.State.StartedAt}}' "$1" 2>/dev/null || echo "ausente"; }

log "=== BIA-RAG-CONTEXT-01 deploy | UTC=${UTC} ==="
[ "$(id -u)" = "0" ] || hard_stop "execute como root"
cd "$REPO_DIR" 2>/dev/null || hard_stop "diretório ${REPO_DIR} inexistente"
docker compose version >/dev/null 2>&1 || hard_stop "docker compose v2 indisponível"
docker compose config --quiet || hard_stop ".env incompleto ou compose inválido"

# preserva evidência do estado dos vizinhos (não podem reiniciar)
PRE_CRM="$(started_of crm_app)"; PRE_PG="$(started_of crm_postgres)"
PRE_N8N="$(started_of n8n_server)"; PRE_CONV="$(started_of conversas_app)"
log "vizinhos: crm=${PRE_CRM} pg=${PRE_PG} n8n=${PRE_N8N} conversas=${PRE_CONV}"

# .env exige GEMINI_API_KEY e BIA_RAG_INTERNAL_SECRET (verifica presença, não valor)
grep -qE '^GEMINI_API_KEY=.+$' .env || hard_stop "GEMINI_API_KEY ausente/vazia no .env"
grep -qE '^BIA_RAG_INTERNAL_SECRET=.+$' .env || hard_stop "BIA_RAG_INTERNAL_SECRET ausente/vazia no .env"
log "preflight ok"

# backup do índice anterior, se existir (volume bia_rag_data)
STAGE="BACKUP"
if docker volume inspect crm_bia_rag_data >/dev/null 2>&1 || docker volume inspect bia_rag_data >/dev/null 2>&1; then
    mkdir -p "$BACKUP_DIR"
    docker run --rm -v bia_rag_data:/data -v "$BACKUP_DIR":/bkp alpine \
        sh -c "cp -f /data/bna_bia_context.sqlite3 /bkp/bia_rag_index_pre_${UTC}.sqlite3 2>/dev/null || true" || true
    log "backup do índice anterior tentado em ${BACKUP_DIR} (best-effort)"
fi

STAGE="BUILD"
log "--- build SOMENTE do ${SVC} ---"
docker compose build "$SVC" || hard_stop "build do ${SVC} falhou"

STAGE="UP"
log "--- up SOMENTE do ${SVC} (--no-deps) ---"
docker compose up -d --no-deps "$SVC" || { STAGE="ROLLBACK"; docker compose rm -sf "$SVC" >/dev/null 2>&1 || true; printf '\nROLLBACK: up do %s falhou; serviço removido. Vizinhos intocados.\n' "$SVC"; exit 2; }

# aguarda health
log "--- aguardando health ---"
ok=0
for i in $(seq 1 40); do
    st="$(docker inspect -f '{{.State.Health.Status}}' "$SVC" 2>/dev/null || echo starting)"
    [ "$st" = "healthy" ] && { ok=1; break; }
    sleep 3
done
[ "$ok" = "1" ] || { STAGE="ROLLBACK"; docker compose logs --tail 40 "$SVC" || true; docker compose rm -sf "$SVC" >/dev/null 2>&1 || true; printf '\nROLLBACK: %s não ficou healthy; serviço removido. Vizinhos intocados.\n' "$SVC"; exit 2; }
log "${SVC} healthy"

STAGE="INGEST"
log "--- ingest-full ---"
docker compose exec -T "$SVC" python -m rag.scripts.rag_cli ingest-full \
    || { STAGE="ROLLBACK"; docker compose rm -sf "$SVC" >/dev/null 2>&1 || true; printf '\nROLLBACK: ingestão falhou; serviço removido.\n'; exit 2; }

STAGE="SMOKE"
log "--- smokes de retrieval (sem efeito externo) ---"
docker compose exec -T "$SVC" python - <<'PYSMOKE' || { STAGE="ROLLBACK"; docker compose rm -sf bia_rag >/dev/null 2>&1 || true; printf '\nROLLBACK: smokes falharam; serviço removido.\n'; exit 2; }
import sys
from rag.app.retrieve import retrieve
# validado: deve fundamentar
a = retrieve("como a Bia faz o handoff para atendimento humano")
# no-answer: deve recusar
b = retrieve("voces vendem passagem aerea nonexistenttopicxyz para marte")
# invariante: nenhum chunk factual com marcador pendente
c = retrieve("desconto pix percentual")
ok = (a["grounded"] and a["sources"]
      and (b["answerable"] is False)
      and all("[PENDENTE_VALIDACAO]" not in ch["text"] for ch in c["context_chunks"]))
print(f"smoke grounded={a['grounded']} no_answer={not b['answerable']} safe_pending={ok}")
sys.exit(0 if ok else 1)
PYSMOKE

# confirma que vizinhos NÃO reiniciaram
STAGE="VERIFY"
for pair in "crm_app:$PRE_CRM" "crm_postgres:$PRE_PG" "n8n_server:$PRE_N8N" "conversas_app:$PRE_CONV"; do
    name="${pair%%:*}"; before="${pair#*:}"; now="$(started_of "$name")"
    [ "$now" = "$before" ] || { printf '\nANOMALIA: %s reiniciou (antes=%s agora=%s). Investigar.\n' "$name" "$before" "$now"; exit 2; }
done
log "vizinhos preservados (nenhum reinício)"

printf '\n===== RESUMO =====\n'
printf 'servico: %s healthy\n' "$SVC"
docker compose exec -T "$SVC" python -m rag.scripts.rag_cli status 2>/dev/null || true
printf 'vizinhos preservados: crm/postgres/n8n/conversas sem reinício\n'
printf '\nBIA-RAG DEPLOY: SUCCESS\n'
exit 0
