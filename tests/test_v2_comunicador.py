"""
BIA-V2 Fase 9 - IA Comunicadora de conversas/app/v2/comunicador.py.

Nao estava na lista de testes do plano (a Fase 9 so cita
tests/test_v2_output_guard.py), mas ha logica real de composicao+guard aqui
que pode quebrar silenciosamente - decisao desta implementacao de cobri-la
com um arquivo dedicado. Nenhum teste bate rede de verdade: a LLM e sempre
mockada via `unittest.mock.patch` sobre `app.v2.comunicador._chamar_gemini`
(.claude/rules/05-testing.md).

Roda standalone:  python tests/test_v2_comunicador.py
"""
import pathlib
import sys
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Conversas - o literal abaixo tambem e o discriminador de job do CI
# (.github/workflows/test.yml separa as suites com grep -l CONVERSAS_DIR).
CONVERSAS_DIR = ROOT / "conversas"

sys.path.insert(0, str(CONVERSAS_DIR))

failures = []
executados = []


def check(cond, msg):
    executados.append(msg)
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


from app.v2.contratos import ResultadoGuard, TriageData  # noqa: E402
from app.v2.output_guard import ACOES_PERMITIDAS  # noqa: E402
from app.v2 import comunicador  # noqa: E402
from app.v2.comunicador import ComunicadorIndisponivel, EntradaComunicador, redigir_resposta  # noqa: E402

_ACAO_OK = "informar_fatos_confirmados"
assert _ACAO_OK in ACOES_PERMITIDAS  # premissa do arquivo inteiro - falha alto se cair.


def _entrada(acao=_ACAO_OK, fatos=None, campos_faltantes=None, duvidas=None):
    """Fabrica uma `EntradaComunicador` valida, com defaults neutros -
    equivalente ao "nenhum fato, nenhum campo faltante, nenhuma duvida"."""
    return EntradaComunicador(
        acao=acao,
        fatos_confirmados=fatos if fatos is not None else TriageData(),
        campos_faltantes=list(campos_faltantes) if campos_faltantes is not None else [],
        duvidas=list(duvidas) if duvidas is not None else [],
    )


# ---------------------------------------------------------------------------
# A. Happy path - LLM devolve texto seguro, comunicador devolve ESSE texto.
# ---------------------------------------------------------------------------
with mock.patch("app.v2.comunicador._chamar_gemini", return_value="Oi! Faltou so o seu email para fecharmos.") as _mock_a:
    r_a = redigir_resposta(_entrada(campos_faltantes=["email"]))
check(isinstance(r_a, ResultadoGuard), "A1. redigir_resposta devolve ResultadoGuard")
check(r_a.permitido is True, "A2. texto seguro da LLM => permitido is True")
check(r_a.motivo is None, "A3. permitido => motivo is None")
check(r_a.texto_final == "Oi! Faltou so o seu email para fecharmos.", "A4. texto_final e exatamente o texto da LLM")
check(_mock_a.call_count == 1, "A5. a LLM foi chamada exatamente uma vez")

# ---------------------------------------------------------------------------
# B. LLM "vaza" valor monetario - o guard bloqueia e o texto vazado NUNCA
# volta no retorno do comunicador (prova de que o guard esta no CAMINHO,
# nao so declarado e ignorado).
# ---------------------------------------------------------------------------
_TEXTO_VAZADO = "Fica R$ 5.000 no total, combinado?"
with mock.patch("app.v2.comunicador._chamar_gemini", return_value=_TEXTO_VAZADO):
    r_b = redigir_resposta(_entrada())
check(r_b.permitido is False, "B1. texto com valor monetario da LLM => permitido is False")
check(r_b.motivo == "valor_monetario_detectado", "B2. motivo e o token de valor monetario")
check(r_b.texto_final == "", "B3. texto_final e vazio")
check(_TEXTO_VAZADO not in r_b.texto_final, "B4. o texto vazado NAO esta em texto_final")
check("5.000" not in r_b.texto_final and "R$" not in r_b.texto_final, "B5. nem o valor nem o simbolo sobrevivem")

# ---------------------------------------------------------------------------
# C. Acao nao autorizada - bloqueada ANTES de gastar uma chamada a LLM
# (decisao documentada em comunicador.py: fail-fast). A UNICA prova valida
# e o call_count da LLM mockada - checar so `permitido`/`motivo` nao provaria
# fail-fast, porque `filtrar_saida` bloquearia a acao de qualquer jeito
# mesmo que a LLM TIVESSE sido chamada (ver comentario no teste).
# ---------------------------------------------------------------------------
with mock.patch("app.v2.comunicador._chamar_gemini", return_value="isto nunca deveria ser lido") as _mock_c:
    r_c = redigir_resposta(_entrada(acao="excluir_conta_do_cliente"))
