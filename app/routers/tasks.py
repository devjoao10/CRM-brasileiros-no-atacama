from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import nullslast
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, date, time, timezone

from app.database import get_db
from app.models.lead import Lead
from app.models.task import Task, TaskStatus, TaskType
from app.schemas.task import TaskCreate, TaskUpdate, TaskResponse
from app.auth import get_current_user, require_admin
from app.models.user import User, UserRole


def _assert_can_set_owner(current_user: User) -> None:
    """AUDIT-2026-08-W2G (F2): só admin escolhe o dono de uma tarefa.

    `user_id` vinha do corpo e era aceito de qualquer autenticado, tanto no
    create quanto no update — e o update dava `setattr` cego em todos os campos
    DEPOIS de checar o dono da linha atual. Resultado: um não-admin empurrava a
    própria tarefa para outra pessoa e perdia o acesso a ela sem volta.
    Mantido para admin porque a IA cria tarefa sem dono (user_id=None) e o
    gestor precisa delegar.
    """
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=403,
            detail="Apenas administradores podem definir o responsável de uma tarefa.",
        )


def _assert_lead_exists(db: Session, lead_id: Optional[int]) -> None:
    """AUDIT-2026-08-W2G (F2): lead_id inexistente batia na FK e virava 500.

    O contrato da API é 404 para referência que não existe — o 500 ainda deixava
    a transação abortada para o resto do request.
    """
    if lead_id is None:
        return
    if not db.query(Lead.id).filter(Lead.id == lead_id).first():
        raise HTTPException(status_code=404, detail=f"Lead {lead_id} não encontrado")

router = APIRouter(prefix="/api/tasks", tags=["Tarefas"])

@router.get("", response_model=List[TaskResponse], summary="Listar tarefas")
def list_tasks(
    status: Optional[TaskStatus] = Query(None, description="Filtrar por status"),
    tipo: Optional[TaskType] = Query(None, description="Filtrar por tipo (manual/automatica)"),
    due_date: Optional[date] = Query(None, description="Filtrar por data exata (YYYY-MM-DD)"),
    overdue: Optional[bool] = Query(False, description="Mostrar apenas atrasadas"),
    user_filter: Optional[int] = Query(None, description="Filtrar por ID do usuário (apenas Admin)"),
    skip: int = Query(0, ge=0, description="Registros para pular"),
    limit: int = Query(100, ge=1, le=500, description="Máximo de registros"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(Task)

    # Filtering by user. Admin can see all, otherwise only see own.
    if current_user.role != UserRole.ADMIN:
        query = query.filter(Task.user_id == current_user.id)
    else:
        if user_filter is not None:
            if user_filter == 0:
                query = query.filter(Task.user_id.is_(None))  # AI tasks
            else:
                query = query.filter(Task.user_id == user_filter)

    if status:
        query = query.filter(Task.status == status)
    
    if tipo:
        query = query.filter(Task.tipo == tipo)

    # AUDIT-2026-08-W2G (F10): `Task.data_vencimento` é DateTime(timezone=True),
    # mas os limites eram datetimes NAIVE. O PostgreSQL resolve o literal naive
    # pelo TimeZone da sessão, então o filtro por dia e o KPI de atraso incluíam
    # ou excluíam algumas horas de tarefas conforme o fuso do servidor. Limites
    # e "agora" passam a ser explicitamente UTC.
    if due_date:
        query = query.filter(Task.data_vencimento >= datetime.combine(due_date, time.min, tzinfo=timezone.utc))
        query = query.filter(Task.data_vencimento <= datetime.combine(due_date, time.max, tzinfo=timezone.utc))

    if overdue:
        query = query.filter(Task.data_vencimento < datetime.now(timezone.utc), Task.status != TaskStatus.CONCLUIDO, Task.status != TaskStatus.CANCELADO)

    # AUDIT-2026-08-W2G (F11): paginação instável. `data_vencimento` é nullable e
    # não é única — sem desempate a mesma tarefa aparecia em duas páginas (ou em
    # nenhuma) entre chamadas idênticas. `nullslast` fixa o lugar dos NULLs, que
    # por padrão vêm primeiro no SQLite e por último no PostgreSQL — exatamente o
    # par dev/prod em que `app/database.py` se ramifica.
    tasks = (
        query.order_by(nullslast(Task.data_vencimento.asc()), Task.id.asc())
        .offset(skip).limit(limit).all()
    )
    return tasks

@router.post("", response_model=TaskResponse, status_code=201, summary="Criar tarefa")
def create_task(
    data: TaskCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Se user_id for explicitamente enviado (inclusive None para IA), use-o —
    # mas só admin pode fazer isso (AUDIT-2026-08-W2G / F2). Sem dono explícito,
    # a tarefa é de quem a criou.
    if "user_id" in data.model_dump(exclude_unset=True):
        _assert_can_set_owner(current_user)
        final_user_id = data.user_id
    else:
        final_user_id = current_user.id

    _assert_lead_exists(db, data.lead_id)

    task_data = data.model_dump(exclude={"user_id"})
    new_task = Task(**task_data, user_id=final_user_id)
    
    db.add(new_task)
    db.commit()
    db.refresh(new_task)
    
    return new_task

@router.put("/{task_id}", response_model=TaskResponse, summary="Atualizar tarefa")
def update_task(
    task_id: int,
    data: TaskUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
        
    if task.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Você não tem permissão para editar esta tarefa.")

    update_data = data.model_dump(exclude_unset=True)

    # AUDIT-2026-08-W2G (F2): reatribuição de dono e troca de lead precisam ser
    # checadas ANTES do setattr — a checagem acima só olha a linha atual.
    if "user_id" in update_data and update_data["user_id"] != task.user_id:
        _assert_can_set_owner(current_user)
    if "lead_id" in update_data:
        _assert_lead_exists(db, update_data["lead_id"])

    for field, value in update_data.items():
        setattr(task, field, value)

    db.commit()
    db.refresh(task)

    return task

@router.delete("/{task_id}", summary="Excluir tarefa")
def delete_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
        
    if task.user_id != current_user.id and current_user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Você não tem permissão para apagar esta tarefa.")

    db.delete(task)
    db.commit()

    return {"message": "Tarefa apagada com sucesso"}
