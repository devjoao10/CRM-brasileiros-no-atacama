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

# Segredos nunca literais no compose (toda credencial e ${VAR})
secretish = re.findall(r"(SECRET_KEY|META_APP_SECRET|META_ACCESS_TOKEN|POSTGRES_PASSWORD)=([^\s]+)", COMPOSE)
literals = [(k, v) for k, v in secretish if not v.startswith("${")]
check(not literals, f"nenhum segredo LITERAL no compose (violacoes: {[k for k, _ in literals]})")

# Deploy continua manual (OPS-01)
check("workflow_dispatch" in WORKFLOW, "deploy.yml tem workflow_dispatch")
on_block = WORKFLOW.split("on:", 1)[1].split("jobs:", 1)[0]
check("push" not in on_block, "deploy.yml SEM gatilho de push (deploy manual)")

# .env.example documenta os NOMES (sem valores reais)
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")
check("CONVERSAS_MEDIA_DIR" in ENV_EXAMPLE, ".env.example documenta CONVERSAS_MEDIA_DIR")
check("META_APP_SECRET" in ENV_EXAMPLE, ".env.example documenta META_APP_SECRET")

if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nGUARD DE INFRA OK")
