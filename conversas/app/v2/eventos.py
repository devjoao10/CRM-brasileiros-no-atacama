"""
BIA-V2 Fase 0 / Task 0.2 — `TipoEvento` e `registrar_evento()`.

Objetivo do WP: conseguir reconstruir "o que aconteceu com esta conversa?"
sem abrir workflow nenhum (ver docs/superpowers/plans/2026-08-29-bia-v2.md).

POR QUE `payload` E ALLOWLIST DE CHAVES TIPADA, E NAO UM FILTRO HEURISTICO
---------------------------------------------------------------------------
Nao existe regex confiavel para "isto e sensivel". Um filtro heuristico
(bloquear string que "parece" telefone, e-mail ou nome) erra nas DUAS
direcoes ao mesmo tempo:

  - deixa passar formato imprevisto — um numero sem o `+55`, um e-mail sem
    `@` reconhecivel, um campo novo que ninguem cobriu ainda;
  - E mascara dado legitimo — um `lead_id` numerico grande confundido com
    telefone, por exemplo — destruindo o proprio valor diagnostico que esta
    tabela existe para criar.

A defesa aqui e ESTRUTURAL, nao heuristica: `CHAVES_PAYLOAD_PERMITIDAS` tem
4 chaves, cada uma com validacao de TIPO (bool, lista de um vocabulario
fixo, ou StrEnum) — nenhuma aceita string livre. Nao ha regex para escapar
de uma chave que nao existe, nem "valor livre" para escapar de um tipo que
so aceita um vocabulario fechado.

CONTRATO DE TOKEN E SINTATICO — NAO E CONTROLE DE PII
--------------------------------------------------------
`state_before`, `state_after`, `action`, `error_code` e `model` sao
validados contra FORMATO (regex de token tecnico) e TAMANHO MAXIMO — nunca
contra CONTEUDO. E uma garantia de FORMA, nao de SEGURANCA. Exemplos que
batem no formato e NAO sao barrados por ele:

  - `state_before`/`state_after` aceitam `^[A-Z][A-Z0-9_]*$` ate 32 chars.
    Nao ha vocabulario fixo aqui de proposito: a maquina de estados da
    Fase 4 ainda nao existe neste WP, e este modulo NAO importa nem depende
    dela — o formato e puramente sintatico ate a Fase 4 definir os valores
    reais.
  - `model` so exige "sem espaco, sem caractere de controle, ate 64 chars"
    — um token JWT como `eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIx.abc` BATE nesse
    formato sem esforco nenhum.
  - um telefone como `5551999999999` nao bate em `state_before`/`state_after`
    (comeca com digito, o regex exige letra maiuscula) nem em `action`
    (exige minuscula) — mas isso e ACIDENTE de formato, nao defesa
    deliberada contra PII. Um valor como `A5551999999999` bateria sem
    problema nenhum no regex de `state_before`.
  - um e-mail como `cliente@gmail.com` tambem nao bate (tem `@`), de novo
    por acidente de forma, nao por deteccao de conteudo.

A defesa real contra PII continua sendo o VOCABULARIO FECHADO: os campos de
token tecnico sao escritos pelo MOTOR/PIPELINE (Fase 4+), nunca pelo
webhook nem pela IA a partir de texto do cliente; `payload` so aceita as 4
chaves tipadas abaixo. Nenhum dos dois mecanismos e "filtro de conteudo" —
sao "quem pode escrever aqui" (processo/contrato de chamada) e "que FORMA a
string tem" (sintaxe), nunca "o que a string significa".

`event_id` NAO segue este regime de token tecnico: e validado como UUID de
verdade (`uuid.UUID(...)`), nao como "qualquer string de ate 36
caracteres" — ver `_validar_event_id`.

RESULT: POR QUE 3 VALORES (sucesso/falha/ignorado) BASTAM PARA OS 18 TIPOS
------------------------------------------------------------------------------
`result` e OPCIONAL (nullable) — nem todo `TipoEvento` representa uma
OPERACAO com desfecho, e este modulo nunca forca um valor onde nao ha
desfecho a expressar. Conferencia dos 18 tipos:

  - SEM desfecho, `result` fica None: `MESSAGE_RECEIVED`,
    `AI_INTERPRETATION_STARTED`, `TRIAGE_DATA_UPDATED`, `HANDOFF_REQUESTED`,
    `HUMAN_FIRST_RESPONSE`, `CONVERSATION_CLOSED`, `CONVERSATION_REOPENED`
    — sao FATO/transicao, nao operacao com sucesso/falha.
  - `sucesso`: `AI_INTERPRETATION_COMPLETED`, `TRIAGE_COMPLETED`,
    `HUMAN_SELECTED`, `HANDOFF_COMPLETED`, `AI_RESPONSE_GENERATED`,
    `MESSAGE_SENT`.
  - `falha`: `AI_INTERPRETATION_FAILED`, `HANDOFF_FAILED`,
    `MESSAGE_SEND_FAILED`. O MOTIVO especifico (timeout, sem atendente
    elegivel, etc.) vai em `error_code`, nao numa variante nova de `result`
    — R5 no plano ja usa exatamente esse par: `HANDOFF_FAILED` +
    `error_code='SEM_ATENDENTE_ELEGIVEL'`.
  - `ignorado`: `MESSAGE_IGNORED`, `AI_RESPONSE_DISCARDED` (Fase 7 descarta
    a resposta por decisao do gate de autorizacao — nao e erro, e escolha
    deliberada de nao usar um resultado que ficou desatualizado).

Nenhum dos 18 precisa de um quarto valor (`parcial`, `expirado`, ...): uma
falha por timeout continua sendo `falha`, com o motivo em `error_code`.

CHAVES REMOVIDAS DO PAYLOAD (e por que)
------------------------------------------
- `motivo` — sem dominio definido; era o vetor de prosa livre que este
  modulo existe para fechar.
- `intent` — texto livre da IA nesta fase; sem contrato seguro ate a Fase 8
  (Interpretadora) definir uma saida tipada.
- `tentativa` — duplicava `model_attempt`, que ja e coluna propria do
  contrato. Um mesmo conceito nao pode viver em dois lugares — quem le nao
  saberia qual confiar.
"""
import json
import re
import uuid
from enum import StrEnum

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.evento import ConversationEvent


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
    """Desfecho de um evento que representa uma OPERACAO — ver docstring do modulo."""

    SUCESSO = "sucesso"
    FALHA = "falha"
    IGNORADO = "ignorado"


