"""
BIA-V2 Fase 8 - IA Interpretadora (conversas/app/v2/interpretador.py).

Cliente LLM SEMPRE mockado (`interpretador._chamar_llm` substituida) - nenhum
teste aqui bate rede real, conforme `.claude/rules/05-testing.md` ("Mock only
external boundaries"). O mock reatribui o atributo do MODULO, mesmo padrao ja
usado em `tests/test_conversas_agent_timeout.py` para
`whatsapp.send_text_message` - `interpretar()` chama `_chamar_llm` como nome
livre resolvido no namespace do proprio modulo em tempo de chamada, entao a
substituicao alcanca a chamada real.

Casos minimos exigidos pela fase (ver prompt da tarefa):
  1. resposta valida -> AIInterpretationResult correto, com STARTED e
     COMPLETED emitidos NESSA ordem, nenhum FAILED.
  2. campo extra nao declarado -> InterpretacaoFalhou, FAILED emitido
     (error_code=RESPOSTA_INVALIDA), nenhum resultado parcial devolvido.
  3. campo obrigatorio faltando (explicit_human_request) -> mesma coisa.
  4. tipo errado (intent como numero) -> mesma coisa.
  4b. bonus: JSON sintaticamente invalido (nem chega a ser um objeto) -> mesma
      coisa - prova que a validacao cobre sintaxe, nao so schema.
  5. bonus: a propria chamada ao modelo falha (rede/SDK) -> InterpretacaoFalhou
     com error_code DIFERENTE (CHAMADA_LLM_FALHOU), distinguindo "o modelo
     nunca respondeu" de "o modelo respondeu algo invalido".
  6. R-INTENT deste arquivo especifico: interpretador.py nunca acessa o
     CONTEUDO de `intent` NEM de `duvidas` para decidir nada - varredura AST
     própria, no espirito de tests/test_v2_r_intent.py (que cobre so
     `intent`, repo-wide; aqui cobrimos os dois campos, so neste arquivo).
  7. Lacuna de cobertura (achado de revisao python-reviewer): as secoes 1-6
     acima substituem `_chamar_llm` INTEIRA antes de chamar `interpretar()`,
     entao o corpo real dessa funcao nunca roda sob teste. Esta secao chama
     `_chamar_llm` DIRETAMENTE (nunca via `interpretar()`) e cobre os dois
     guard clauses reais: GEMINI_API_KEY ausente/vazia, e resposta do Gemini
     sem texto (`.text` vazio/None) - mockando so `genai.Client` (o cliente
     do SDK) para o segundo caso, nunca `_chamar_llm`, mesmo nivel de mock
     do grupo F de tests/test_v2_comunicador.py.

Roda standalone:  python tests/test_v2_interpretador.py
"""
import ast
import os
import pathlib
import sys
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Conversas - o literal abaixo tambem e o discriminador de job do CI
# (.github/workflows/test.yml separa as suites com grep -l CONVERSAS_DIR).
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "v2_interpretador_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"
# Nunca usada de verdade - _chamar_llm e substituida em TODO caso abaixo.
os.environ["GEMINI_API_KEY"] = ""

sys.path.insert(0, str(CONVERSAS_DIR))

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models.evento import ConversationEvent  # noqa: E402
from app.v2.contratos import AIInterpretationResult  # noqa: E402
import app.v2.interpretador as interpretador  # noqa: E402
from app.v2.interpretador import InterpretacaoFalhou  # noqa: E402

# Capturada AQUI, antes de qualquer secao abaixo reatribuir
# interpretador._chamar_llm (secoes 1-6 fazem isso). E o unico jeito da
# secao 7 alcancar o corpo REAL da funcao mais adiante no arquivo - se
# fosse lida so na hora de usar, o atributo do modulo ja teria sido
# sobrescrito. Continua resolvendo globals (GEMINI_API_KEY, genai) no
# namespace de interpretador em tempo de chamada, entao mock.patch sobre
# "app.v2.interpretador.X" alcanca esta funcao normalmente.
_CHAMAR_LLM_REAL = interpretador._chamar_llm

