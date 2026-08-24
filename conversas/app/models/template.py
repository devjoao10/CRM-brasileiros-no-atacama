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


class TemplateParamMap(Base):
    """
    CONV-TPLMAP-01 — de que variavel interna sai o valor de cada `{{n}}` do BODY.

    Tres conceitos DIFERENTES convivem num template e esta tabela liga o 1o ao 2o:
      1. `{{1}}`                 -> parametro posicional oficial da Meta
      2. `@PRIMEIRONOMECLIENTE`  -> variavel interna do Conversas (CONV-VAR-01)
      3. `"Joao"`                -> exemplo que a Meta exige para APROVAR
    O exemplo (3) e material de aprovacao e NUNCA vira valor de envio.

    TABELA PROPRIA, e nao coluna nas que ja existem — as duas alternativas
    quebram por motivo concreto:
      - `service_templates`: autorizacao ali e PRESENCA DE LINHA (fail closed).
        Guardar mapping nessa tabela faria "tem mapping" implicar "liberado no
        atendimento" — dois significados na mesma linha, divergindo no primeiro
        template que alguem mapeia antes de autorizar.
      - `message_templates`: `name` e UNIQUE, entao nao consegue representar a
        chave real (name, language); e so contem o que foi criado PELO app,
        enquanto o composer envia do catalogo da Meta, que inclui templates
        feitos no Business Manager (sem linha local nenhuma).

    Uma linha por POSICAO, com UNIQUE (name, language, position): "no maximo um
    mapeamento por posicao" fica sendo constraint do banco, nao verificacao em
    Python que alguem esquece de chamar no proximo endpoint.

    Guarda o TOKEN em texto, nao FK para `message_variables`, porque
    `variables.render_strict(db, "@TOKEN", ctx)` e exatamente o ponto de entrada
    do resolver (CONV-VAR-02) — sem token nao ha o que resolver, e um id exigiria
    traduzir de volta antes de chamar a mesma funcao. A integridade vem do outro
    lado: excluir/renomear variavel em uso e bloqueado em routers/variables.py.
    """
    __tablename__ = "template_param_maps"
    __table_args__ = (
        UniqueConstraint("name", "language", "position", name="uq_template_param_maps_key"),
    )

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(512), nullable=False)
    language = Column(String(10), nullable=False)
    position = Column(Integer, nullable=False)          # o n de {{n}}, base 1
    token = Column(String(61), nullable=False)          # "@PRIMEIRONOMECLIENTE"
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self):
        return f"<TemplateParamMap({self.name}/{self.language} {{{{{self.position}}}}}={self.token})>"


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
