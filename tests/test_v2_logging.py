"""
BIA-V2 Fase 0 / Task 0.1 — o logging INFO precisa sair do processo,
sem deixar o cliente forjar linha de log.

Achado da auditoria (CONFIRMADO empiricamente): nenhum dos dois `main.py`
chama `basicConfig`/`dictConfig`; os Dockerfiles sobem uvicorn sem
`--log-config`; e o `LOGGING_CONFIG` do proprio uvicorn configura apenas os
loggers `uvicorn*` — nunca define a chave "root". Resultado: o root logger
fica no default do Python (WARNING, sem handler) e TODA a trilha `.info` que
o codigo ja escreve ("Handoff BIA->humano na conversa X", "Debounce: enviando
N msg(s)", "Resposta da Bia") morre dentro do processo.

Precisao importante: `.warning`/`.error` NAO eram perdidos antes — o Python
usa `logging.lastResort` (stderr) para WARNING+ sem handler. A superficie
que este WP realmente torna visivel e `.info`/`.debug`.

E e por isso que a sanitizacao entra junto: varias dessas chamadas `.info`
interpolam texto do cliente (`webhook.py` registra nome de perfil e os
primeiros 50 caracteres da mensagem). Texto de WhatsApp aceita quebra de
linha; sem tratamento, o cliente FORJA uma linha de log. Sanitizar no
formatter cobre todas as chamadas de uma vez, sem tocar em arquivo da V1.

Prova que:
  1. Um record INFO chega ao handler.
  2. O formatter inclui nivel e nome do logger.
  3. O root logger fica em INFO ou mais permissivo.
  4. O root logger tem pelo menos um handler instalado.
  5. `configurar_logging()` nao duplica HANDLER.
  6. `configurar_logging()` nao duplica LINHA DE LOG (o dano real).
  7. LOG_LEVEL do ambiente e respeitado.
  8. LOG_LEVEL="" (var setada vazia) cai para INFO.
  9. LOG_LEVEL invalido falha alto, no boot — nao silenciosamente.
 10. Quebra de linha vinda do cliente e escapada (injecao de log).
 11. Traceback de `exc_info` continua multilinha (a sanitizacao nao o achata).
 12. Importar `app.main` de verdade instala o handler, na sequencia real.

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
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "v2_logging_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(CONVERSAS_DIR))

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


def capturar(fn):
    """Roda `fn` com sys.stdout redirecionado e devolve o que foi escrito.

    O handler resolve `ext://sys.stdout` no momento da construcao, entao
    redirecionar ANTES de configurar faz o handler REAL da aplicacao escrever
    no buffer — a prova cobre a cadeia inteira (nivel do root + handler +
    formatter), nao um mock.
    """
    buffer = io.StringIO()
    original = sys.stdout
    sys.stdout = buffer
    try:
        fn()
    finally:
        sys.stdout = original
    return buffer.getvalue()


from app.logging_config import configurar_logging  # noqa: E402

root = logging.getLogger()

saida = capturar(
    lambda: (configurar_logging(), logging.getLogger("app.teste.v2").info("mensagem-de-info"))
)

check("mensagem-de-info" in saida, "1. record INFO chega ao handler do root logger")
check("INFO" in saida and "app.teste.v2" in saida, "2. formatter inclui nivel e nome do logger")
check(root.level <= logging.INFO, f"3. root logger em INFO ou mais permissivo (atual={root.level})")
check(len(root.handlers) >= 1, "4. root logger tem pelo menos um handler")

# 5 e 6. Idempotencia. A contagem de handlers sozinha nao prova o que importa:
# o dano real de reconfigurar errado e a MESMA linha sair duas vezes.
handlers_antes = len(root.handlers)
saida_repetida = capturar(
    lambda: (configurar_logging(), logging.getLogger("app.teste.v2").info("linha-unica"))
)
check(
    len(root.handlers) == handlers_antes,
    f"5. chamada repetida nao duplica handler ({handlers_antes} -> {len(root.handlers)})",
)
check(
    saida_repetida.count("linha-unica") == 1,
    f"6. chamada repetida nao duplica LINHA de log (contou {saida_repetida.count('linha-unica')})",
)

# 7 e 8. LOG_LEVEL do ambiente.
os.environ["LOG_LEVEL"] = "WARNING"
capturar(configurar_logging)
check(root.level == logging.WARNING, "7. LOG_LEVEL do ambiente e respeitado")

os.environ["LOG_LEVEL"] = ""
capturar(configurar_logging)
check(root.level == logging.INFO, "8. LOG_LEVEL vazio cai para INFO, nao quebra")
os.environ.pop("LOG_LEVEL", None)

# 9. LOG_LEVEL invalido. Falhar no import derruba o boot com traceback claro,
# em vez de subir silenciosamente no nivel errado — e a escolha certa para um
# servico de producao, e precisa ficar travada por teste.
os.environ["LOG_LEVEL"] = "verbose"
levantou = False
try:
    capturar(configurar_logging)
except ValueError:
    levantou = True
finally:
    os.environ.pop("LOG_LEVEL", None)
    capturar(configurar_logging)
check(levantou, "9. LOG_LEVEL invalido levanta ValueError (fail-closed, nao fail-silent)")

# 10. Injecao de log. `webhook.py` interpola nome de perfil e texto do cliente
# direto na f-string; texto de WhatsApp aceita quebra de linha.
texto_forjado = "oi\n2026-01-01 00:00:00 ERROR app.fake LINHA FORJADA PELO CLIENTE"
saida_injecao = capturar(
    lambda: (configurar_logging(), logging.getLogger("app.teste.v2").info(texto_forjado))
)
# O buffer contem tambem a linha "logging configurado em ..." emitida pela
# propria `configurar_logging()`. O que importa e que o texto forjado NAO se
# partiu: as duas metades tem de estar na MESMA linha.
linhas_forjadas = [ln for ln in saida_injecao.splitlines() if "LINHA FORJADA" in ln]
check(
    len(linhas_forjadas) == 1 and "oi" in linhas_forjadas[0],
    f"10. texto do cliente fica em UMA linha, sem se partir (achou {len(linhas_forjadas)})",
)
check("\\n" in saida_injecao, "10b. a quebra aparece escapada como \\n, nao perdida")

# 11. A sanitizacao nao pode achatar traceback — e a informacao mais util num erro.
def _com_excecao():
    # Reconfigura dentro da captura: o handler resolve `ext://sys.stdout` no
    # momento da construcao, entao so escreve no buffer desta chamada.
    configurar_logging()
    try:
        raise ValueError("erro-de-teste")
    except ValueError:
        logging.getLogger("app.teste.v2").error("falhou", exc_info=True)


saida_tb = capturar(_com_excecao)
check(
    saida_tb.count("\n") >= 3 and "Traceback" in saida_tb,
    "11. traceback de exc_info continua multilinha",
)

# 12. A sequencia real de imports do app — nenhum outro teste deste WP a exercita,
# e e ela que roda em producao.
def _importar_main():
    import app.main  # noqa: F401


capturar(_importar_main)
check(len(root.handlers) >= 1, "12. importar app.main deixa o root logger com handler")
check(root.level <= logging.INFO, "12b. importar app.main deixa o root em INFO")

print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
