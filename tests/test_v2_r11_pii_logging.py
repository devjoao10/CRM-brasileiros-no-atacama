"""
BIA-V2 R11 - auditoria de PII exposure em logging (lado Conversas).

Escopo da auditoria R11 (ver app/logging_config.py, HOTFIX-08 / Task 0.1):
todo `logger.*`/`logging.*`/`print(...)` do caminho executavel da BIA V2 do
lado Conversas (`conversas/app/v2/**`) - incluindo a IA Interpretadora
(interpretador.py) e a IA Comunicadora (comunicador.py).

DEPOIS DE LER TODOS OS 17 ARQUIVOS DE `conversas/app/v2/`, o UNICO call site
que loga uma excecao capturada (`logger.exception`, que anexa `exc_info`
implicito - o traceback INTEIRO, inclusive `str(excecao)`) e
`comunicador.py::_chamar_gemini`. Todo o resto (`handoff.py`,
`worker_retomada.py`, `worker_reconciliacao.py`) so interpola ids e tokens de
vocabulario fechado (`operacao_id`, `conversation_id`, `error_code`,
contadores) - nunca uma excecao inteira.

Este teste prova que `_chamar_gemini` (chamado por `redigir_resposta`) NUNCA
deixa o `prompt` (que carrega fatos do cliente: nome, email, duvidas) alcancar
o log, mesmo quando a chamada ao Gemini falha e o record de log carrega o
traceback inteiro. Cobre as DUAS formas REAIS de falha que o SDK instalado
pode propagar (confirmado por inspecao do pacote instalado nesta venv -
google-genai==1.75.0 e httpx==0.28.1, os MESMOS pins de
conversas/requirements.txt):

  1. falha de TRANSPORTE (`httpx.RequestError` e subclasses) - `str(exc)` e
     uma mensagem curta e generica (ex.: "Failed to establish a
     connection."); a classe (`httpx/_exceptions.py`) nao sobrescreve
     `__str__`/`__repr__` para incluir `.request`/`.response`, entao nem
     corpo, nem cabecalho (onde vai a API key, `x-goog-api-key`), nem URL
     entram no texto logado por padrao.
  2. erro da API do Gemini (`google.genai.errors.APIError` e subclasses,
     ex. `ClientError`) - `str(exc)` e `f'{code} {status}. {details}'`, onde
     `details` e o corpo da RESPOSTA de erro do Gemini (o que ELE reclama),
     nunca um eco do PROMPT que foi enviado.

Nenhum dos dois cenarios bate rede real (.claude/rules/05-testing.md) - so o
`genai.Client` e substituido, mesmo ponto de substituicao que
tests/test_v2_comunicador.py ja usa para as demais provas deste modulo (aqui
um nivel mais fundo, porque e o PROPRIO `_chamar_gemini`, e nao so
`redigir_resposta`, que precisa ser exercitado para chegar no `except` que
loga).

Roda standalone: python tests/test_v2_r11_pii_logging.py
"""
import logging
import pathlib
import sys
from unittest import mock

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Conversas - o literal abaixo tambem e o discriminador de job do CI
# (.github/workflows/test.yml separa as suites com grep -l CONVERSAS_DIR).
CONVERSAS_DIR = ROOT / "conversas"

sys.path.insert(0, str(CONVERSAS_DIR))

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


from google.genai import errors as genai_errors  # noqa: E402

from app.v2 import comunicador  # noqa: E402
from app.v2.comunicador import ComunicadorIndisponivel, EntradaComunicador, redigir_resposta  # noqa: E402
from app.v2.contratos import TriageData  # noqa: E402
from app.v2.output_guard import ACOES_PERMITIDAS  # noqa: E402


class _ListaHandler(logging.Handler):
    """Handler minimo que acumula as linhas formatadas de UM logger nomeado -
    anexado direto em `app.v2.comunicador` (sem depender do
    redirect-de-stdout que tests/test_v2_logging.py usa para provar o
    root logger; aqui um handler isolado no logger do proprio modulo basta e
    nao interfere no processo)."""

    def __init__(self):
        super().__init__()
        self.linhas = []

    def emit(self, record):
        self.linhas.append(self.format(record))


def capturar_logs(fn):
    logger = logging.getLogger(comunicador.__name__)
    handler = _ListaHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    nivel_antigo = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        fn()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(nivel_antigo)
    return "\n".join(handler.linhas)


_ACAO_OK = "informar_fatos_confirmados"
assert _ACAO_OK in ACOES_PERMITIDAS  # premissa do arquivo - falha alto se cair.