class MotivoPrefiltro(StrEnum):
    """As 4 regras absolutas do pre-filtro (Fase 3 do plano)."""

    REACAO_ISOLADA = "reacao_isolada"
    STICKER_ISOLADO = "sticker_isolado"
    EMOJI_ISOLADO = "emoji_isolado"
    MENSAGEM_VAZIA = "mensagem_vazia"


class OrigemEvento(StrEnum):
    """De onde o evento foi disparado."""

    WEBHOOK = "webhook"
    REPLAY = "replay"
    SINTETICO = "sintetico"
    MANUAL = "manual"


class EventoDuplicado(Exception):
    """
    `event_id` ja existe. NUNCA devolve a linha existente em silencio — isso
    mascararia o caso em que dois eventos DIFERENTES colidem no mesmo id.
    Quem quer idempotencia (ex.: Fase 6, handoff) captura esta excecao de
    proposito e decide o que fazer.
    """

    def __init__(self, event_id: str):
        self.event_id = event_id
        super().__init__(f"event_id duplicado: {event_id!r}")


class PayloadInvalido(ValueError):
    """
    `payload` fora da allowlist de chaves ou fora do formato aceito.

    `chave` carrega o nome da chave problematica quando aplicavel (None
    quando o problema e do payload inteiro — tipo errado ou nao
    serializavel) — mesmo padrao de `EventoDuplicado.event_id`: o caller nao
    precisa fazer parsing da mensagem para decidir o que fazer.
    """

    def __init__(self, mensagem: str, *, chave: str | None = None):
        self.chave = chave
        super().__init__(mensagem)


class CampoInvalido(ValueError):
    """
    Campo fora do `payload` (event_id, event_type, whatsapp_msg_id,
    state_before/after, action, model, result, error_code, tipo) violou o
    tamanho maximo ou o formato exigido pelo contrato — ver docstring do
    modulo sobre o contrato ser SINTATICO, nao controle de PII.

    `campo` carrega o nome do parametro problematico, mesmo padrao de
    `PayloadInvalido.chave`.
    """

    def __init__(self, mensagem: str, *, campo: str):
        self.campo = campo
        super().__init__(mensagem)


# ---------------------------------------------------------------------------
# Validacao de campos fora do payload — tamanho E formato, sempre ANTES de
# qualquer db.add(). Limites alinhados aos declarados em
# conversas/app/models/evento.py.
# ---------------------------------------------------------------------------
_LIMITE_EVENT_ID = 36
_LIMITE_EVENT_TYPE = 48
_LIMITE_WHATSAPP_MSG_ID = 100
_LIMITE_STATE = 32
_LIMITE_ACTION = 64
_LIMITE_MODEL = 64
_LIMITE_RESULT = 32
_LIMITE_ERROR_CODE = 64

