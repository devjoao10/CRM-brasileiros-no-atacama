from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime
from app.models.task import TaskStatus, TaskType

class TaskBase(BaseModel):
    titulo: str
    descricao: Optional[str] = None
    data_vencimento: Optional[datetime] = None
    status: TaskStatus = TaskStatus.PENDENTE
    tipo: TaskType = TaskType.MANUAL
    lead_id: Optional[int] = None
    google_calendar_event_id: Optional[str] = None
    google_calendar_link: Optional[str] = None

class TaskCreate(TaskBase):
    pass
    # user_id is automatically assigned by the backend based on current user

class TaskUpdate(BaseModel):
    titulo: Optional[str] = None
    descricao: Optional[str] = None
    data_vencimento: Optional[datetime] = None
    status: Optional[TaskStatus] = None
    tipo: Optional[TaskType] = None
    lead_id: Optional[int] = None
    google_calendar_event_id: Optional[str] = None
    google_calendar_link: Optional[str] = None

class TaskResponse(TaskBase):
    id: int
    user_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
