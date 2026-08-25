from pydantic import BaseModel, ConfigDict
from typing import Optional
from datetime import datetime
from app.models.task import TaskStatus, TaskType

class TaskBase(BaseModel):
    """Campos que o CHAMADOR da API pode definir.

    AUDIT-2026-08-W2G: `google_calendar_event_id`, `google_calendar_link` e
    `resultado_ia` saíram daqui de propósito — são gravados pela automação
    (sync de agenda / agente de IA), nunca pelo corpo de um POST/PUT. Enquanto
    estavam no schema de entrada, qualquer usuário autenticado podia forjar o
    resultado de uma execução de IA ou um link de agenda. Continuam no
    `TaskResponse` porque a leitura deles é legítima.
    """
    titulo: str
    descricao: Optional[str] = None
    data_vencimento: Optional[datetime] = None
    status: TaskStatus = TaskStatus.PENDENTE
    tipo: TaskType = TaskType.MANUAL
    lead_id: Optional[int] = None

class TaskCreate(TaskBase):
    # AUDIT-2026-08-W2G: `user_id` continua aceito porque a IA precisa criar
    # tarefa sem dono (user_id=None) e o admin precisa delegar — mas quem NÃO é
    # admin recebe 403 no router (`create_task`). O comentário anterior dizia
    # que o backend atribuía o dono sozinho; era falso, e a mentira escondia a
    # falha de autorização.
    user_id: Optional[int] = None

class TaskUpdate(BaseModel):
    titulo: Optional[str] = None
    descricao: Optional[str] = None
    data_vencimento: Optional[datetime] = None
    status: Optional[TaskStatus] = None
    tipo: Optional[TaskType] = None
    lead_id: Optional[int] = None
    # AUDIT-2026-08-W2G: idem — só admin pode reatribuir. Antes, o router
    # checava o dono da linha ATUAL e depois dava setattr em tudo, inclusive
    # user_id: o não-admin empurrava a própria tarefa para outro usuário e
    # perdia o acesso a ela de forma irreversível.
    user_id: Optional[int] = None

class TaskResponse(TaskBase):
    id: int
    user_id: Optional[int] = None
    google_calendar_event_id: Optional[str] = None
    google_calendar_link: Optional[str] = None
    resultado_ia: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