# state_before/state_after: token tecnico SEM vocabulario fixo — a Fase 4
# (maquina de estados) ainda nao existe neste WP; nao importar nem depender
# dela. So formato e tamanho.
_REGEX_ESTADO = re.compile(r"^[A-Z][A-Z0-9_]*$")
_REGEX_ACTION = re.compile(r"^[a-z][a-z0-9_]*$")
_REGEX_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_REGEX_MODEL_PROIBIDO = re.compile(r"[\s\x00-\x1f\x7f]")


def _validar_tamanho(valor: object, limite: int, campo: str) -> None:
    if not isinstance(valor, str):
        raise CampoInvalido(f"{campo} deve ser string, recebido {type(valor).__name__}", campo=campo)
    if len(valor) > limite:
        raise CampoInvalido(f"{campo} excede {limite} caracteres (tem {len(valor)})", campo=campo)


def _validar_token(valor: str, regex: re.Pattern, limite: int, campo: str) -> None:
    _validar_tamanho(valor, limite, campo)
    if not regex.match(valor):
        raise CampoInvalido(f"{campo} nao bate com o formato exigido ({regex.pattern})", campo=campo)


def _validar_model(valor: str) -> None:
    _validar_tamanho(valor, _LIMITE_MODEL, "model")
    if _REGEX_MODEL_PROIBIDO.search(valor):
        raise CampoInvalido("model nao pode conter espaco ou caractere de controle", campo="model")


def _validar_event_id(valor: str) -> None:
    """
    `event_id` e um UUID de verdade (RFC 4122) — nao "qualquer string de ate
    36 caracteres". Validado via `uuid.UUID(...)`; qualquer string que nao
    parseie como UUID e rejeitada, mesmo tendo exatamente 36 caracteres
    (ex.: `"x" * 36`). Nada neste plano (Fases 0-10) precisa de um formato
    de id diferente de UUID.
    """
    _validar_tamanho(valor, _LIMITE_EVENT_ID, "event_id")
    try:
        uuid.UUID(valor)
    except ValueError as exc:
        raise CampoInvalido(
            f"event_id deve ser um UUID valido (RFC 4122), recebido {valor!r}",
            campo="event_id",
        ) from exc


def _validar_tipo(tipo: "TipoEvento | str") -> TipoEvento:
    """
    Aceita `TipoEvento` ou a string exata de um dos 18 valores; qualquer
    outra coisa levanta `CampoInvalido` — nunca `AttributeError` (`tipo.value`
    sobre uma string comum) nem `ValueError` cru vazando do enum.
    """
    if isinstance(tipo, TipoEvento):
        return tipo
    if isinstance(tipo, str):
        try:
            return TipoEvento(tipo)
        except ValueError as exc:
            raise CampoInvalido(f"tipo de evento invalido: {tipo!r}", campo="tipo") from exc
    raise CampoInvalido(f"tipo deve ser TipoEvento ou str, recebido {type(tipo).__name__}", campo="tipo")


def _validar_resultado(valor: object) -> str | None:
    if valor is None:
        return None
    if isinstance(valor, ResultadoEvento):
        return valor.value
    if isinstance(valor, str):
        try:
            return ResultadoEvento(valor).value
        except ValueError as exc:
            raise CampoInvalido(f"result invalido: {valor!r}", campo="result") from exc
    raise CampoInvalido(f"result deve ser ResultadoEvento ou str, recebido {type(valor).__name__}", campo="result")


# ---------------------------------------------------------------------------
# Payload — allowlist tipada. Ver docstring do modulo.
# ---------------------------------------------------------------------------
CAMPOS_TRIAGEM: frozenset[str] = frozenset({
    "nome", "email", "destinos", "total_pessoas", "adultos", "criancas", "datas", "dias",
})

CHAVES_PAYLOAD_PERMITIDAS: frozenset[str] = frozenset({
    "explicit_human_request",
    "campos_faltantes",
    "prefiltro_motivo",
    "origem",
})

_TAMANHO_MAX_LISTA = 20
_TAMANHO_MAX_PAYLOAD_JSON = 2000


def _validar_explicit_human_request(valor: object) -> None:
    if not isinstance(valor, bool):
        raise PayloadInvalido(
            f"explicit_human_request deve ser bool, recebido {type(valor).__name__}",
            chave="explicit_human_request",
        )


