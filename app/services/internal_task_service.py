import calendar
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.internal_task import InternalTask
from app.models.user import User
from app.services.operational_notification_service import OperationalNotificationService


def _add_month(d: date) -> date:
    """Próximo mês calendário, com clamp do dia (31/jan -> 28/fev)."""
    year = d.year + (1 if d.month == 12 else 0)
    month = 1 if d.month == 12 else d.month + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


class InternalTaskService:
    """Regras de negócio das pendências internas (WP-GI).

    - effective_status derivado (nunca gravar 'atrasada') — coerência §8.6.
    - Recorrência MVP "rolling": concluir avança due_date, sem duplicar linhas.
    - Notificação no sino global (reuso de operational_notifications) quando a
      pendência é criada para outra pessoa (GI-04).
    """

    def __init__(self, db: Session):
        self.db = db

    # ── Queries ──────────────────────────────────────────────────────
    def list_tasks(self, assignee_id: Optional[int] = None,
                   include_archived: bool = False) -> List[InternalTask]:
        q = self.db.query(InternalTask)
        if not include_archived:
            q = q.filter(InternalTask.is_archived.is_(False))
        if assignee_id is not None:
            q = q.filter(InternalTask.assignee_id == assignee_id)
        return q.order_by(InternalTask.due_date.is_(None),
                          InternalTask.due_date.asc(),
                          InternalTask.created_at.desc()).all()

    def get_task(self, task_id: int) -> InternalTask:
        task = self.db.query(InternalTask).get(task_id)
        if not task:
            raise ValueError(f"Pendência {task_id} não encontrada")
        return task

    # ── Permissões ───────────────────────────────────────────────────
    @staticmethod
    def _can_manage(task: InternalTask, user: User) -> bool:
        return user.role == "admin" or user.id in (task.created_by, task.assignee_id)

    # ── Comandos ─────────────────────────────────────────────────────
    def create_task(self, data: dict, current_user: User) -> InternalTask:
        assignee = self.db.query(User).get(data["assignee_id"])
        if not assignee or not assignee.is_active:
            raise ValueError("Responsável inválido ou inativo")

        task = InternalTask(created_by=current_user.id, **data)
        self.db.add(task)
        self.db.flush()

        # GI-04: notifica o responsável no sino global (se não for o criador)
        if assignee.id != current_user.id:
            OperationalNotificationService(self.db).create_notification(
                user_id=assignee.id,
                card_id=None,
                event_type="internal_task",
                message=f"{current_user.nome} criou uma pendência para você: {task.title}",
            )

        self.db.commit()
        self.db.refresh(task)
        return task

    def update_task(self, task_id: int, changes: dict, current_user: User) -> InternalTask:
        task = self.get_task(task_id)
        if not self._can_manage(task, current_user):
            raise PermissionError("Sem permissão para editar esta pendência")
        if "assignee_id" in changes and changes["assignee_id"] is not None:
            assignee = self.db.query(User).get(changes["assignee_id"])
            if not assignee or not assignee.is_active:
                raise ValueError("Responsável inválido ou inativo")
        for field, value in changes.items():
            setattr(task, field, value)
        self.db.commit()
        self.db.refresh(task)
        return task

    def complete_task(self, task_id: int, current_user: User) -> InternalTask:
        task = self.get_task(task_id)
        if not self._can_manage(task, current_user):
            raise PermissionError("Sem permissão para concluir esta pendência")

        task.last_completed_at = datetime.now(timezone.utc)
        if task.task_type == "recorrente" and task.recurrence:
            # rolling: avança a próxima ocorrência a partir de hoje/due, mantém pendente
            base = max(task.due_date or date.today(), date.today())
            if task.recurrence == "diaria":
                task.due_date = base + timedelta(days=1)
            elif task.recurrence == "semanal":
                task.due_date = base + timedelta(days=7)
            else:  # mensal
                task.due_date = _add_month(base)
            task.status = "pendente"
        else:
            task.status = "concluida"

        self.db.commit()
        self.db.refresh(task)
        return task

    def archive_task(self, task_id: int, current_user: User) -> InternalTask:
        task = self.get_task(task_id)
        if not (current_user.role == "admin" or current_user.id == task.created_by):
            raise PermissionError("Apenas o criador ou um admin pode arquivar")
        task.is_archived = True
        self.db.commit()
        self.db.refresh(task)
        return task