# Dado FALSO, propositalmente PII-shaped (NUNCA dado real) - especifico o
# bastante para nao aparecer por acidente em nenhuma mensagem tecnica do SDK.
NOME_PII_FALSO = "Fulano PII de Teste"
EMAIL_PII_FALSO = "fulano.pii.teste@exemplo-fake.test"
DUVIDA_PII_FALSA = "meu numero e 11-99999-0000 marcador-pii-teste"
API_KEY_FALSA = "chave-gemini-fake-de-teste-r11"


def _entrada_com_pii_falsa():
    return EntradaComunicador(
        acao=_ACAO_OK,
        fatos_confirmados=TriageData(nome=NOME_PII_FALSO, email=EMAIL_PII_FALSO),
        campos_faltantes=[],
        duvidas=[DUVIDA_PII_FALSA],
    )


class _ChamadaCapturada:
    """So para a prova de nao-vacuidade: guarda o prompt REAL que
    `_chamar_gemini` recebeu, para confirmar que a PII falsa estava mesmo em
    escopo no instante da falha (senao o teste provaria uma ausencia trivial,
    nunca exercitada de verdade)."""

    prompt = None


class _ClienteFalso:
    """Fake de `genai.Client` - so o suficiente para `_chamar_gemini` chegar
    na chamada real e estourar a excecao injetada, sem tocar rede."""

    def __init__(self, excecao):
        self._excecao = excecao
        self.models = self

    def generate_content(self, *, model, contents):
        _ChamadaCapturada.prompt = contents
        raise self._excecao


def _tentar_redigir(entrada):
    try:
        redigir_resposta(entrada)
    except ComunicadorIndisponivel:
        pass  # esperado - a prova e sobre o que foi LOGADO, nao o retorno.


# ---------------------------------------------------------------------------
# 1. Falha de TRANSPORTE (httpx.ConnectError) - string curta e generica.
# ---------------------------------------------------------------------------
_ChamadaCapturada.prompt = None
excecao_transporte = httpx.ConnectError("Failed to establish a connection.")
with mock.patch("app.v2.comunicador.GEMINI_API_KEY", API_KEY_FALSA), \
     mock.patch.object(comunicador.genai, "Client", return_value=_ClienteFalso(excecao_transporte)):
    saida_1 = capturar_logs(lambda: _tentar_redigir(_entrada_com_pii_falsa()))

check(
    NOME_PII_FALSO in (_ChamadaCapturada.prompt or ""),
    "1-sanity. o prompt REALMENTE continha o nome PII-falso no instante da falha (transporte)",
)
check("[COMUNICADOR] chamada ao Gemini falhou" in saida_1, "1a. a mensagem fixa e segura foi logada")
check(NOME_PII_FALSO not in saida_1, "1b. nome PII-falso NAO aparece no log (falha de transporte)")
check(EMAIL_PII_FALSO not in saida_1, "1c. email PII-falso NAO aparece no log (falha de transporte)")
check(DUVIDA_PII_FALSA not in saida_1, "1d. texto da duvida PII-falsa NAO aparece no log (falha de transporte)")
check(API_KEY_FALSA not in saida_1, "1e. a API key (fake) NAO aparece no log (falha de transporte)")

# ---------------------------------------------------------------------------
# 2. Erro da API do Gemini (google.genai.errors.ClientError) - `details` e o
# corpo da RESPOSTA de erro, nunca eco do prompt enviado.
# ---------------------------------------------------------------------------
_ChamadaCapturada.prompt = None
excecao_api = genai_errors.ClientError(
    400, {"message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}, None
)
with mock.patch("app.v2.comunicador.GEMINI_API_KEY", API_KEY_FALSA), \
     mock.patch.object(comunicador.genai, "Client", return_value=_ClienteFalso(excecao_api)):
    saida_2 = capturar_logs(lambda: _tentar_redigir(_entrada_com_pii_falsa()))

check(
    NOME_PII_FALSO in (_ChamadaCapturada.prompt or ""),
    "2-sanity. o prompt REALMENTE continha o nome PII-falso no instante da falha (erro de API)",
)
check("[COMUNICADOR] chamada ao Gemini falhou" in saida_2, "2a. a mensagem fixa e segura foi logada (erro de API)")
check(NOME_PII_FALSO not in saida_2, "2b. nome PII-falso NAO aparece no log (erro de API)")
check(EMAIL_PII_FALSO not in saida_2, "2c. email PII-falso NAO aparece no log (erro de API)")
check(DUVIDA_PII_FALSA not in saida_2, "2d. texto da duvida PII-falsa NAO aparece no log (erro de API)")
check(API_KEY_FALSA not in saida_2, "2e. a API key (fake) NAO aparece no log (erro de API)")
check("INVALID_ARGUMENT" in saida_2, "2f. o status tecnico REAL do erro da API aparece (diagnostico preservado)")


print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