def _validar_campos_faltantes(valor: object) -> None:
    if not isinstance(valor, list):
        raise PayloadInvalido(
            f"campos_faltantes deve ser lista, recebido {type(valor).__name__}",
            chave="campos_faltantes",
        )
    if len(valor) > _TAMANHO_MAX_LISTA:
        raise PayloadInvalido(f"campos_faltantes excede {_TAMANHO_MAX_LISTA} itens", chave="campos_faltantes")
    for item in valor:
        if not isinstance(item, str) or item not in CAMPOS_TRIAGEM:
            raise PayloadInvalido(
                f"campos_faltantes so aceita valores de {sorted(CAMPOS_TRIAGEM)}, "
                f"recebido item do tipo {type(item).__name__}",
                chave="campos_faltantes",
            )


def _validar_enum_payload(valor: object, enum_cls: type[StrEnum], chave: str) -> None:
    if isinstance(valor, enum_cls):
        return
    if isinstance(valor, str):
        try:
            enum_cls(valor)
            return
        except ValueError:
            pass
    raise PayloadInvalido(
        f"{chave} deve ser um de {[e.value for e in enum_cls]}, recebido {type(valor).__name__}",
        chave=chave,
    )


_VALIDADORES_PAYLOAD = {
    "explicit_human_request": _validar_explicit_human_request,
    "campos_faltantes": _validar_campos_faltantes,
    "prefiltro_motivo": lambda v: _validar_enum_payload(v, MotivoPrefiltro, "prefiltro_motivo"),
    "origem": lambda v: _validar_enum_payload(v, OrigemEvento, "origem"),
}


def _validar_payload(payload: dict | None) -> None:
    """
    Levanta `PayloadInvalido` nomeando a chave/motivo da rejeicao. Roda ANTES
    de qualquer `db.add()` (payload rejeitado nao pode deixar linha parcial
    nem sessao suja).

    O corpo inteiro roda dentro de um try/except que converte QUALQUER
    `ValueError`/`TypeError` inesperado — nao so os que este modulo levanta
    de proposito — em `PayloadInvalido`. Caso concreto: `json.dumps` levanta
    `ValueError` puro para um int com mais de `sys.get_int_max_str_digits()`
    digitos (default 4300). Sem este wrapper esse `ValueError` escaparia cru,
    quebrando o contrato de que so excecoes tipadas deste modulo saem de
    `registrar_evento`.
    """
    if payload is None:
        return
    try:
        _validar_payload_conteudo(payload)
    except PayloadInvalido:
        raise
    except (ValueError, TypeError) as exc:
        raise PayloadInvalido(f"payload invalido: {exc}", chave=None) from exc


