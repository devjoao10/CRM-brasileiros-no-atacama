"""
BIA-V2 Fase 0 / Task 0.2 - contrato de validacao de conversation_events.

Vocabulario, limites, regexes, excecoes tipadas e TODOS os validadores de
`conversas/app/v2/eventos.py:registrar_evento()`. Validacao PURA - nao toca
banco/sessao, chamada ANTES de qualquer `db.add()`.

Direcao de dependencia: `eventos.py` (persistencia) importa DESTE modulo,
nunca o contrario. `TipoEvento` mora aqui (nao em `eventos.py`) por isso:
`validar_tipo` precisa coagir string para `TipoEvento`, e se o enum ficasse
do outro lado este modulo teria que importar DE la so pra validar `tipo` -
a dependencia invertida que a regra acima proibe.

`payload` e ALLOWLIST DE CHAVES TIPADA (2 chaves: bool ou StrEnum), nao
filtro heuristico: nao existe regex confiavel para "isto e sensivel" - erra
deixando passar formato imprevisto E mascarando dado legitimo (`lead_id`
grande "parece" telefone) ao mesmo tempo. Defesa ESTRUTURAL: nao ha "valor
livre" pra escapar de um vocabulario fechado.

`state_before/after`, `action`, `error_code`, `model` sao validados por
FORMATO+TAMANHO - contrato SINTATICO, nao controle de PII (um JWT bate no
formato de `model` sem esforco; a defesa real contra PII e o vocabulario
fechado do payload). `event_id` foge deste regime: validado E CANONICALIZADO
como UUID de verdade - ver `validar_event_id`.

`result` e opcional - nem todo `TipoEvento` e OPERACAO com desfecho
(`MESSAGE_RECEIVED` e fato). 3 valores bastam pros 18 tipos: o motivo
especifico de falha vai em `error_code`, nunca numa variante nova de result.

CHAVES REMOVIDAS: `motivo`/`intent` (prosa livre sem contrato), `tentativa`
(duplicava `model_attempt`), e agora `prefiltro_motivo`/`campos_faltantes`
(com o enum `MotivoPrefiltro` e o frozenset `CAMPOS_TRIAGEM`) - vocabulario
de Fase 2/3 vazando pra Fase 0; grep confirmou zero import real fora deste
modulo e do seu teste. Dominio futuro nao deveria importar sua propria
linguagem de volta da auditoria - voltam via DTO/builder tipado quando as
Fases 2/3 definirem seus contratos.

`explicit_human_request` TAMBEM SAIU. O argumento de que `bool` primitivo,
sem enum nem import, nao criaria dependencia invertida cobria apenas o
acoplamento de IMPORT - nao o SEMANTICO. O fato "o cliente pediu humano
explicitamente" pertence ao dominio futuro de interpretacao/handoff, nao a
infraestrutura neutra de eventos. Volta na fase que define o fato.

Sobra UMA chave: `origem`, que e vocabulario da propria infraestrutura de
eventos (de onde o evento veio), nao de dominio nenhum.
"""
import json
import re
import uuid
from enum import StrEnum


class TipoEvento(StrEnum):
    """Os 18 tipos de evento exigidos pelo WP (Fase 0 / Task 0.2)."""

    MESSAGE_RECEIVED = "MESSAGE_RECEIVED"
    MESSAGE_IGNORED = "MESSAGE_IGNORED"
    AI_INTERPRETATION_STARTED = "AI_INTERPRETATION_STARTED"
    AI_INTERPRETATION_COMPLETED = "AI_INTERPRETATION_COMPLETED"
    AI_INTERPRETATION_FAILED = "AI_INTERPRETATION_FAILED"
    TRIAGE_DATA_UPDATED = "TRIAGE_DATA_UPDATED"
    TRIAGE_COMPLETED = "TRIAGE_COMPLETED"
    HANDOFF_REQUESTED = "HANDOFF_REQUESTED"
    HUMAN_SELECTED = "HUMAN_SELECTED"
    HANDOFF_COMPLETED = "HANDOFF_COMPLETED"
    HANDOFF_FAILED = "HANDOFF_FAILED"
    HUMAN_FIRST_RESPONSE = "HUMAN_FIRST_RESPONSE"
    AI_RESPONSE_GENERATED = "AI_RESPONSE_GENERATED"
    AI_RESPONSE_DISCARDED = "AI_RESPONSE_DISCARDED"
    MESSAGE_SENT = "MESSAGE_SENT"
    MESSAGE_SEND_FAILED = "MESSAGE_SEND_FAILED"
    CONVERSATION_CLOSED = "CONVERSATION_CLOSED"
    CONVERSATION_REOPENED = "CONVERSATION_REOPENED"


