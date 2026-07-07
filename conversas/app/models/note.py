from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class ConversationNote(Base):
    """
    CONV-07 — Nota INTERNA de conversa.

    INVARIANTE: notas NUNCA sao enviadas ao WhatsApp — sao visiveis apenas
    para a equipe (nenhum caminho de codigo chama whatsapp.* a partir de
    notas; ha teste que conta as chamadas).

    `user_nome` e snapshot para exibicao (padrao responsavel_nome) — evita
    join com users em toda listagem.
    """
    __tablename__ = "conversation_notes"

    id = Column(Integer, primary_key=True, index=True)
    conversation_id = Column(
        Integer,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(Integer, nullable=False, index=True)  # autor (current_user)
    user_nome = Column(String(200), nullable=True)         # snapshot p/ exibicao
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    conversation = relationship("Conversation", back_populates="notes")

    def __repr__(self):
        return f"<ConversationNote(id={self.id}, conversation_id={self.conversation_id})>"