def _validar_payload_conteudo(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise PayloadInvalido(f"payload deve ser dict, recebido {type(payload).__name__}", chave=None)

    # Guarda de tamanho/serializacao ANTES da checagem por chave — ver
    # docstring de `_validar_payload` sobre o caso do int com milhares de
    # digitos: e aqui que `json.dumps` seria chamado sobre ele.
    serializado = json.dumps(payload)
    if len(serializado) > _TAMANHO_MAX_PAYLOAD_JSON:
        raise PayloadInvalido(f"payload serializado excede {_TAMANHO_MAX_PAYLOAD_JSON} caracteres", chave=None)

    for chave, valor in payload.items():
        if chave not in CHAVES_PAYLOAD_PERMITIDAS:
            raise PayloadInvalido(f"chave de payload fora da allowlist: {chave!r}", chave=str(chave))
        _VALIDADORES_PAYLOAD[chave](valor)


def _persistir_ou_compensar(db: Session, evento: ConversationEvent, event_id_final: str) -> None:
    """
    Insere `evento` dentro de um SAVEPOINT (`db.begin_nested()`), nunca de um
    `db.rollback()` direto — mesmo motivo de `garantir_entrada_no_funil` e
    `_obter_ou_criar_tag` (app/services/lead_creation.py, CRM): um
    `db.rollback()` aqui desfaria a transacao INTEIRA do caller, nao so este
    INSERT. Sem o SAVEPOINT, o `IntegrityError` deixa a sessao em
    `InFailedSqlTransaction` no PostgreSQL e toda chamada seguinte na mesma
    sessao falha — o SAVEPOINT desfaz SO o INSERT que colidiu.

    Em `IntegrityError`, RE-CONSULTA por `event_id_final`: so vira
    `EventoDuplicado` se a linha REALMENTE existir. As unicas constraints
    desta tabela sao o UNIQUE de `event_id` e o NOT NULL de `event_id`/
    `event_type` — os dois ultimos ja garantidos pela validacao de
    `registrar_evento` antes de chegar aqui, entao na pratica a UNICA
    violacao esperada e a de `event_id`. Mas se OUTRA violacao acontecer
    (ex.: um caller que constroi `ConversationEvent` direto, sem passar por
    `registrar_evento`), mascara-la de `EventoDuplicado` esconderia a causa
    real atras de um erro que nao aponta pra ela — mesmo padrao de
    `_obter_ou_criar_tag` (CRM): "nao era essa a violacao... levanta o
    original".
    """
    try:
        with db.begin_nested():
            db.add(evento)
            db.flush()
    except IntegrityError as exc:
        existe = db.query(ConversationEvent).filter_by(event_id=event_id_final).first()
        if existe is None:
            raise
        raise EventoDuplicado(event_id=event_id_final) from exc


def registrar_evento(
    db: Session,
    *,
    tipo: "TipoEvento | str",
    event_id: str | None = None,
    conversation_id: int | None = None,
    lead_id: int | None = None,
    message_id: int | None = None,
    whatsapp_msg_id: str | None = None,
    state_before: str | None = None,
    state_after: str | None = None,
    action: str | None = None,
    target_user_id: int | None = None,
    model: str | None = None,
    model_attempt: int | None = None,
    duration_ms: int | None = None,
    result: "ResultadoEvento | str | None" = None,
    error_code: str | None = None,
    payload: dict | None = None,
    commit: bool = True,
) -> ConversationEvent:
    """
    Grava um `ConversationEvent`.

    `event_id` omitido gera um `uuid4()`; informado, precisa ser um UUID
    valido (`_validar_event_id`) — nao qualquer string de ate 36 caracteres.
    Duplicata levanta `EventoDuplicado` — nunca devolve a linha existente
    (ver docstring da excecao).

    `tipo` aceita `TipoEvento` ou a string exata de um dos 18 valores;
    qualquer outra coisa levanta `CampoInvalido` (ver `_validar_tipo`).

    Todo campo (`event_id`, `event_type`, `whatsapp_msg_id`, `state_before`,
    `state_after`, `action`, `model`, `result`, `error_code`) e validado
    contra o tamanho maximo da coluna e, quando aplicavel, contra um formato
    de token tecnico — ver docstring do modulo sobre isto ser contrato
    SINTATICO, nao controle de PII. `payload` e validado por
    `_validar_payload`. TODA validacao roda ANTES de qualquer `db.add()`.

    O INSERT roda dentro de um SAVEPOINT via `_persistir_ou_compensar` — ver
    o docstring la para a checagem compensatoria de `IntegrityError`.

    `commit=False` faz so `flush()`: o evento recebe `id`, mas o controle da
    transacao continua com o caller (ex.: gravar o evento junto de outras
    mudancas na mesma unidade atomica).
    """
    tipo_validado = _validar_tipo(tipo)
    resultado_validado = _validar_resultado(result)

    event_id_final = event_id or str(uuid.uuid4())
    _validar_event_id(event_id_final)
    _validar_tamanho(tipo_validado.value, _LIMITE_EVENT_TYPE, "event_type")
    if resultado_validado is not None:
        _validar_tamanho(resultado_validado, _LIMITE_RESULT, "result")

    if whatsapp_msg_id is not None:
        _validar_tamanho(whatsapp_msg_id, _LIMITE_WHATSAPP_MSG_ID, "whatsapp_msg_id")
    if state_before is not None:
        _validar_token(state_before, _REGEX_ESTADO, _LIMITE_STATE, "state_before")
    if state_after is not None:
        _validar_token(state_after, _REGEX_ESTADO, _LIMITE_STATE, "state_after")
    if action is not None:
        _validar_token(action, _REGEX_ACTION, _LIMITE_ACTION, "action")
    if model is not None:
        _validar_model(model)
    if error_code is not None:
        _validar_token(error_code, _REGEX_ERROR_CODE, _LIMITE_ERROR_CODE, "error_code")

    _validar_payload(payload)  # ANTES de qualquer db.add() — ver docstring.

    evento = ConversationEvent(
        event_id=event_id_final,
        event_type=tipo_validado.value,
        conversation_id=conversation_id,
        lead_id=lead_id,
        message_id=message_id,
        whatsapp_msg_id=whatsapp_msg_id,
        state_before=state_before,
        state_after=state_after,
        action=action,
        target_user_id=target_user_id,
        model=model,
        model_attempt=model_attempt,
        duration_ms=duration_ms,
        result=resultado_validado,
        error_code=error_code,
        payload=payload,
    )
    _persistir_ou_compensar(db, evento, event_id_final)

    if commit:
        db.commit()
    return evento
