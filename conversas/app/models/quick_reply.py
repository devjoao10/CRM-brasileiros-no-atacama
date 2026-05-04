from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean
from sqlalchemy.sql import func

from app.database import Base


class QuickReply(Base):
    """Mensagem rápida — atalho para respostas frequentes."""
    __tablename__ = "quick_replies"

    id         = Column(Integer, primary_key=True, index=True)
    shortcut   = Column(String(50), unique=True, nullable=False)   # ex: "/boasvindas"
    title      = Column(String(100), nullable=False)               # ex: "Boas-vindas ao Atacama"
    content    = Column(Text, nullable=False)                      # Texto completo da mensagem
    category   = Column(String(50), nullable=True)                 # ex: "Saudação", "Pagamento"
    is_active  = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<QuickReply(shortcut='{self.shortcut}', title='{self.title}')>"