Base.metadata.create_all(bind=engine)
db = SessionLocal()

_conv_seq = {"n": 0}


def proxima_conversa() -> int:
    """`conversation_id` sem ForeignKey (app/models/evento.py) - qualquer
    inteiro serve, mesmo padrao de tests/test_v2_eventos.py. Um id por caso
    mantem os eventos de cada caso isolados na consulta abaixo."""
    _conv_seq["n"] += 1
    return _conv_seq["n"]


def eventos_da_conversa(conversation_id):
    return (
        db.query(ConversationEvent)
        .filter(ConversationEvent.conversation_id == conversation_id)
        .order_by(ConversationEvent.id)
        .all()
    )


# === 1. Caminho feliz =======================================================
print("1 - resposta valida")

JSON_VALIDO = (
    '{"intent": "quer pacote para o Atacama", '
    '"explicit_human_request": false, '
    '"extracted": {"nome": "Joao", "email": null, "destinos": [], '
    '"total_pessoas": null, "adultos": null, "criancas": null, '
    '"datas_definidas": null, "quantidade_dias_pretendida": null}, '
    '"duvidas": ["quantas pessoas vao?"]}'
)
interpretador._chamar_llm = lambda mensagem: JSON_VALIDO

cid1 = proxima_conversa()
resultado = interpretador.interpretar(
    db, mensagem="ola, quero viajar", commit=True, conversation_id=cid1, model_attempt=0,
)

check(isinstance(resultado, AIInterpretationResult), "1a. devolve AIInterpretationResult")
check(resultado.intent == "quer pacote para o Atacama", "1b. intent preservado intacto")
check(resultado.explicit_human_request is False, "1c. explicit_human_request preservado")
check(resultado.extracted.nome == "Joao", "1d. extracted.nome preservado")
check(resultado.duvidas == ["quantas pessoas vao?"], "1e. duvidas preservada intacta")

eventos1 = eventos_da_conversa(cid1)
tipos1 = [e.event_type for e in eventos1]
check(
    tipos1 == ["AI_INTERPRETATION_STARTED", "AI_INTERPRETATION_COMPLETED"],
    f"1f. STARTED e COMPLETED emitidos NESSA ordem, nada mais (got {tipos1})",
)
check(
    eventos1[0].model == "gemini-2.5-flash" and eventos1[1].model == "gemini-2.5-flash",
    "1g. os dois eventos registram o nome do modelo",
)
check(eventos1[1].result == "sucesso", f"1h. COMPLETED tem result=sucesso (got {eventos1[1].result})")
check(
    eventos1[1].duration_ms is not None and eventos1[1].duration_ms >= 0,
    f"1i. COMPLETED grava duration_ms >= 0 (got {eventos1[1].duration_ms})",
)
check(
    eventos1[0].model_attempt == 0 and eventos1[1].model_attempt == 0,
    "1j. model_attempt=0 (convencao 0-based desta fase) nos dois eventos",
)


# === 2. Campo extra nao declarado (extra="forbid") ==========================
print("\n2 - campo extra")

JSON_CAMPO_EXTRA = (
    '{"intent": null, "explicit_human_request": false, '
    '"extracted": {}, "duvidas": [], "pronto_para_humano": true}'
)
interpretador._chamar_llm = lambda mensagem: JSON_CAMPO_EXTRA

cid2 = proxima_conversa()
levantou = False
try:
    interpretador.interpretar(db, mensagem="x", commit=True, conversation_id=cid2)
except InterpretacaoFalhou as exc:
    levantou = True
    check(exc.error_code == "RESPOSTA_INVALIDA", f"2a. error_code correto (got {exc.error_code})")
check(levantou, "2b. InterpretacaoFalhou levantada - campo extra nunca vira sucesso parcial")

