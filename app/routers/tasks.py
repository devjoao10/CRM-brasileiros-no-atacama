from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, date

from app.database import get_db
from app.models.task import Task, TaskStatus, TaskType
from app.schemas.task import TaskCreate, TaskUpdate, TaskResponse
from app.auth import get_current_user, require_admin
from app.models.user import User, UserRole

router = APIRouter(prefix="/api/tasks", tags=["Tarefas"])

@router.get("", response_model=List[TaskResponse], summary="Listar tarefas")
async def list_tasks(
    status: Optional[TaskStatus] = Query(None, description="Filtrar por status"),
    tipo: Optional[TaskType] = Query(None, description="Filtrar por tipo (manual/automatica)"),
    due_date: Optional[date] = Query(None, description="Filtrar por data exata (YYYY-MM-DD)"),
    overdue: Optional[bool] = Query(False, description="Mostrar apenas atrasadas"),
    user_filter: Optional[int] = Query(None, description="Filtrar por ID do usuário (apenas Admin)"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(Task)

    # Filtering by user. Admin can see all, otherwise only see own.
    if current_user.role != UserRole.ADMIN:
        query = query.filter(Task.user_id == current_user.id)
    else:
        if user_filter:
            query = query.filter(Task.user_id == user_filter)

    if status:
        query = query.filter(Task.status == status)
    
    if tipo:
        query = query.filter(Task.tipo == tipo)

    if due_date:
        query = query.filter(Task.data_vencimento >= datetime.combine(due_date, datetime.min.time()))
        query = query.filter(Task.data_vencimento <= datetime.combine(due_date, datetime.max.time()))

    if overdue:
        query = query.filter(Task.data_vencimento < datetime.now(), Task.status != TaskStatus.CONCLUIDO, Task.status != TaskStatus.CANCELADO)

    # Order by due date
    tasks = query.order_by(Task.data_vencimento.asc()).all()
    return tasks

@router.post("", response_model=TaskResponse, status_code=201, summary="Criar tarefa")
async def create_task(
    data: TaskCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    new_task = Task(**data.model_dump(), user_id=current_user.id)
    
    db.add(new_task)
    db.commit()
    db.refresh(new_task)
    
    return new_task

@router.put("/{task_id}", response_model=TaskResponse, summary="Atualizar tarefa")
async def update_task(
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
    for field, value in update_data.items():
        setattr(task, field, value)

    db.commit()
    db.refresh(task)

    return task

@router.delete("/{task_id}", summary="Excluir tarefa")
async def delete_task(
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
