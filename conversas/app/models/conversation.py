from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, Boolean, Index, text,
)
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
    # AUDIT-2026-08-W2E (F1) — o numero e a CHAVE de negocio da conversa, nao um
    # campo pesquisavel qualquer. `webhook.py` e `conversations.py` fazem
    # find-or-create sobre ele; sem UNIQUE, duas primeiras mensagens do mesmo
    # numero chegando juntas criam DUAS conversas e todo leitor usa `.first()`,
    # entao metade das mensagens do cliente some numa thread invisivel.
    # O UNIQUE aqui e o mesmo mecanismo que ja torna o inbound idempotente via
    # `Message.whatsapp_msg_id` — e a unica trava que sobrevive a concorrencia.
    # `index=True` foi REMOVIDO de proposito: o indice unico ja atende as buscas
    # por numero; manter os dois seria indice duplicado na mesma coluna.
    whatsapp = Column(String(30), nullable=False)
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
    # Preenchido no handoff BIA->humano e no release; zerado na PRIMEIRA RESPOSTA
    # HUMANA (nao mais ao atribuir — ver a coluna abaixo).
    queued_at = Column(DateTime(timezone=True), nullable=True, index=True)
    # AUDIT-2026-08-WA — ATRIBUIDO != ATENDIDO.
    # Ate aqui o inbox classificava por `atendente_id IS NULL`, o que fazia de
    # "atribuir" sinonimo de "atender": assim que o handoff (ou um assign)
    # definia um dono, a conversa saia da FILA DE ESPERA — mesmo sem nenhum
    # humano ter falado com o cliente. A regra operacional real e o contrario:
    # a conversa fica na fila enquanto NINGUEM tiver respondido.
    #
    # Abrir, visualizar, outro atendente abrir: nada disso e atendimento. O
    # unico evento que encerra a espera e a PRIMEIRA MENSAGEM OUTBOUND HUMANA.
    # `messages` nao guarda autoria (Bia, auto-resposta e humano passam pelo
    # mesmo record_outbound_message), entao o instante e gravado aqui, pela
    # rota que sabe quem e o `current_user`.
    primeira_resposta_humana_at = Column(DateTime(timezone=True), nullable=True, index=True)

    # AUDIT-2026-08-W2E (F1) — declarado como Index(unique=True) e nao como
    # UniqueConstraint de proposito: assim `create_all()` e a migration m011
    # emitem EXATAMENTE o mesmo objeto (`CREATE UNIQUE INDEX uq_...`) nos dois
    # dialetos. Este sistema tem dois donos de schema competindo (create_all no
    # startup + scripts manuais); DDL divergente entre eles ja produziu drift
    # (ver m003 vs create_all em `send_attempts`) e nao vamos criar mais um.
    __table_args__ = (
        Index("uq_conversations_whatsapp", "whatsapp", unique=True),
    )

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

    # AUDIT-2026-08-WA — NAO e coluna: e um atributo de apresentacao que o
    # router preenche em lote antes de serializar (uma query por pagina, nunca
    # uma por linha). Declarado aqui com default None para que
    # `ConversationResponse.model_validate(conversation)` sempre encontre o
    # atributo, mesmo nos caminhos que nao o preenchem.
    atendente_nome = None

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
    # AUDIT-2026-08-W2E (F5) — `default=0` e CLIENT-side: so a ORM o aplica.
    # A coluna e NOT NULL sem DEFAULT no DDL, entao qualquer INSERT fora da ORM
    # (psql, n8n, COPY, o SQL cru de services/crm.py) era REJEITADO. Pior: o
    # m003 ja cria a coluna com `DEFAULT 0`, logo banco migrado e banco nascido
    # do create_all tinham DDL diferente. `server_default` alinha os dois.
    send_attempts = Column(Integer, default=0, server_default=text("0"), nullable=False)
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