tipos2 = [e.event_type for e in eventos_da_conversa(cid2)]
check(tipos2 == ["AI_INTERPRETATION_STARTED", "AI_INTERPRETATION_FAILED"],
      f"2c. STARTED + FAILED, nunca COMPLETED (got {tipos2})")
check(eventos_da_conversa(cid2)[1].error_code == "RESPOSTA_INVALIDA", "2d. evento FAILED grava o error_code")
check(eventos_da_conversa(cid2)[1].result == "falha", "2e. evento FAILED tem result=falha")


# === 3. Campo obrigatorio faltando (explicit_human_request) =================
print("\n3 - campo obrigatorio faltando")

JSON_SEM_CAMPO = '{"intent": null, "extracted": {}, "duvidas": []}'
interpretador._chamar_llm = lambda mensagem: JSON_SEM_CAMPO

cid3 = proxima_conversa()
levantou = False
try:
    interpretador.interpretar(db, mensagem="x", commit=True, conversation_id=cid3)
except InterpretacaoFalhou as exc:
    levantou = True
    check(exc.error_code == "RESPOSTA_INVALIDA", f"3a. error_code correto (got {exc.error_code})")
check(levantou, "3b. InterpretacaoFalhou levantada - campo ausente nunca vira False presumido")

tipos3 = [e.event_type for e in eventos_da_conversa(cid3)]
check(tipos3 == ["AI_INTERPRETATION_STARTED", "AI_INTERPRETATION_FAILED"],
      f"3c. STARTED + FAILED, nunca COMPLETED (got {tipos3})")


# === 4. Tipo errado (intent como numero) ====================================
print("\n4 - tipo errado")

JSON_TIPO_ERRADO = '{"intent": 123, "explicit_human_request": false, "extracted": {}, "duvidas": []}'
interpretador._chamar_llm = lambda mensagem: JSON_TIPO_ERRADO

cid4 = proxima_conversa()
levantou = False
try:
    interpretador.interpretar(db, mensagem="x", commit=True, conversation_id=cid4)
except InterpretacaoFalhou as exc:
    levantou = True
    check(exc.error_code == "RESPOSTA_INVALIDA", f"4a. error_code correto (got {exc.error_code})")
check(levantou, "4b. InterpretacaoFalhou levantada - tipo errado nunca vira sucesso")

tipos4 = [e.event_type for e in eventos_da_conversa(cid4)]
check(tipos4 == ["AI_INTERPRETATION_STARTED", "AI_INTERPRETATION_FAILED"],
      f"4c. STARTED + FAILED, nunca COMPLETED (got {tipos4})")


# === 4b. JSON sintaticamente invalido (nem chega a ser objeto) ==============
print("\n4b - JSON sintaticamente invalido")

interpretador._chamar_llm = lambda mensagem: "isto nao e um JSON"

cid4b = proxima_conversa()
levantou = False
try:
    interpretador.interpretar(db, mensagem="x", commit=True, conversation_id=cid4b)
except InterpretacaoFalhou as exc:
    levantou = True
    check(exc.error_code == "RESPOSTA_INVALIDA", f"4b-a. error_code correto (got {exc.error_code})")
check(levantou, "4b-b. InterpretacaoFalhou levantada - JSON sintaticamente invalido")


# === 5. Falha na propria chamada ao modelo (rede/SDK) =======================
print("\n5 - falha na chamada ao modelo")


def _boom(mensagem):
    raise RuntimeError("simulado: timeout de rede")


interpretador._chamar_llm = _boom

cid5 = proxima_conversa()
levantou = False
try:
    interpretador.interpretar(db, mensagem="x", commit=True, conversation_id=cid5)
except InterpretacaoFalhou as exc:
    levantou = True
    check(exc.error_code == "CHAMADA_LLM_FALHOU", f"5a. error_code correto (got {exc.error_code})")
    check(
        exc.__cause__ is not None and "timeout" in str(exc.__cause__),
        "5b. causa original preservada via 'raise ... from exc'",
    )
check(levantou, "5c. InterpretacaoFalhou levantada - falha de rede/SDK")