check(r_c.permitido is False, "C1. acao nao autorizada => permitido is False")
check(r_c.motivo == "acao_nao_autorizada", "C2. motivo e o token de acao nao autorizada")
check(r_c.texto_final == "", "C3. texto_final e vazio")
check(_mock_c.call_count == 0, "C4. PROVA de fail-fast: a LLM nunca foi chamada para uma acao nao autorizada")

# ---------------------------------------------------------------------------
# D. R-INTENT vale para `duvidas`: contar/repassar e legitimo, decidir sobre
# o CONTEUDO e proibido (contratos.py:151-157). Duas entradas com duvidas de
# mesmo TAMANHO e CONTEUDO diferente devem produzir o MESMO veredito -
# prova de que nenhuma branch olha para dentro do texto da duvida.
# ---------------------------------------------------------------------------
_prompts_capturados = []


def _llm_fake_captura(prompt):
    _prompts_capturados.append(prompt)
    return "Resposta neutra, sem valor monetario."


_prompts_capturados.clear()
with mock.patch("app.v2.comunicador._chamar_gemini", side_effect=_llm_fake_captura):
    r_d1 = redigir_resposta(_entrada(duvidas=["ele quer falar com um humano AGORA, e urgente"]))
    r_d2 = redigir_resposta(_entrada(duvidas=["duvida generica sem nenhuma palavra especial"]))
check(
    (r_d1.permitido, r_d1.motivo, r_d1.texto_final) == (r_d2.permitido, r_d2.motivo, r_d2.texto_final),
    "D1. CONTEUDO de duvidas (mesmo tamanho, texto diferente) nao muda o veredito",
)
check(len(_prompts_capturados) == 2, "D2. as duas chamadas foram capturadas")
check(
    "ele quer falar com um humano AGORA, e urgente" in _prompts_capturados[0],
    "D3. o TEXTO da duvida entra verbatim no prompt (repassar como dado e permitido)",
)
check(
    "duvida generica sem nenhuma palavra especial" in _prompts_capturados[1],
    "D4. idem para a segunda entrada",
)

# Uso legitimo do OUTRO lado da regra: contar/checar PRESENCA de duvidas
# pode, sim, mudar o prompt (nao e decisao operacional, e so composicao).
_prompts_capturados.clear()
with mock.patch("app.v2.comunicador._chamar_gemini", side_effect=_llm_fake_captura):
    redigir_resposta(_entrada(duvidas=[]))
    redigir_resposta(_entrada(duvidas=["uma duvida qualquer"]))
check("duvida(s)" not in _prompts_capturados[0], "D5. sem duvidas, a secao de duvidas NAO entra no prompt")
check("duvida(s)" in _prompts_capturados[1], "D6. com duvidas, a secao entra (contar/checar presenca e legitimo)")

# ---------------------------------------------------------------------------
# E. Falha da LLM propaga - redigir_resposta nao engole a excecao nem a
# disfarca de ResultadoGuard(permitido=False, ...) (ver docstring de
# ComunicadorIndisponivel: falha de infraestrutura != conteudo bloqueado).
# ---------------------------------------------------------------------------
def _llm_falha(prompt):
    raise RuntimeError("erro de rede simulado")


_erro_e = None
with mock.patch("app.v2.comunicador._chamar_gemini", side_effect=_llm_falha):
    try:
        redigir_resposta(_entrada())
    except RuntimeError as exc:
        _erro_e = exc
check(_erro_e is not None, "E1. redigir_resposta PROPAGA a falha da LLM, nunca a engole")

# ---------------------------------------------------------------------------
# F. `_chamar_gemini` sem GEMINI_API_KEY levanta ComunicadorIndisponivel -
# ponto isolado testado diretamente (nao via redigir_resposta), mesmo
# padrao de robustez de app/routers/ai.py (checagem antes de qualquer
# chamada real ao SDK).
# ---------------------------------------------------------------------------
check(issubclass(ComunicadorIndisponivel, RuntimeError), "F0. ComunicadorIndisponivel e RuntimeError (mesmo padrao de TransicaoInvalida)")
_erro_f = None
with mock.patch("app.v2.comunicador.GEMINI_API_KEY", ""):
    try:
        comunicador._chamar_gemini("prompt qualquer")
    except ComunicadorIndisponivel as exc:
        _erro_f = exc
check(_erro_f is not None, "F1. _chamar_gemini sem GEMINI_API_KEY levanta ComunicadorIndisponivel")
check(str(_erro_f) == "gemini_api_key_ausente", f"F2. mensagem e o token fechado, sem detalhe extra (obtido={str(_erro_f)!r})")

