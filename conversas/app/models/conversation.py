from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base

# CONV-WINDOW-01: janela de atendimento de 24h da WhatsApp Business Platform.
SERVICE_WINDOW = timedelta(hours=24)


def service_window_open(last_customer_msg_at, now=None) -> bool:
    """
    FONTE UNICA da regra de 24h. Funcao PURA — sem DB, sem I/O, sem now() implicito
    quando `now` e informado. Todo o resto do sistema (guard das rotas, serializacao,
    frontend) consome ESTE calculo; ninguem reimplementa 24h em outro lugar.

        aberta  <=>  last_customer_msg_at IS NOT NULL
                     AND now < last_customer_msg_at + 24h

    Exatamente 24h => FECHADA (`<`, nunca `<=`).
    NULL            => FECHADA (conversa sem inbound do cliente nunca teve janela).

    Timestamps naive sao normalizados como UTC: o PostgreSQL de producao devolve
    tz-aware (TIMESTAMPTZ) mas o SQLite de dev/CI devolve naive, e comparar os dois
    levantaria TypeError. Sem esta normalizacao a suite inteira quebra fora de prod.
    """
    if last_customer_msg_at is None:
        return False
    if last_customer_msg_at.tzinfo is None:
        last_customer_msg_at = last_customer_msg_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now < last_customer_msg_at + SERVICE_WINDOW


class Conversation(Base):
    """Uma conversa com um lead via WhatsApp."""
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, nullable=False, index=True)
    whatsapp = Column(String(30), nullable=False, index=True)
    nome = Column(String(200), nullable=True)
    status = Column(String(20), default="aberta", nullable=False, index=True)
    ultimo_msg = Column(Text, nullable=True)
    unread_count = Column(Integer, default=0, nullable=False)
    atendente_id = Column(Integer, nullable=True, index=True)
    is_bot_active = Column(Boolean, default=True, nullable=False)
    responsavel_id = Column(Integer, nullable=True, index=True)     # Synced with CRM lead.responsavel_id
    responsavel_nome = Column(String(200), nullable=True)           # Cached name for display
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    last_customer_msg_at = Column(DateTime(timezone=True), nullable=True)  # Janela 24h Meta
    # PACOTE-A: momento em que a conversa ENTROU na fila de atendimento humano.
    # NAO e atividade do cliente (last_customer_msg_at), nem updated_at/created_at.
    # Preenchido no handoff BIA->humano e no release; zerado quando alguem assume.
    queued_at = Column(DateTime(timezone=True), nullable=True, index=True)

    # Relationships
    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at"
    )
    # CONV-05: tags N:N (link table com PK composta)
    tags = relationship(
        "ConversationTag",
        secondary="conversation_tag_links",
        back_populates="conversations",
    )
    # CONV-07: notas internas (nunca enviadas ao WhatsApp)
    notes = relationship(
        "ConversationNote",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="ConversationNote.created_at",
    )

    @property
    def service_window_open(self) -> bool:
        """
        CONV-WINDOW-01: recalculado a CADA leitura (nunca persistido) — a janela
        fecha pela passagem do tempo, nao por um evento que pudesse ser gravado.
        `from_attributes` do Pydantic serializa isto automaticamente, entao a
        lista, o detalhe e o guard das rotas leem o MESMO valor.
        """
        return service_window_open(self.last_customer_msg_at)

    def __repr__(self):
        return f"<Conversation(id={self.id}, lead_id={self.lead_id}, nome='{self.nome}')>"


class Message(Base):
    """Uma mensagem individual dentro de uma conversa."""
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    direction = Column(String(10), nullable=False)  # 'inbound' ou 'outbound'
    content = Column(Text, nullable=False)
    msg_type = Column(String(20), default="text", nullable=False)  # text, image, audio, document, video
    media_url = Column(Text, nullable=True)
    whatsapp_msg_id = Column(String(100), nullable=True, unique=True)
    status = Column(String(20), default="sent", nullable=False)  # sent, delivered, read, failed
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # CONV-08b — integridade de outbound (base para retry).
    # Bancos existentes: aplicar migrations/m003_conversas_message_error_fields.py.
    last_error = Column(Text, nullable=True)          # resumo SEGURO da ultima falha (sem token/payload)
    send_attempts = Column(Integer, default=0, nullable=False)  # tentativas de envio (outbound)
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)  # ultima tentativa de envio

    # Relationships
    conversation = relationship("Conversation", back_populates="messages")
    # CONV-01: 1:1 com media_assets (so mensagens de midia possuem asset)
    media_asset = relationship(
        "MediaAsset",
        back_populates="message",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self):
        return f"<Message(id={self.id}, direction='{self.direction}', type='{self.msg_type}')>"