tipos5 = [e.event_type for e in eventos_da_conversa(cid5)]
check(tipos5 == ["AI_INTERPRETATION_STARTED", "AI_INTERPRETATION_FAILED"],
      f"5d. STARTED + FAILED, nunca COMPLETED (got {tipos5})")
check(
    eventos_da_conversa(cid5)[1].error_code == "CHAMADA_LLM_FALHOU",
    "5e. evento FAILED distingue CHAMADA_LLM_FALHOU de RESPOSTA_INVALIDA",
)


# === 6. R-INTENT - este arquivo nunca acessa intent/duvidas =================
# tests/test_v2_r_intent.py ja varre TODO o repo (inclusive este arquivo, por
# estar sob conversas/app/v2/**/*.py) mas so cobre `intent`. Aqui provamos,
# por AST (nao grep textual ingenuo, que confundiria comentario/docstring com
# codigo), a ausencia de acesso ao CONTEUDO dos dois campos de texto livre -
# `intent` E `duvidas` (contratos.py: "R-INTENT VALE PARA duvidas TAMBEM").
print("\n6 - R-INTENT: interpretador.py nunca acessa intent/duvidas")

CAMPOS_PROIBIDOS = ("intent", "duvidas")


def _eh_constante(no, valor):
    return isinstance(no, ast.Constant) and no.value == valor


def _acessa_campo(no, campo):
    """Os mesmos 4 padroes minimos de tests/test_v2_r_intent.py - `.attr`,
    `["chave"]`, `.get("chave")`, `getattr(x, "chave")` - aplicados a UM
    campo por vez. Deliberadamente mais amplo que "so em posicao decisoria":
    aqui a alegacao e mais forte (ZERO acesso no arquivo inteiro, nem para
    transporte/log), entao nao ha necessidade de restringir a posicao."""
    if isinstance(no, ast.Attribute) and no.attr == campo:
        return True
    if isinstance(no, ast.Subscript) and _eh_constante(no.slice, campo):
        return True
    if (
        isinstance(no, ast.Call)
        and isinstance(no.func, ast.Attribute)
        and no.func.attr == "get"
        and no.args
        and _eh_constante(no.args[0], campo)
    ):
        return True
    if (
        isinstance(no, ast.Call)
        and isinstance(no.func, ast.Name)
        and no.func.id == "getattr"
        and len(no.args) >= 2
        and _eh_constante(no.args[1], campo)
    ):
        return True
    return False


_ARQUIVO_INTERPRETADOR = CONVERSAS_DIR / "app" / "v2" / "interpretador.py"
_ARVORE = ast.parse(
    _ARQUIVO_INTERPRETADOR.read_text(encoding="utf-8"), filename=str(_ARQUIVO_INTERPRETADOR)
)

for campo in CAMPOS_PROIBIDOS:
    ocorrencias = [no for no in ast.walk(_ARVORE) if _acessa_campo(no, campo)]
    linhas = [getattr(no, "lineno", "?") for no in ocorrencias]
    check(
        ocorrencias == [],
        f"6-{campo}. interpretador.py NUNCA acessa .{campo} / ['{campo}'] / "
        f".get('{campo}') / getattr(x,'{campo}') (achados nas linhas {linhas})",
    )

# Prova que o scanner acima FUNCIONA de verdade (nao e um no-op que sempre
# devolve vazio) - roda contra um snippet sintetico que acessa `intent`, no
# mesmo espirito das provas positivas (grupo P) de tests/test_v2_r_intent.py.
_SNIPPET_POSITIVO = "def f(r):\n    return r.intent\n"
_ARVORE_POSITIVA = ast.parse(_SNIPPET_POSITIVO)
_achados_positivos = [no for no in ast.walk(_ARVORE_POSITIVA) if _acessa_campo(no, "intent")]
check(
    len(_achados_positivos) == 1,
    f"6-controle. o scanner ACUSA um acesso .intent real, prova que nao e no-op "
    f"(achados={len(_achados_positivos)})",
)


