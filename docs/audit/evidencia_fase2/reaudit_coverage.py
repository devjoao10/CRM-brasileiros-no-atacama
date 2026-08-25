# -*- coding: utf-8 -*-
"""
Gera docs/audit/REAUDIT_COVERAGE.csv — cobertura da REAUDITORIA, que e uma
pergunta diferente da cobertura da auditoria.

A auditoria perguntava "todo arquivo do escopo foi lido?". A reauditoria pergunta
"todo arquivo QUE MUDOU foi conferido, e por quem?".

Três colunas de evidência, deliberadamente separadas porque valem coisas
diferentes:

  diff_lido_pelo_orquestrador  eu abri o diff (ou o arquivo resultante) e li.
                               Lista curada abaixo, escrita à mão. Não é
                               inferida de nada — inferir isto seria exatamente
                               o tipo de cobertura fictícia que esta missão
                               proíbe.
  teste_de_regressao           existe teste desta auditoria que exercita este
                               arquivo, e ele passa.
  origem                       qual pacote de trabalho produziu a mudança.

Um arquivo sem NENHUMA das duas primeiras é um buraco, e a planilha mostra isso
em vez de escondê-lo.
"""
import csv
import io
import re
import subprocess
import sys

REPO = sys.argv[1] if len(sys.argv) > 1 else "."


