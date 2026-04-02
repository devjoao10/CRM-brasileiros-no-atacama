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
    data_vencimento = Column(DateTime(timezone=True), nullable=True)
    
    status = Column(SAEnum(TaskStatus), default=TaskStatus.PENDENTE, nullable=False)
    tipo = Column(SAEnum(TaskType), default=TaskType.MANUAL, nullable=False)
    
    google_calendar_event_id = Column(String(255), nullable=True)
    google_calendar_link = Column(String(500), nullable=True)
    
    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Foreign Keys
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=True)

    # Relationships
    user = relationship("app.models.user.User", backref="tasks")
    lead = relationship("app.models.lead.Lead", backref="tasks")

    def __repr__(self):
        return f"<Task(id={self.id}, titulo='{self.titulo}', status='{self.status}')>"
