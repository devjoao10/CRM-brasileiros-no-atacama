from datetime import datetime, timezone

from sqlalchemy import Column, Integer, String, DateTime, Index, JSON
from sqlalchemy.sql import func

from app.database import Base


class ConversationEvent(Base):
    """
    Trilha de eventos da conversa — WP BIA-V2 Fase 0 (Task 0.2).

    Objetivo: reconstruir "o que aconteceu com esta conversa?" sem abrir
    workflow nenhum. Tabela APPEND-ONLY (so INSERT; nunca UPDATE/DELETE pelo
    fluxo normal).

    `conversation_id` e PROPOSITALMENTE sem ForeignKey: o evento e um registro
    de auditoria e precisa sobreviver a delecao do que ele descreve. Uma
    conversa apagada nao pode levar a propria trilha de eventos junto.

    `payload` tem allowlist de chaves aplicada em `app/v2/eventos.py`
    (`registrar_evento`) — ver o docstring la para o motivo de ser allowlist e
    nao filtro heuristico.
    """
    __tablename__ = "conversation_events"

    id = Column(Integer, primary_key=True, index=True)
    # UNIQUE de verdade no banco — ver __table_args__. Gerado com uuid4() por
    # registrar_evento() quando nao informado.
    event_id = Column(String(36), nullable=False)
    event_type = Column(String(48), nullable=False)

    conversation_id = Column(Integer, nullable=True)
    lead_id = Column(Integer, nullable=True)
    message_id = Column(Integer, nullable=True)
    # String(100) — mesmo tamanho de Message.whatsapp_msg_id (models/conversation.py):
    # mesmo dado (wamid da Meta), mesma coluna origem em outbound.py/webhook.py.
    whatsapp_msg_id = Column(String(100), nullable=True)

    state_before = Column(String(32), nullable=True)
    state_after = Column(String(32), nullable=True)
    action = Column(String(64), nullable=True)
    target_user_id = Column(Integer, nullable=True)

    model = Column(String(64), nullable=True)
    model_attempt = Column(Integer, nullable=True)
    duration_ms = Column(Integer, nullable=True)

    result = Column(String(32), nullable=True)
    error_code = Column(String(64), nullable=True)

    payload = Column(JSON, nullable=True)

    # Mesmo padrao de Message.created_at (models/conversation.py): default
    # Python-side + server_default. `now()` do Postgres e por-TRANSACAO, entao
    # eventos gravados numa transacao longa (ex.: debounce esperando a Bia)
    # ficariam todos com o MESMO instante sem o default do lado do Python.
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    # AUDIT-2026-08-W2E ja deixou o motivo documentado em Conversation.whatsapp:
    # UNIQUE declarada como Index(unique=True), nao Column(unique=True) nem
    # UniqueConstraint, para que create_all() e a migration m013 emitam
    # EXATAMENTE o mesmo objeto (CREATE UNIQUE INDEX) nos dois dialetos.
    __table_args__ = (
        Index("uq_conversation_events_event_id", "event_id", unique=True),
        # O motivo de existir esta tabela e reconstruir o historico de UMA
        # conversa em ordem — daqui os dois indices abaixo.
        Index("ix_conversation_events_conversation_id", "conversation_id"),
        Index("ix_conversation_events_created_at", "created_at"),
    )

    def __repr__(self):
        return f"<ConversationEvent(id={self.id}, event_type='{self.event_type}')>"