# === 7. _chamar_llm chamada DIRETAMENTE - os dois guard clauses reais ======
# Achado de revisao (python-reviewer): as secoes 1-6 acima substituem
# interpretador._chamar_llm INTEIRA (reatribuicao de atributo do modulo)
# antes de chamar interpretar() - o CORPO REAL de _chamar_llm (linhas
# 181-217 de interpretador.py, com os dois guard clauses: GEMINI_API_KEY
# ausente na linha ~198-201, e resposta vazia/bloqueada do Gemini na linha
# ~215-216) nunca roda sob teste. Esta secao fecha a lacuna chamando
# _CHAMAR_LLM_REAL (capturada logo apos o import, ver acima) DIRETAMENTE -
# nunca via interpretar() - no mesmo espirito do grupo F de
# tests/test_v2_comunicador.py (chamada direta a _chamar_gemini com
# GEMINI_API_KEY mockado).
print("\n7 - _chamar_llm chamada direta: guard clauses reais")


class _RespostaGeminiFalsa:
    """Sosia minimo de google.genai.types.GenerateContentResponse - so o
    atributo que _chamar_llm realmente le (.text)."""

    def __init__(self, texto):
        self.text = texto


class _ModelsFalso:
    def __init__(self, texto):
        self._texto = texto

    def generate_content(self, *, model, contents, config):
        return _RespostaGeminiFalsa(self._texto)


class _ClienteGeminiFalso:
    """Sosia minimo de genai.Client - so o suficiente para _chamar_llm
    completar cliente.models.generate_content(...) sem tocar rede."""

    def __init__(self, texto):
        self.models = _ModelsFalso(texto)


# --- 7a. GEMINI_API_KEY vazia -> RuntimeError (guard da linha ~198-201) ---
with mock.patch("app.v2.interpretador.GEMINI_API_KEY", ""):
    erro_7a = None
    try:
        _CHAMAR_LLM_REAL("mensagem qualquer")
    except RuntimeError as exc:
        erro_7a = exc
check(erro_7a is not None, "7a. _chamar_llm sem GEMINI_API_KEY levanta RuntimeError")
check(
    erro_7a is not None and "GEMINI_API_KEY" in str(erro_7a),
    f"7a2. mensagem do erro cita GEMINI_API_KEY (obtido={str(erro_7a)!r})",
)

# --- 7b. GEMINI_API_KEY None (o guard usa `if not X`, cobre os dois valores
# falsy - "ausente" e "vazia" nao podem ser distinguidos por esse guard, e
# este teste prova que nenhum dos dois escapa). ---
with mock.patch("app.v2.interpretador.GEMINI_API_KEY", None):
    erro_7b = None
    try:
        _CHAMAR_LLM_REAL("mensagem qualquer")
    except RuntimeError as exc:
        erro_7b = exc
check(erro_7b is not None, "7b. _chamar_llm com GEMINI_API_KEY=None tambem levanta RuntimeError")

# --- 7c. resposta do Gemini sem texto, .text is None (guard da linha
# ~215-216). Mocka SO genai.Client (o cliente do SDK) - NUNCA _chamar_llm
# inteira, senao esta secao reproduziria a mesma lacuna que existe pra
# fechar. GEMINI_API_KEY precisa ser truthy aqui para passar do guard 7a e
# alcancar este segundo guard. ---
with mock.patch("app.v2.interpretador.GEMINI_API_KEY", "fake-key-de-teste"):
    with mock.patch(
        "app.v2.interpretador.genai.Client", side_effect=lambda api_key, http_options: _ClienteGeminiFalso(None)
    ) as mock_cliente_7c:
        erro_7c = None
        try:
            _CHAMAR_LLM_REAL("mensagem qualquer")
        except RuntimeError as exc:
            erro_7c = exc