class ResultadoEvento(StrEnum):
    """Desfecho de um evento que representa uma OPERACAO - ver docstring do modulo."""

    SUCESSO = "sucesso"
    FALHA = "falha"
    IGNORADO = "ignorado"


class OrigemEvento(StrEnum):
    """De onde o evento foi disparado."""

    WEBHOOK = "webhook"
    REPLAY = "replay"
    SINTETICO = "sintetico"
    MANUAL = "manual"


class PayloadInvalido(ValueError):
    """
    `payload` fora da allowlist de chaves ou fora do formato aceito. `chave`
    carrega o nome da chave problematica (None quando o problema e do
    payload inteiro). A mensagem (`str(exc)`) NUNCA carrega o VALOR recebido
    - so o nome do campo/chave e a natureza da violacao.
    """

    def __init__(self, mensagem: str, *, chave: str | None = None):
        self.chave = chave
        super().__init__(mensagem)


class CampoInvalido(ValueError):
    """
    Campo fora do payload (event_id, event_type, whatsapp_msg_id, state_*,
    action, model, result, error_code, tipo, campos inteiros) violou
    tamanho/intervalo/formato do contrato - SINTATICO, nao controle de PII
    (ver docstring do modulo). `campo` carrega o nome do parametro; a
    mensagem NUNCA inclui o valor recebido - se um caller inverter
    argumentos, o texto do cliente nao deve ir parar num log ou resposta.
    """

    def __init__(self, mensagem: str, *, campo: str):
        self.campo = campo
        super().__init__(mensagem)


# ---------------------------------------------------------------------------
# Limites e formatos - alinhados aos declarados em conversas/app/models/evento.py.
# ---------------------------------------------------------------------------
_LIMITE_EVENT_ID = 36
_LIMITE_EVENT_TYPE = 48
_LIMITE_WHATSAPP_MSG_ID = 100
_LIMITE_STATE = 32
_LIMITE_ACTION = 64
_LIMITE_MODEL = 64
_LIMITE_RESULT = 32
_LIMITE_ERROR_CODE = 64

# int4 do PostgreSQL - as colunas inteiras desta tabela (conversation_id,
# lead_id, message_id, target_user_id, model_attempt, duration_ms) sao
# Integer, nao BigInteger. SQLite aceita qualquer largura; sem esta checagem
# em Python, um valor fora do intervalo so falharia tarde, como DataError do
# PostgreSQL (nao IntegrityError - escaparia do except de compensacao).
_INT4_MIN = -2_147_483_648
_INT4_MAX = 2_147_483_647

# state_before/state_after: token tecnico SEM vocabulario fixo - a Fase 4
# (maquina de estados) ainda nao existe neste WP; nao importar nem depender
# dela. So formato e tamanho.
_REGEX_ESTADO = re.compile(r"^[A-Z][A-Z0-9_]*$")
_REGEX_ACTION = re.compile(r"^[a-z][a-z0-9_]*$")
_REGEX_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_REGEX_MODEL_PROIBIDO = re.compile(r"[\s\x00-\x1f\x7f]")
# whatsapp_msg_id: sem vocabulario/regex de formato (nao ha contrato ou
# evidencia documentada do formato real de WAMID da Meta para justificar um
# regex mais estrito) - so tamanho e ausencia de NUL/caractere de controle,
# incompativeis com persistencia.
_REGEX_CONTROLE = re.compile(r"[\x00-\x1f\x7f]")


def _validar_tamanho(valor: object, limite: int, campo: str) -> None:
    if not isinstance(valor, str):
        raise CampoInvalido(f"{campo} deve ser string, recebido {type(valor).__name__}", campo=campo)
    if len(valor) > limite:
        raise CampoInvalido(f"{campo} excede {limite} caracteres (tem {len(valor)})", campo=campo)


def _validar_token(valor: str, regex: re.Pattern, limite: int, campo: str) -> None:
    _validar_tamanho(valor, limite, campo)
    if not regex.match(valor):
        raise CampoInvalido(f"{campo} nao bate com o formato exigido ({regex.pattern})", campo=campo)


