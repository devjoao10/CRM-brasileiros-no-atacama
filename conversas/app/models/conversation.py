from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


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
    atendente_id = Column(Integer, nullable=True)
    is_bot_active = Column(Boolean, default=True, nullable=False)
    responsavel_id = Column(Integer, nullable=True, index=True)     # Synced with CRM lead.responsavel_id
    responsavel_nome = Column(String(200), nullable=True)           # Cached name for display
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    last_customer_msg_at = Column(DateTime(timezone=True), nullable=True)  # Janela 24h Meta

    # Relationships
    messages = relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at"
    )

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

    def __repr__(self):
        return f"<Message(id={self.id}, direction='{self.direction}', type='{self.msg_type}')>"
