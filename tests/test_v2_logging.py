"""
BIA-V2 Fase 0 / Task 0.1 — o logging INFO precisa sair do processo.

Achado da auditoria (CONFIRMADO empiricamente): nenhum dos dois `main.py`
chama `basicConfig`/`dictConfig`; os Dockerfiles sobem uvicorn sem
`--log-config`; e o `LOGGING_CONFIG` do proprio uvicorn configura apenas os
loggers `uvicorn*` — nunca define a chave "root". Resultado: o root logger
fica no default do Python (WARNING, sem handler) e TODA a trilha `.info` que
o codigo ja escreve ("Handoff BIA->humano na conversa X", "Debounce: enviando
N msg(s)", "Resposta da Bia") morre dentro do processo.

As chamadas `.info` ja existem e estao corretas. Falta so o handler.

Prova que:
  1. Depois de `configurar_logging()`, um record INFO chega ao handler.
  2. O formatter inclui nivel e nome do logger.
  3. O root logger fica em INFO ou mais permissivo.
  4. O root logger tem pelo menos um handler instalado.
  5. `configurar_logging()` e idempotente — chamar duas vezes nao duplica
     handler (o que duplicaria toda linha de log em producao).
  6. LOG_LEVEL do ambiente e respeitado.

ESCOPO: apenas o Conversas. CRM (`app/`) e Conversas (`conversas/app/`) sao
dois pacotes independentes que se chamam `app` — nao existe import
compartilhado entre eles. O logging do CRM e HOTFIX-08, fora desta fase.

Roda standalone:  python tests/test_v2_logging.py
"""
import io
import logging
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"

os.environ["ENVIRONMENT"] = "development"
os.environ["SECRET_KEY"] = "test-secret-key"

sys.path.insert(0, str(CONVERSAS_DIR))

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


# O handler e construido a partir de `ext://sys.stdout`, resolvido no momento
# da configuracao. Redirecionando ANTES de configurar, o handler real da
# aplicacao passa a escrever no buffer — a prova cobre a cadeia inteira
# (nivel do root + handler + formatter), nao apenas o nivel.
buffer = io.StringIO()
stdout_original = sys.stdout
sys.stdout = buffer
try:
    from app.logging_config import configurar_logging  # noqa: E402

    configurar_logging()
    logging.getLogger("app.teste.v2").info("mensagem-de-info")
finally:
    sys.stdout = stdout_original

saida = buffer.getvalue()
root = logging.getLogger()

check("mensagem-de-info" in saida, "1. record INFO chega ao handler do root logger")
check("INFO" in saida and "app.teste.v2" in saida, "2. formatter inclui nivel e nome do logger")
check(root.level <= logging.INFO, f"3. root logger em INFO ou mais permissivo (atual={root.level})")
check(len(root.handlers) >= 1, "4. root logger tem pelo menos um handler")

# 5. Idempotencia — em producao, handler duplicado significa cada linha de log
#    impressa duas vezes, o que corrompe qualquer contagem feita sobre o log.
handlers_antes = len(root.handlers)
buffer2 = io.StringIO()
sys.stdout = buffer2
try:
    configurar_logging()
finally:
    sys.stdout = stdout_original
check(
    len(root.handlers) == handlers_antes,
    f"5. chamada repetida nao duplica handler ({handlers_antes} -> {len(root.handlers)})",
)

# 6. LOG_LEVEL do ambiente.
os.environ["LOG_LEVEL"] = "WARNING"
buffer3 = io.StringIO()
sys.stdout = buffer3
try:
    configurar_logging()
finally:
    sys.stdout = stdout_original
    os.environ.pop("LOG_LEVEL", None)
check(root.level == logging.WARNING, "6. LOG_LEVEL do ambiente e respeitado")

print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