def validar_model(valor: str) -> None:
    _validar_tamanho(valor, _LIMITE_MODEL, "model")
    if _REGEX_MODEL_PROIBIDO.search(valor):
        raise CampoInvalido("model nao pode conter espaco ou caractere de controle", campo="model")


def validar_whatsapp_msg_id(valor: str) -> None:
    """
    Tamanho maximo (equivalencia com `Message.whatsapp_msg_id` ja confirmada)
    + rejeicao de NUL/caractere de controle. NAO valida formato de WAMID -
    sem contrato ou evidencia documentada do formato real da Meta.
    """
    _validar_tamanho(valor, _LIMITE_WHATSAPP_MSG_ID, "whatsapp_msg_id")
    if _REGEX_CONTROLE.search(valor):
        raise CampoInvalido(
            "whatsapp_msg_id nao pode conter NUL ou caractere de controle", campo="whatsapp_msg_id"
        )


def validar_event_id(valor: str) -> str:
    """
    UUID de verdade (RFC 4122) via `uuid.UUID(...)` - nao "qualquer string
    de ate 36 caracteres". Devolve a forma CANONICA (minuscula, com hifens):
    e essa forma que deve ser PERSISTIDA e usada na RE-CONSULTA de
    compensacao (`_persistir_ou_compensar`) - sem canonicalizar,
    `"A0EEBC99-..."` (maiusculo) e a mesma string sem hifen validam como
    UUIDs iguais-no-valor mas diferentes-na-string e viram DUAS linhas: o
    UNIQUE index e a re-consulta comparam string, a colisao real escapa.
    """
    _validar_tamanho(valor, _LIMITE_EVENT_ID, "event_id")
    try:
        parsed = uuid.UUID(valor)
    except ValueError as exc:
        raise CampoInvalido("event_id deve ser um UUID valido (RFC 4122)", campo="event_id") from exc
    return str(parsed)


def _coagir_enum(valor: object, enum_cls: type[StrEnum], nome_campo: str) -> StrEnum:
    """
    Aceita um membro de `enum_cls` ou a string exata de um dos seus valores;
    qualquer outra coisa levanta `ValueError`. Helper unico substitui a
    checagem duplicada em 3 pontos (`tipo`, `result`, enum de payload) - cada
    chamador decide a excecao tipada. NUNCA inclui o valor recebido na
    mensagem, so o nome do campo e o tipo Python (nao ecoar conteudo).
    """
    if isinstance(valor, enum_cls):
        return valor
    if isinstance(valor, str):
        try:
            return enum_cls(valor)
        except ValueError:
            pass
    raise ValueError(f"{nome_campo} fora do vocabulario permitido (tipo recebido: {type(valor).__name__})")


def validar_tipo(tipo: "TipoEvento | str") -> TipoEvento:
    """
    Aceita `TipoEvento` ou a string exata de um dos 18 valores; qualquer
    outra coisa levanta `CampoInvalido` - nunca `AttributeError` nem
    `ValueError` cru vazando do enum. Tambem valida o tamanho de
    `event_type` (redundante para um enum fechado, mas mantido como rede
    de seguranca alinhada a largura da coluna).
    """
    try:
        tipo_validado = _coagir_enum(tipo, TipoEvento, "tipo")
    except ValueError as exc:
        raise CampoInvalido("tipo de evento invalido", campo="tipo") from exc
    _validar_tamanho(tipo_validado.value, _LIMITE_EVENT_TYPE, "event_type")
    return tipo_validado


def validar_resultado(valor: object) -> str | None:
    if valor is None:
        return None
    try:
        resultado = _coagir_enum(valor, ResultadoEvento, "result").value
    except ValueError as exc:
        raise CampoInvalido("result fora do vocabulario permitido", campo="result") from exc
    _validar_tamanho(resultado, _LIMITE_RESULT, "result")
    return resultado


def validar_state(valor: str, campo: str) -> None:
    """state_before/state_after - mesmo formato, nome de campo varia."""
    _validar_token(valor, _REGEX_ESTADO, _LIMITE_STATE, campo)


def validar_action(valor: str) -> None:
    _validar_token(valor, _REGEX_ACTION, _LIMITE_ACTION, "action")


