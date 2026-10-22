"""
BIA-V2 Fase 0 / Task 0.2 - `registrar_evento()`.

Objetivo do WP: reconstruir "o que aconteceu com esta conversa?" sem abrir
workflow nenhum (ver docs/superpowers/plans/2026-08-29-bia-v2.md).

Este modulo e a camada de PERSISTENCIA: monta o `ConversationEvent`, roda o
INSERT dentro de um SAVEPOINT (`_persistir_ou_compensar`) e decide
commit/flush. O CONTRATO de validacao (vocabulario, limites, regexes,
excecoes tipadas, `TipoEvento`) mora em
`conversas/app/v2/eventos_validacao.py` - ver o docstring de la para o "por
que" de cada regra de campo e de payload, e para o motivo de `TipoEvento`
morar la. Este arquivo so CONSOME aquele contrato; nunca o contrario.
"""
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.evento import ConversationEvent
from app.v2.eventos_validacao import (
    ResultadoEvento,
    TipoEvento,
    validar_action,
    validar_error_code,
    validar_event_id,
    validar_inteiro,
    validar_model,
    validar_payload,
    validar_resultado,
    validar_state,
    validar_tipo,
    validar_whatsapp_msg_id,
)


class EventoDuplicado(Exception):
    """
    `event_id` ja existe. NUNCA devolve a linha existente em silencio - isso
    mascararia o caso em que dois eventos DIFERENTES colidem no mesmo id.
    Quem quer idempotencia (ex.: Fase 6, handoff) captura esta excecao de
    proposito e decide o que fazer.
    """

    def __init__(self, event_id: str):
        # A mensagem NAO ecoa o valor. Hoje `event_id` sempre chega canonizado
        # por `validar_event_id` (unico ponto de construcao e
        # `_persistir_ou_compensar`), mas isso e disciplina de call-site, nao
        # garantia da classe: um caller futuro (Fase 6) pode construir esta
        # excecao direto com valor nao validado, e o valor cru voltaria a
        # aparecer em log ou num 500. O dado continua disponivel de forma
        # estruturada em `.event_id`, para quem precisar dele de proposito.
        self.event_id = event_id
        super().__init__("event_id duplicado")


def _persistir_ou_compensar(db: Session, evento: ConversationEvent, event_id_final: str) -> None:
    """
    Insere `evento` dentro de um SAVEPOINT (`db.begin_nested()`), nunca de um
    `db.rollback()` direto - mesmo motivo de `garantir_entrada_no_funil` e
    `_obter_ou_criar_tag` (app/services/lead_creation.py, CRM): um
    `db.rollback()` aqui desfaria a transacao INTEIRA do caller, nao so este
    INSERT. Sem o SAVEPOINT, o `IntegrityError` deixa a sessao em
    `InFailedSqlTransaction` no PostgreSQL e toda chamada seguinte na mesma
    sessao falha - o SAVEPOINT desfaz SO o INSERT que colidiu.

    Em `IntegrityError`, RE-CONSULTA por `event_id_final`: so vira
    `EventoDuplicado` se a linha REALMENTE existir. As unicas constraints
    desta tabela sao o UNIQUE de `event_id` e o NOT NULL de `event_id`/
    `event_type` - os dois ultimos ja garantidos pela validacao de
    `registrar_evento` antes de chegar aqui, entao na pratica a UNICA
    violacao esperada e a de `event_id`. Mas se OUTRA violacao acontecer
    (ex.: um caller que constroi `ConversationEvent` direto, sem passar por
    `registrar_evento`), mascara-la de `EventoDuplicado` esconderia a causa
    real atras de um erro que nao aponta pra ela - mesmo padrao de
    `_obter_ou_criar_tag` (CRM): "nao era essa a violacao... levanta o
    original".

    `event_id_final` chega aqui ja CANONICALIZADO (`validar_event_id` em
    `eventos_validacao.py`) - a mesma forma que foi persistida no INSERT e a
    forma usada nesta re-consulta, por construcao (mesma variavel).
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
    commit: bool,
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
) -> ConversationEvent:
    """
    Grava um `ConversationEvent`.

    `event_id` omitido gera um `uuid4()`; informado, precisa ser um UUID
    valido e e CANONICALIZADO (minusculo, com hifens) antes de persistir e
    de servir de chave pra re-consulta de compensacao - ver
    `eventos_validacao.validar_event_id`. Duplicata levanta `EventoDuplicado`
    - nunca devolve a linha existente (ver docstring da excecao).

    `tipo` aceita `TipoEvento` ou a string exata de um dos 18 valores;
    qualquer outra coisa levanta `CampoInvalido`.

    `commit` e KEYWORD-ONLY SEM DEFAULT: toda chamada declara explicitamente
    se commita ou deixa o controle da transacao com o caller. Nao existe
    ainda nenhum caller fora deste WP - a decisao fica explicita desde o
    primeiro, em vez de herdar um default que um router futuro poderia
    commitar sem querer no meio de uma operacao composta.

    Todo campo (event_id, event_type, whatsapp_msg_id, state_before/after,
    action, model, result, error_code, e os campos inteiros abaixo) e
    validado contra o contrato de `eventos_validacao.py` ANTES de qualquer
    `db.add()`. `payload` e validado por `validar_payload`.

    O INSERT roda dentro de um SAVEPOINT via `_persistir_ou_compensar` - ver
    o docstring la para a checagem compensatoria de `IntegrityError`.

    `commit=False` faz so `flush()`: o evento recebe `id`, mas o controle da
    transacao continua com o caller (ex.: gravar o evento junto de outras
    mudancas na mesma unidade atomica).
    """
    tipo_validado = validar_tipo(tipo)
    resultado_validado = validar_resultado(result)

    if event_id is None:
        event_id_final = str(uuid.uuid4())
    else:
        event_id_final = validar_event_id(event_id)

    if whatsapp_msg_id is not None:
        validar_whatsapp_msg_id(whatsapp_msg_id)
    if state_before is not None:
        validar_state(state_before, "state_before")
    if state_after is not None:
        validar_state(state_after, "state_after")
    if action is not None:
        validar_action(action)
    if model is not None:
        validar_model(model)
    if error_code is not None:
        validar_error_code(error_code)

    # conversation_id/lead_id/message_id/target_user_id sao PK autoincrement
    # (Integer, primary_key=True - confirmado em app/models/*.py do CRM e em
    # conversas/app/models/conversation.py) - minimo=1, nunca 0 ou negativo.
    if conversation_id is not None:
        validar_inteiro(conversation_id, "conversation_id", minimo=1)
    if lead_id is not None:
        validar_inteiro(lead_id, "lead_id", minimo=1)
    if message_id is not None:
        validar_inteiro(message_id, "message_id", minimo=1)
    if target_user_id is not None:
        validar_inteiro(target_user_id, "target_user_id", minimo=1)
    if model_attempt is not None:
        # AMBIGUIDADE REGISTRADA (nao resolvida aqui): nao ha contrato
        # definindo se a primeira tentativa e 0-based ou 1-based - grep no
        # plano e em n8n/workflows/** (maxTries/retryOnFail) nao expoe um
        # contador. So o piso >=0 e aplicado, a uniao dos dois esquemas.
        # Fase 8 (Interpretadora), que de fato implementa retry, precisa
        # decidir e documentar a convencao antes de gravar este campo.
        validar_inteiro(model_attempt, "model_attempt", minimo=0)
    if duration_ms is not None:
        validar_inteiro(duration_ms, "duration_ms", minimo=0)

    validar_payload(payload)  # ANTES de qualquer db.add() - ver eventos_validacao.py.

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