# ---------------------------------------------------------------------------
# F3-F5. TETO DE ESPERA na chamada ao SDK. Sem `http_options.timeout` o
# google-genai nao impoe teto nenhum e quem chama fica preso indefinidamente -
# o caminho vivo e a rota SOMBRA (app/v2/sombra.py) entram aqui a partir de uma
# rota HTTP. Espelha 7f/7f2/7f3 de tests/test_v2_interpretador.py: os dois
# pontos que falam com o Gemini precisam do MESMO guard.
#
# A unidade e MILISSEGUNDOS (`types.HttpOptions.timeout` e documentado como
# "Timeout for the request in milliseconds" no SDK instalado); passar segundos
# daria um teto de 90ms e reprovaria toda chamada real - F5 pega isso.
# ---------------------------------------------------------------------------
class _RespostaGeminiFalsa:
    def __init__(self, texto):
        self.text = texto


class _ModelsFalso:
    def __init__(self, texto):
        self._texto = texto

    def generate_content(self, *, model, contents):
        return _RespostaGeminiFalsa(self._texto)


class _ClienteGeminiFalso:
    def __init__(self, texto):
        self.models = _ModelsFalso(texto)


with mock.patch("app.v2.comunicador.GEMINI_API_KEY", "fake-key-de-teste"):
    with mock.patch(
        "app.v2.comunicador.genai.Client",
        side_effect=lambda api_key, http_options: _ClienteGeminiFalso("texto qualquer"),
    ) as _mock_cliente_f3:
        comunicador._chamar_gemini("prompt qualquer")

_http_options_f3 = _mock_cliente_f3.call_args.kwargs.get("http_options")
check(
    _http_options_f3 is not None and _http_options_f3.timeout is not None,
    f"F3. genai.Client recebe http_options com timeout (obtido={_http_options_f3!r})",
)
check(
    _http_options_f3 is not None and _http_options_f3.timeout == comunicador._TIMEOUT_GEMINI_MS,
    f"F4. o timeout passado e `_TIMEOUT_GEMINI_MS` "
    f"(obtido={getattr(_http_options_f3, 'timeout', None)!r}, "
    f"esperado={comunicador._TIMEOUT_GEMINI_MS!r})",
)
check(
    10_000 <= comunicador._TIMEOUT_GEMINI_MS <= 600_000,
    f"F5. o valor esta na faixa de MILISSEGUNDOS plausivel (10s-600s) - pega a "
    f"troca acidental de unidade para segundos (obtido={comunicador._TIMEOUT_GEMINI_MS!r})",
)

# ---------------------------------------------------------------------------
# G. Sanidade do prompt - fatos_confirmados, campos_faltantes e acao
# realmente chegam ao texto que vai para a LLM (o comunicador nao ignora a
# entrada que recebeu).
# ---------------------------------------------------------------------------
_prompts_capturados.clear()
with mock.patch("app.v2.comunicador._chamar_gemini", side_effect=_llm_fake_captura):
    redigir_resposta(_entrada(fatos=TriageData(nome="Maria"), campos_faltantes=["email", "total_pessoas"]))
check("Maria" in _prompts_capturados[0], "G1. fatos_confirmados (nome) aparece no prompt")
check("email" in _prompts_capturados[0] and "total_pessoas" in _prompts_capturados[0], "G2. campos_faltantes aparecem no prompt")
check(_ACAO_OK in _prompts_capturados[0], "G3. a acao aparece no prompt")

# Achado de revisao de codigo: TriageData.destinos tem default_factory=list
# (`[]`, nunca None) - exclude_none=True sozinho NAO removia essa chave, e
# uma EntradaComunicador totalmente fresca (nada confirmado ainda) gerava
# "Fatos ja confirmados pelo cliente: {'destinos': []}" no prompt, dizendo a
# IA que "destinos vazio" era um FATO CONFIRMADO em vez de "ainda nao
# perguntado". Fix: exclude_defaults=True tambem. Prova simetrica a D5, que
# ja cobre o mesmo principio para `duvidas`.
_prompts_capturados.clear()
with mock.patch("app.v2.comunicador._chamar_gemini", side_effect=_llm_fake_captura):
    redigir_resposta(_entrada())  # fatos=None -> TriageData() fresca, so defaults
check(
    "Fatos ja confirmados" not in _prompts_capturados[0],
    "G4. TriageData() fresca (so defaults, incluindo destinos=[]) NAO gera secao de fatos confirmados",
)
check(
    "destinos" not in _prompts_capturados[0],
    "G4b. a palavra 'destinos' nem aparece no prompt quando nada foi confirmado",
)


print()
print(f"{len(executados)} verificacoes executadas.")
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
