from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Table
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


# CONV-05 — N:N conversa<->tag. PK composta = aplicar 2x e naturalmente
# impossivel (idempotencia no banco, nao so na rota).
conversation_tag_links = Table(
    "conversation_tag_links",
    Base.metadata,
    Column("conversation_id", Integer,
           ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer,
           ForeignKey("conversation_tags.id", ondelete="CASCADE"), primary_key=True),
)


class ConversationTag(Base):
    """
    CONV-05 — Tag do Conversas (aplicada a CONVERSA, nao a mensagem).

    `cor` e validada na rota (^#hex6$) porque vai para um atributo style no
    frontend — nunca confiar no valor persistido sem essa garantia.
    """
    __tablename__ = "conversation_tags"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String(50), unique=True, nullable=False, index=True)
    cor = Column(String(7), default="#3B82F6", nullable=False)  # hex #RRGGBB
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    conversations = relationship(
        "Conversation",
        secondary=conversation_tag_links,
        back_populates="tags",
    )

    def __repr__(self):
        return f"<ConversationTag(id={self.id}, nome='{self.nome}')>"
