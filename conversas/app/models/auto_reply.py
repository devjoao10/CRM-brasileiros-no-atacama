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
