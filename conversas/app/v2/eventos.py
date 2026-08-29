"""
BIA-V2 Fase 0 / Task 0.2 — `TipoEvento` e `registrar_evento()`.

Objetivo do WP: conseguir reconstruir "o que aconteceu com esta conversa?"
sem abrir workflow nenhum (ver docs/superpowers/plans/2026-08-29-bia-v2.md).

POR QUE `payload` E ALLOWLIST DE CHAVES, E NAO UM FILTRO HEURISTICO
--------------------------------------------------------------------
Nao existe regex confiavel para "isto e sensivel". Um filtro heuristico
(bloquear string que "parece" telefone, e-mail ou nome) erra nas DUAS
direcoes ao mesmo tempo:

  - deixa passar formato imprevisto — um numero sem o `+55`, um e-mail sem
    `@` reconhecivel, um campo novo que ninguem cobriu ainda;
  - E mascara dado legitimo — um `lead_id` numerico grande confundido com
    telefone, por exemplo — destruindo o proprio valor diagnostico que esta
    tabela existe para criar.

A defesa aqui e ESTRUTURAL, nao heuristica: `CHAVES_PAYLOAD_PERMITIDAS` nao
tem NENHUMA chave para conteudo de mensagem, telefone, nome, e-mail ou token.
Nao ha regex para escapar de uma chave que nao existe.
"""
import json
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
    """`payload` fora da allowlist de chaves ou fora do formato aceito."""


# Allowlist deliberadamente minima — ver docstring do modulo.
CHAVES_PAYLOAD_PERMITIDAS: frozenset[str] = frozenset({
    "motivo",
    "campos_faltantes",
    "intent",
    "explicit_human_request",
    "prefiltro_motivo",
    "tentativa",
    "origem",
})

_TAMANHO_MAX_STR = 200
_TAMANHO_MAX_LISTA = 20
_TAMANHO_MAX_PAYLOAD_JSON = 2000


def _validar_payload(payload: dict | None) -> None:
    """
    Levanta `PayloadInvalido` nomeando a chave/motivo da rejeicao.

    Roda ANTES de qualquer `db.add()`: payload rejeitado nao pode deixar
    linha parcial nem sessao suja (ver teste 13 do WP).
    """
    if payload is None:
        return

    for chave, valor in payload.items():
        if chave not in CHAVES_PAYLOAD_PERMITIDAS:
            raise PayloadInvalido(f"chave de payload fora da allowlist: {chave!r}")

        if isinstance(valor, str):
            if len(valor) > _TAMANHO_MAX_STR:
                raise PayloadInvalido(
                    f"valor de {chave!r} excede {_TAMANHO_MAX_STR} caracteres"
                )
        elif isinstance(valor, list):
            if len(valor) > _TAMANHO_MAX_LISTA:
                raise PayloadInvalido(
                    f"lista de {chave!r} excede {_TAMANHO_MAX_LISTA} itens"
                )
            for item in valor:
                if not isinstance(item, str) or len(item) > _TAMANHO_MAX_STR:
                    raise PayloadInvalido(
                        f"lista de {chave!r} so aceita strings de ate {_TAMANHO_MAX_STR} caracteres"
                    )
        elif isinstance(valor, (bool, int, float)) or valor is None:
            pass
        else:
            raise PayloadInvalido(
                f"valor de {chave!r} nao e escalar nem lista de strings: {type(valor).__name__}"
            )

    if len(json.dumps(payload)) > _TAMANHO_MAX_PAYLOAD_JSON:
        raise PayloadInvalido(
            f"payload serializado excede {_TAMANHO_MAX_PAYLOAD_JSON} caracteres"
        )


def registrar_evento(
    db: Session,
    *,
    tipo: TipoEvento,
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
    result: str | None = None,
    error_code: str | None = None,
    payload: dict | None = None,
    commit: bool = True,
) -> ConversationEvent:
    """
    Grava um `ConversationEvent`.

    `event_id` omitido gera um `uuid4()`. Duplicata levanta `EventoDuplicado`
    — nunca devolve a linha existente (ver docstring da excecao).

    O INSERT roda dentro de um SAVEPOINT (`db.begin_nested()`), nunca de um
    `db.rollback()` direto — mesmo motivo de `garantir_entrada_no_funil` e
    `_obter_ou_criar_tag` (app/services/lead_creation.py, CRM): um
    `db.rollback()` aqui desfaria a transacao INTEIRA do caller, nao so este
    INSERT. Sem o SAVEPOINT, o `IntegrityError` deixa a sessao em
    `InFailedSqlTransaction` no PostgreSQL e toda chamada seguinte na mesma
    sessao falha — o SAVEPOINT desfaz SO o INSERT que colidiu.

    `commit=False` faz so `flush()`: o evento recebe `id`, mas o controle da
    transacao continua com o caller (ex.: gravar o evento junto de outras
    mudancas na mesma unidade atomica).
    """
    _validar_payload(payload)  # ANTES de qualquer db.add() — ver docstring.

    event_id_final = event_id or str(uuid.uuid4())
    evento = ConversationEvent(
        event_id=event_id_final,
        event_type=tipo.value,
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
        result=result,
        error_code=error_code,
        payload=payload,
    )
    try:
        with db.begin_nested():
            db.add(evento)
            db.flush()
    except IntegrityError as exc:
        raise EventoDuplicado(event_id=event_id_final) from exc

    if commit:
        db.commit()
    return evento
