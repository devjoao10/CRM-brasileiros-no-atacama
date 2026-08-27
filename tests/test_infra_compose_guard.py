"""
OPS-CONV-MEDIA-01 — Guard estatico do docker-compose (prontidao de producao).

Protege os fixes C1/C2 do gate PROD-READINESS-CONVERSAS-01 contra regressao:
  C1: volume PERSISTENTE de midia do Conversas (conversas_media) declarado e
      montado, com CONVERSAS_MEDIA_DIR apontando para o mount.
  C2: META_APP_SECRET passado ao servico conversas (webhook HMAC em prod).
E invariantes de seguranca do compose:
  - segredos SEMPRE por referencia ${VAR} (nunca literal);
  - deploy workflow segue APENAS workflow_dispatch (sem on: push).

Estatico puro (le YAML como texto + parse) — nao executa Docker.
Roda standalone:  python tests/test_infra_compose_guard.py
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


print("OPS-CONV-MEDIA-01 — guard do docker-compose")

# C1 — volume de midia
check("conversas_media:" in COMPOSE.split("volumes:")[-1],
      "volume nomeado conversas_media declarado no bloco volumes")
check("- conversas_media:/app/data/media" in COMPOSE,
      "servico conversas monta conversas_media em /app/data/media")
check("- CONVERSAS_MEDIA_DIR=/app/data/media" in COMPOSE,
      "CONVERSAS_MEDIA_DIR aponta para o path do volume")

# C2 — META_APP_SECRET por referencia
check("- META_APP_SECRET=${META_APP_SECRET" in COMPOSE,
      "META_APP_SECRET passado ao conversas por REFERENCIA ${...}")

# AUDIT-2026-08-WF2 — item de `environment` com `: ` PRECISA de aspas.
#
# `- VAR=${VAR:-Vendas: Principal}` nao e string para o YAML: o `: ` (dois
# pontos SEGUIDO DE ESPACO) faz o parser ler o item da lista como MAPA, e o
# compose recusa o arquivo inteiro com
# `services.conversas.environment.[7]: unexpected type map[string]interface {}`.
# Foi bloqueador de deploy: o compose ficou invalido em producao.
#
# `${VAR:-...}` sozinho nao dispara nada — ali o dois-pontos vem seguido de `-`,
# nao de espaco. Quem quebra e o espaco no VALOR (`Vendas: Principal`), entao a
# regra e sobre `: `, nao sobre `:`.
#
# Sem PyYAML de proposito: ele nao esta em requirements.txt nem em
# conversas/requirements.txt, e este guard tem de rodar no CI como esta.
_env_sem_aspas = []
for _linha in COMPOSE.splitlines():
    _cru = _linha.strip()
    if not _cru.startswith("- ") or "=" not in _cru:
        continue
    _item = _cru[2:]
    if ": " in _item and not (_item.startswith('"') and _item.endswith('"')):
        _env_sem_aspas.append(_item)
check(not _env_sem_aspas,
      f"todo item de environment com `: ` esta entre aspas — sem elas o YAML le "
      f"a linha como MAPA e o compose recusa o arquivo inteiro "
      f"(violacoes: {_env_sem_aspas})")

# Segredos nunca literais no compose (toda credencial e ${VAR})
secretish = re.findall(r"(SECRET_KEY|META_APP_SECRET|META_ACCESS_TOKEN|POSTGRES_PASSWORD)=([^\s]+)", COMPOSE)
literals = [(k, v) for k, v in secretish if not v.startswith("${")]
check(not literals, f"nenhum segredo LITERAL no compose (violacoes: {[k for k, _ in literals]})")

# Healthcheck em TODO servico de aplicacao (crm e conversas) — sem health o
# Docker nao reinicia container travado e o Traefik segue roteando para ele.
for _svc, _port in (("crm", 8000), ("conversas", 8001)):
    _m = re.search(rf"\n  {_svc}:\n(.*?)(?=\n  \S|\Z)", COMPOSE, re.S)
    check(_m is not None and f"localhost:{_port}/api/health" in _m.group(1),
          f"servico {_svc} tem healthcheck apontando para :{_port}/api/health")

# Deploy continua manual (OPS-01)
check("workflow_dispatch" in WORKFLOW, "deploy.yml tem workflow_dispatch")
on_block = WORKFLOW.split("on:", 1)[1].split("jobs:", 1)[0]
check("push" not in on_block, "deploy.yml SEM gatilho de push (deploy manual)")

# Fail-fast no script SSH. Sem `set -e`, uma falha em `docker compose build`
# seguiria para `up -d` (imagem VELHA + config nova) e o `echo` final ainda
# devolveria exit 0 — deploy parcial com o workflow VERDE.
_script = WORKFLOW.split("script: |", 1)[-1]
_set_e = _script.find("set -e")
check(_set_e != -1, "deploy.yml: script SSH tem `set -e` (fail-fast)")
for _cmd in ("git pull", "docker compose build", "docker compose up"):
    check(_set_e != -1 and 0 <= _set_e < _script.find(_cmd),
          f"`set -e` vem ANTES de `{_cmd}`")

# .env.example documenta os NOMES (sem valores reais)
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")
check("CONVERSAS_MEDIA_DIR" in ENV_EXAMPLE, ".env.example documenta CONVERSAS_MEDIA_DIR")
check("META_APP_SECRET" in ENV_EXAMPLE, ".env.example documenta META_APP_SECRET")

if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nGUARD DE INFRA OK")