check(erro_7c is not None, "7c. resposta.text is None -> RuntimeError")
check(
    erro_7c is not None and "Resposta vazia" in str(erro_7c),
    f"7c2. mensagem e a do guard de resposta vazia (obtido={str(erro_7c)!r})",
)
check(mock_cliente_7c.call_count == 1, "7c3. genai.Client foi instanciado - prova que passou do guard 7a")

# --- 7d. resposta com .text == "" (string vazia, nao None) - `if not
# resposta.text` cobre os dois valores falsy, mesma logica do par 7a/7b. ---
with mock.patch("app.v2.interpretador.GEMINI_API_KEY", "fake-key-de-teste"):
    with mock.patch(
        "app.v2.interpretador.genai.Client", side_effect=lambda api_key, http_options: _ClienteGeminiFalso("")
    ):
        erro_7d = None
        try:
            _CHAMAR_LLM_REAL("mensagem qualquer")
        except RuntimeError as exc:
            erro_7d = exc
check(erro_7d is not None, "7d. resposta.text == '' (string vazia) tambem levanta RuntimeError")

# --- 7e. controle positivo: resposta.text presente -> _CHAMAR_LLM_REAL
# devolve o texto intacto. Sem este controle, 7c/7d passariam mesmo se
# _chamar_llm levantasse RuntimeError incondicionalmente - mesmo papel do
# "6-controle" acima para o scanner AST. ---
with mock.patch("app.v2.interpretador.GEMINI_API_KEY", "fake-key-de-teste"):
    with mock.patch(
        "app.v2.interpretador.genai.Client",
        side_effect=lambda api_key, http_options: _ClienteGeminiFalso('{"ok": true}'),
    ):
        resultado_7e = _CHAMAR_LLM_REAL("mensagem qualquer")
check(
    resultado_7e == '{"ok": true}',
    f"7e. controle positivo - resposta.text presente e devolvido intacto (obtido={resultado_7e!r})",
)

# --- 7f. TETO DE ESPERA na chamada ao SDK. Sem `http_options.timeout` o
# google-genai NAO impoe teto nenhum: a chamada fica pendurada para sempre e,
# junto com ela, a rota HTTP que a invocou (o caminho vivo e a rota SOMBRA).
# O valor e conferido EM MILISSEGUNDOS - `types.HttpOptions.timeout` e
# documentado como "Timeout for the request in milliseconds" no SDK instalado,
# entao um valor em segundos aqui viraria um teto de 90ms e reprovaria toda
# chamada real. Este check trava as duas coisas: que existe teto e que a
# ORDEM DE GRANDEZA e de milissegundos. ---
with mock.patch("app.v2.interpretador.GEMINI_API_KEY", "fake-key-de-teste"):
    with mock.patch(
        "app.v2.interpretador.genai.Client",
        side_effect=lambda api_key, http_options: _ClienteGeminiFalso('{"ok": true}'),
    ) as mock_cliente_7f:
        _CHAMAR_LLM_REAL("mensagem qualquer")

_http_options_7f = mock_cliente_7f.call_args.kwargs.get("http_options")
check(
    _http_options_7f is not None and _http_options_7f.timeout is not None,
    f"7f. genai.Client recebe http_options com timeout (obtido={_http_options_7f!r})",
)
check(
    _http_options_7f is not None and _http_options_7f.timeout == interpretador._TIMEOUT_GEMINI_MS,
    f"7f2. o timeout passado e `_TIMEOUT_GEMINI_MS` "
    f"(obtido={getattr(_http_options_7f, 'timeout', None)!r}, "
    f"esperado={interpretador._TIMEOUT_GEMINI_MS!r})",
)
check(
    10_000 <= interpretador._TIMEOUT_GEMINI_MS <= 600_000,
    f"7f3. o valor esta na faixa de MILISSEGUNDOS plausivel (10s-600s) - pega "
    f"a troca acidental de unidade para segundos (obtido="
    f"{interpretador._TIMEOUT_GEMINI_MS!r})",
)


# ─── Resultado ───────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FALHA(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("TODOS OS CHECKS PASSARAM")
