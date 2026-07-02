from sqlalchemy import Column, Integer, String, Text, Boolean, Date, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class InternalTask(Base):
    """Pendência interna (Gestão Interna) — domínio próprio, separado dos
    cards operacionais (WP-GI, SDD em docs/wps/WP_GI_01_SDD_GESTAO_INTERNA.md).

    `status` armazenado: pendente | concluida.
    `atrasada` é DERIVADA no service (effective_status) — nunca gravada.
    Recorrência MVP "rolling": concluir uma recorrente avança due_date.
    """
    __tablename__ = "internal_tasks"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)

    assignee_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    task_type = Column(String(20), default="pontual", nullable=False)   # pontual | recorrente
    recurrence = Column(String(20), nullable=True)                       # diaria | semanal | mensal
    due_date = Column(Date, nullable=True, index=True)
    priority = Column(String(10), nullable=True)                         # baixa | media | alta

    status = Column(String(20), default="pendente", nullable=False, index=True)  # pendente | concluida
    last_completed_at = Column(DateTime(timezone=True), nullable=True)
    is_archived = Column(Boolean, default=False, nullable=False, index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    assignee = relationship("User", foreign_keys=[assignee_id])
    creator = relationship("User", foreign_keys=[created_by])

    def __repr__(self):
        return f"<InternalTask(id={self.id}, title='{self.title}', status='{self.status}')>"
