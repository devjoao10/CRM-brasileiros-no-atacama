from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field, model_validator

RECURRENCES = {"diaria", "semanal", "mensal"}
PRIORITIES = {"baixa", "media", "alta"}


class InternalTaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    assignee_id: int = Field(..., description="Responsável pela pendência")
    task_type: str = Field("pontual", pattern="^(pontual|recorrente)$")
    recurrence: Optional[str] = None
    due_date: Optional[date] = None
    priority: Optional[str] = None

    @model_validator(mode="after")
    def _check_rules(self):
        if self.task_type == "recorrente":
            if self.recurrence not in RECURRENCES:
                raise ValueError("Pendência recorrente exige recurrence: diaria, semanal ou mensal")
            if self.due_date is None:
                raise ValueError("Pendência recorrente exige due_date (primeira ocorrência)")
        else:
            self.recurrence = None
        if self.priority is not None and self.priority not in PRIORITIES:
            raise ValueError("priority deve ser baixa, media ou alta")
        return self


class InternalTaskUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    assignee_id: Optional[int] = None
    due_date: Optional[date] = None
    priority: Optional[str] = None

    @model_validator(mode="after")
    def _check_priority(self):
        if self.priority is not None and self.priority not in PRIORITIES:
            raise ValueError("priority deve ser baixa, media ou alta")
        return self


class InternalTaskResponse(BaseModel):
    id: int
    title: str
    description: Optional[str]
    assignee_id: Optional[int]
    assignee_nome: Optional[str]
    created_by: Optional[int]
    creator_nome: Optional[str]
    task_type: str
    recurrence: Optional[str]
    due_date: Optional[date]
    priority: Optional[str]
    status: str                 # armazenado: pendente | concluida
    effective_status: str       # derivado: pendente | atrasada | concluida (§8.6)
    last_completed_at: Optional[datetime]
    is_archived: bool
    created_at: Optional[datetime]

    @classmethod
    def from_task(cls, t) -> "InternalTaskResponse":
        if t.status == "concluida":
            eff = "concluida"
        elif t.due_date and t.due_date < date.today():
            eff = "atrasada"
        else:
            eff = "pendente"
        return cls(
            id=t.id, title=t.title, description=t.description,
            assignee_id=t.assignee_id,
            assignee_nome=t.assignee.nome if t.assignee else None,
            created_by=t.created_by,
            creator_nome=t.creator.nome if t.creator else None,
            task_type=t.task_type, recurrence=t.recurrence,
            due_date=t.due_date, priority=t.priority,
            status=t.status, effective_status=eff,
            last_completed_at=t.last_completed_at,
            is_archived=t.is_archived, created_at=t.created_at,
        )
