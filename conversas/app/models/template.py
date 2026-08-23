from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, UniqueConstraint
from sqlalchemy.sql import func

from app.database import Base


class ServiceTemplate(Base):
    """
    CONV-CURATION-01 — curadoria: quais templates a Meta aprovou E o CRM autoriza
    a aparecer para os atendentes.

    APPROVED na Meta != AUTORIZADO PARA ATENDIMENTO. A conta tem templates
    operacionais (alertas de lead, notificacoes internas, hello_world, testes)
    que a Meta aprovou e que NUNCA devem ser oferecidos a um cliente.

    Guarda SOMENTE a autorizacao — `name` + `language` e nada mais. A Meta segue
    sendo a fonte de verdade de status, category e components; duplicar o
    template aqui criaria um segundo estado para divergir do primeiro.

    Autorizacao e PRESENCA DE LINHA, nao booleano: sem linha => nao autorizado.
    Fail closed por construcao — nao existe flag para alguem inverter, nem
    default a interpretar, e um banco vazio nao libera nada.

    Nao usa `message_templates` porque (a) aquela tabela e um espelho do que foi
    criado PELO app e nao contem os templates criados no Business Manager, e
    (b) `message_templates.name` e UNIQUE, o que impede representar o mesmo nome
    em dois idiomas — e a identidade correta e (name, language).
    """
    __tablename__ = "service_templates"
    __table_args__ = (
        UniqueConstraint("name", "language", name="uq_service_templates_name_language"),
    )

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(512), nullable=False)
    language = Column(String(10), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<ServiceTemplate(name='{self.name}', language='{self.language}')>"


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
