from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean
from sqlalchemy.sql import func

from app.database import Base


class MessageTemplate(Base):
    """WhatsApp Message Template — obrigatório pelo Meta para envio fora da janela 24h."""
    __tablename__ = "message_templates"

    id              = Column(Integer, primary_key=True, index=True)
    name            = Column(String(512), unique=True, nullable=False)   # snake_case
    category        = Column(String(20), nullable=False)                 # MARKETING, UTILITY, AUTHENTICATION
    language        = Column(String(10), default="pt_BR", nullable=False)
    status          = Column(String(20), default="PENDING", nullable=False)  # PENDING, APPROVED, REJECTED, PAUSED

    # Components
    header_type     = Column(String(10), nullable=True)    # TEXT, IMAGE, VIDEO, DOCUMENT ou None
    header_text     = Column(String(60), nullable=True)
    body_text       = Column(Text, nullable=False)         # Até 1024 chars, suporta {{1}}, {{2}}...
    footer_text     = Column(String(60), nullable=True)
    buttons_json    = Column(Text, nullable=True)          # JSON array: [{type, text, url/payload}]

    # Exemplos de variáveis (obrigatório para aprovação Meta)
    sample_values_json = Column(Text, nullable=True)       # JSON: {"header": ["João"], "body": ["12345", "15/03"]}

    # Meta sync
    meta_template_id = Column(String(100), nullable=True, unique=True)
    rejection_reason = Column(Text, nullable=True)

    created_at      = Column(DateTime(timezone=True), server_default=func.now())
    updated_at      = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<MessageTemplate(name='{self.name}', status='{self.status}')>"
