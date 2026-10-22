from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean
from sqlalchemy.sql import func

from app.database import Base


class AutoReply(Base):
    """Frase automática — resposta do sistema em situações específicas."""
    __tablename__ = "auto_replies"

    id        = Column(Integer, primary_key=True, index=True)
    trigger   = Column(String(50), unique=True, nullable=False)
    # Triggers possíveis:
    # - greeting           → Frase de apresentação (1ª mensagem do cliente)
    # - start_service      → Início de atendimento (atendente assume a conversa)
    # - waiting            → Aguardando atendimento (ninguém assumiu ainda)
    # - end_service        → Término de atendimento (conversa encerrada)
    # - out_of_hours       → Fora do expediente
    # - break_time         → Intervalo de atendimento
    # - paused             → Status "em pausa"

    title     = Column(String(100), nullable=False)     # Nome legível (para a UI)
    message   = Column(Text, nullable=False)            # Texto da mensagem
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<AutoReply(trigger='{self.trigger}', title='{self.title}')>"


# CONV-EXPEDIENTE-01: catálogo dos 7 gatilhos com título e texto padrão — FONTE ÚNICA do seed
# de desenvolvimento (`seed_auto_replies`) e da tela de Configurações (o GET lista o gatilho
# que ainda não tem linha; o PUT cria a linha a partir daqui). Em produção o seed não roda,
# então sem este catálogo a tela não teria o que mostrar nem criar.
# `is_active` aqui é o padrão do SEED: na tela, gatilho sem linha no banco aparece INATIVO
# (quem liga é o dono, de propósito). greeting e break_time não têm texto padrão.
AUTO_REPLY_PADROES = (
    {
        "trigger": "greeting",
        "title": "Frase de apresentação",
        "message": "",
        "is_active": False,
    },
    {
        "trigger": "start_service",
        "title": "Início de atendimento",
        "message": "Estamos iniciando o seu atendimento. Como podemos te ajudar hoje?",
        "is_active": True,
    },
    {
        "trigger": "waiting",
        "title": "Aguardando atendimento",
        "message": "Recebemos sua mensagem! Em breve, um atendente estará com você. Obrigado pela paciência.",
        "is_active": True,
    },
    {
        "trigger": "end_service",
        "title": "Término de atendimento",
        "message": "Seu atendimento foi finalizado. Caso precise de mais alguma coisa, estamos à disposição. Até breve!",
        "is_active": True,
    },
    {
        "trigger": "out_of_hours",
        "title": "Fora do expediente",
        "message": "*Seja bem-vind@ à Brasileiros no Atacama!* 🌵\n\nNossa equipe comercial responde em horários específicos para garantir um atendimento ágil e de qualidade. No momento, estamos fora do horário, mas retornaremos sua mensagem o mais breve possível!",
        "is_active": True,
    },
    {
        "trigger": "break_time",
        "title": "Intervalo de atendimento",
        "message": "",
        "is_active": False,
    },
    {
        "trigger": "paused",
        "title": "Status em pausa",
        "message": "Estamos em uma breve pausa no momento, mas logo retornaremos para continuar seu atendimento. Obrigado pela compreensão!",
        "is_active": True,
    },
)


class BusinessHours(Base):
    """Horário comercial — expediente da empresa por dia da semana."""
    __tablename__ = "business_hours"

    id         = Column(Integer, primary_key=True, index=True)
    weekday    = Column(Integer, unique=True, nullable=False)  # 0=segunda, 1=terça, ..., 6=domingo
    is_open    = Column(Boolean, default=True, nullable=False)
    open_time  = Column(String(5), nullable=True)   # "13:00"
    close_time = Column(String(5), nullable=True)   # "19:00"

    def __repr__(self):
        days = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
        return f"<BusinessHours({days[self.weekday]}: {'Aberto' if self.is_open else 'Fechado'})>"
