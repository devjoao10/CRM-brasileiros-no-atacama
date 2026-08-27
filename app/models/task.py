from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, ForeignKey, Enum as SAEnum
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import enum

from app.database import Base

class TaskStatus(str, enum.Enum):
    PENDENTE = "pendente"
    EM_ANDAMENTO = "em_andamento"
    CONCLUIDO = "concluido"
    CANCELADO = "cancelado"

class TaskType(str, enum.Enum):
    MANUAL = "manual"
    AUTOMATICA = "automatica"

class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    titulo = Column(String(200), nullable=False)
    descricao = Column(Text, nullable=True)
    data_vencimento = Column(DateTime(timezone=True), nullable=True, index=True)
    
    status = Column(SAEnum(TaskStatus), default=TaskStatus.PENDENTE, nullable=False, index=True)
    tipo = Column(SAEnum(TaskType), default=TaskType.MANUAL, nullable=False)
    
    google_calendar_event_id = Column(String(255), nullable=True)
    google_calendar_link = Column(String(500), nullable=True)
    resultado_ia = Column(Text, nullable=True)
    
    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Foreign Keys
    # AUDIT-2026-08-W2E (F6) — eram as DUAS unicas FKs do app sem `ondelete`.
    # Sem isso o banco BLOQUEIA o delete do pai em vez de propagar, e o unico
    # lugar que compensava era routers/leads.py:533-535 apagando tasks na mao:
    # qualquer hard delete por outro caminho (admin, script, cascade de outro
    # objeto) morria com violacao de FK. `SET NULL` no dono (a tarefa sobrevive
    # ao usuario que saiu) e `CASCADE` no lead (tarefa sem lead nao existe).
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=True, index=True)

    # Relationships
    user = relationship("app.models.user.User", backref="tasks")
    lead = relationship("app.models.lead.Lead", backref="tasks")

    def __repr__(self):
        return f"<Task(id={self.id}, titulo='{self.titulo}', status='{self.status}')>"