def validar_error_code(valor: str) -> None:
    _validar_token(valor, _REGEX_ERROR_CODE, _LIMITE_ERROR_CODE, "error_code")


def validar_inteiro(valor: object, campo: str, *, minimo: int = _INT4_MIN, maximo: int = _INT4_MAX) -> None:
    """
    Rejeita `bool` EXPLICITAMENTE primeiro - `bool` e subclasse de `int` em
    Python (`isinstance(True, int)` e `True`), entao a checagem de tipo
    sozinha deixaria `True`/`False` passar como 1/0 silenciosamente. Depois
    rejeita qualquer coisa que nao seja `int`. Por fim aplica o intervalo:
    `maximo` e sempre o teto int4 do PostgreSQL; `minimo` fecha o piso
    especifico do campo (1 para IDs autoincrement, 0 para contador/duracao).
    """
    if isinstance(valor, bool):
        raise CampoInvalido(f"{campo} nao aceita bool", campo=campo)
    if not isinstance(valor, int):
        raise CampoInvalido(f"{campo} deve ser int, recebido {type(valor).__name__}", campo=campo)
    if valor < minimo or valor > maximo:
        raise CampoInvalido(f"{campo} deve estar entre {minimo} e {maximo}", campo=campo)


# ---------------------------------------------------------------------------
# Payload - allowlist tipada. Ver docstring do modulo.
# ---------------------------------------------------------------------------
CHAVES_PAYLOAD_PERMITIDAS: frozenset[str] = frozenset({
    "origem",
})

_TAMANHO_MAX_PAYLOAD_JSON = 2000


def _validar_enum_payload(valor: object, enum_cls: type[StrEnum], chave: str) -> None:
    try:
        _coagir_enum(valor, enum_cls, chave)
    except ValueError as exc:
        raise PayloadInvalido(f"{chave} fora do vocabulario permitido", chave=chave) from exc


_VALIDADORES_PAYLOAD = {
    "origem": lambda v: _validar_enum_payload(v, OrigemEvento, "origem"),
}


def validar_payload(payload: dict | None) -> None:
    """
    Levanta `PayloadInvalido` nomeando a chave/motivo da rejeicao. Roda ANTES
    de qualquer `db.add()`. O corpo roda num try/except que converte QUALQUER
    `ValueError`/`TypeError`/`RecursionError` inesperado em `PayloadInvalido`.
    `RecursionError` entra porque herda de `RuntimeError`, nao cairia num
    `except (ValueError, TypeError)` - deixaria estrutura aninhada demais
    escapar crua. Na pratica `_validar_payload_conteudo` ja rejeita isso PELO
    TIPO antes de serializar (ver la); este except e rede de seguranca.
    """
    if payload is None:
        return
    try:
        _validar_payload_conteudo(payload)
    except PayloadInvalido:
        raise
    except (ValueError, TypeError, RecursionError) as exc:
        raise PayloadInvalido(f"payload invalido: {type(exc).__name__}", chave=None) from exc


def _validar_payload_conteudo(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise PayloadInvalido(f"payload deve ser dict, recebido {type(payload).__name__}", chave=None)

    # Validacao TIPADA por chave PRIMEIRO, antes de qualquer tentativa de
    # serializar. Um valor mal formado (ex.: lista aninhada demais o
    # suficiente para estourar RecursionError dentro de json.dumps) e
    # rejeitado aqui pelo TIPO - nunca chega a ser serializado.
    for chave, valor in payload.items():
        if chave not in CHAVES_PAYLOAD_PERMITIDAS:
            raise PayloadInvalido("chave de payload fora da allowlist", chave=str(chave))
        _VALIDADORES_PAYLOAD[chave](valor)

    # Rede de seguranca de tamanho/serializacao POR ULTIMO: a esta altura
    # todo valor ja passou por um validador tipado (bool ou StrEnum/string
    # curta do vocabulario fechado), entao json.dumps nao deveria conseguir
    # estourar aqui. O guarda continua amplo (ver validar_payload) para o
    # dia em que a allowlist crescer com um tipo novo.
    serializado = json.dumps(payload)
    if len(serializado) > _TAMANHO_MAX_PAYLOAD_JSON:
        raise PayloadInvalido(f"payload serializado excede {_TAMANHO_MAX_PAYLOAD_JSON} caracteres", chave=None)