def git(*a):
    return subprocess.run(["git", *a], cwd=REPO, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout


BASE = git("merge-base", "origin/main", "HEAD").strip()
mudados = [f for f in git("diff", "--name-only", f"{BASE}...HEAD").split("\n") if f]

# ── Lista CURADA: arquivos cujo diff (ou conteudo resultante) eu abri e li ────
LIDOS = {
    "scripts/backup_postgres.sh", "app/query_filters.py", "app/schemas/lead.py",
    "tests/test_filter_normalization_and_backup.py", "docs/n8n-toolHttpRequest-guia.md",
    "docker-compose.yml", ".gitignore", "requirements.txt",
    ".github/workflows/test.yml", "tests/test_conversas_security.py",
    "templates/leads.html", "templates/partials/_lead_edit_modal.html",
    "templates/pipeline.html", "templates/segmentacao.html", "templates/tarefas.html",
    "app/schemas/pipeline.py", "tests/test_pipeline_inline_lead_edit.py",
    "conversas/app/services/crm.py", "conversas/app/routers/conversations.py",
    "conversas/static/js/conversas.js", "conversas/app/routers/media.py",
    "app/models/lead.py", "app/models/pipeline.py", "app/models/task.py",
    "app/models/user.py", "app/models/operational/card.py",
    "conversas/app/models/conversation.py", "migrations/m011_audit_unique_constraints.py",
    "tests/test_conversas_media_storage.py", "app/routers/tasks.py", "app/schemas/task.py",
    "conversas/app/config.py", "conversas/app/auth.py", "conversas/app/seed.py",
    "conversas/app/main.py", "conversas/app/routers/auth.py",
    "conversas/static/js/settings.js", "conversas/static/js/templates.js",
    "app/auth.py", "app/main.py", "app/services/ai_tools.py",
    "conversas/app/routers/pages.py", "tests/test_conversas_service_window.py",
    "tests/test_conversas_outbound_integrity.py", "tests/test_conversas_mobile_pwa.py",
    "tests/test_conversas_notifications.py", "tests/test_conversas_auth_hardening.py",
    "conversas/app/routers/webhook.py", "conversas/app/services/whatsapp.py",
    "conversas/app/services/outbound.py", "tests/test_frontend_injection_contract.py",
    "tests/test_conversas_webhook_signature.py", "app/routers/auth.py",
    "templates/login.html", "static/js/login.js",
    # lidos na segunda passada da reauditoria, fechando o buraco que a propria
    # planilha apontou na primeira geracao
    "app/routers/ai.py", "app/routers/analytics.py", "app/routers/tags.py",
    "app/routers/teams.py", "app/schemas/user.py",
    "conversas/app/routers/api_config.py", "conversas/app/schemas/api_config.py",
    "conversas/static/js/auth.js", "conversas/templates/login.html",
    "templates/equipes.html", "templates/relatorios.html",
    "Dockerfile", "conversas/Dockerfile", ".dockerignore", "conversas/.dockerignore",
    ".env.example", "migrations/README.md",
    # ── FASE 2: lidos/escritos por mim nesta fase ──────────────────────────
    "tests/test_n8n_contract_lead_update.py",       # escrito por mim
    "tests/test_conversas_agent_silence.py",        # escrito por mim
    "tests/test_postgres_dialect_divergence.py",    # lido e corrigido em 3 checks
    "docs/audit/N8N_CURRENT_STATE_RECONCILIATION.md",
    "docs/audit/N8N_MANUAL_CHANGES.md",
    "docs/audit/POSTGRES_VALIDATION.md",
    "docs/audit/BACKUP_RESTORE_VALIDATION.md",
    "n8n/workflows/live_exports/20260825_fase2/wf01_agente_bia.json",
    "n8n/workflows/live_exports/20260825_fase2/gerenciador_leads.json",
    "n8n/workflows/live_exports/20260825_fase2/formulario_site.json",
    "docs/audit/proposed_n8n/wf01_agente_bia.PROPOSTO.json",
    "docs/audit/proposed_n8n/gerenciador_leads.PROPOSTO.json",
}
# tests/test_backup_restore_e2e.py NAO entra: eu o EXECUTEI e reproduzi de forma
# independente a alegacao central dele (SIGPIPE), mas nao li o arquivo inteiro.
# Marcar como lido seria a cobertura ficticia que esta auditoria proibe.

# ── que teste desta auditoria toca cada arquivo ───────────────────────────────
NOVOS = [f for f in mudados if f.startswith("tests/test_")]
toca = {}
for t in NOVOS + ["tests/test_security_greps.py", "tests/test_render_templates.py"]:
    try:
        txt = io.open(f"{REPO}/{t}", encoding="utf-8").read()
    except OSError:
        continue
    alvos = set(re.findall(r"[\w./-]+\.(?:py|html|js|sh|yml)", txt))
    for m in re.finditer(r'ROOT\s*/\s*((?:"[^"]+"\s*/\s*)*"[^"]+")', txt):
        alvos.add("/".join(re.findall(r'"([^"]+)"', m.group(1))))
    ehconv = "CONVERSAS_DIR" in txt
    for m in re.finditer(r"(?:^|[^\w.])((?:app|conversas)(?:\.\w+){1,4})\b", txt, re.M):
        rel = m.group(1).replace(".", "/") + ".py"
        alvos.add(("conversas/" + rel) if (ehconv and rel.startswith("app/")) else rel)
    for a in alvos:
        toca.setdefault(a.lstrip("./"), set()).add(t.split("/")[-1])

# ── de qual pacote de trabalho veio ──────────────────────────────────────────
def origem(f):
    log = git("log", "--format=%s", f"{BASE}..HEAD", "--", f)
    prim = log.strip().split("\n")[0] if log.strip() else ""
    return prim[:70]


linhas = []
for f in sorted(mudados):
    add, rem = 0, 0
    st = git("diff", "--numstat", f"{BASE}...HEAD", "--", f).split()
    if len(st) >= 2 and st[0].isdigit():
        add, rem = int(st[0]), int(st[1])
    testes = sorted(toca.get(f, ()))
    linhas.append({
        "file": f,
        "linhas_adicionadas": add,
        "linhas_removidas": rem,
        "diff_lido_pelo_orquestrador": "sim" if f in LIDOS else "nao",
        "teste_de_regressao": ";".join(testes) if testes else "",
        "origem": origem(f),
    })

campos = ["file", "linhas_adicionadas", "linhas_removidas",
          "diff_lido_pelo_orquestrador", "teste_de_regressao", "origem"]
out = f"{REPO}/docs/audit/REAUDIT_COVERAGE.csv"
with io.open(out, "w", encoding="utf-8", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=campos)
    w.writeheader()
    w.writerows(linhas)

n = len(linhas)
lidos = sum(1 for r in linhas if r["diff_lido_pelo_orquestrador"] == "sim")
com_teste = sum(1 for r in linhas if r["teste_de_regressao"])
nenhum = [r["file"] for r in linhas
          if r["diff_lido_pelo_orquestrador"] == "nao" and not r["teste_de_regressao"]]
print(f"arquivos alterados: {n}")
print(f"  diff lido pelo orquestrador: {lidos} ({100*lidos//n}%)")
print(f"  com teste desta auditoria:   {com_teste} ({100*com_teste//n}%)")
print(f"  com PELO MENOS uma evidencia: {n - len(nenhum)} ({100*(n-len(nenhum))//n}%)")
print(f"  SEM evidencia nenhuma: {len(nenhum)}")
for f in nenhum:
    print(f"    - {f}")
