from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.internal_task import (
    InternalTaskCreate, InternalTaskResponse, InternalTaskUpdate,
)
from app.services.internal_task_service import InternalTaskService

router = APIRouter(prefix="/api/internal/tasks", tags=["Gestão Interna"])


def _service(db: Session = Depends(get_db)) -> InternalTaskService:
    return InternalTaskService(db)


@router.get("", response_model=List[InternalTaskResponse])
def list_internal_tasks(
    assignee_id: Optional[int] = None,
    include_archived: bool = False,
    service: InternalTaskService = Depends(_service),
    current_user: User = Depends(get_current_user),
):
    tasks = service.list_tasks(assignee_id=assignee_id, include_archived=include_archived)
    return [InternalTaskResponse.from_task(t) for t in tasks]


@router.post("", response_model=InternalTaskResponse, status_code=status.HTTP_201_CREATED)
def create_internal_task(
    data: InternalTaskCreate,
    service: InternalTaskService = Depends(_service),
    current_user: User = Depends(get_current_user),
):
    try:
        task = service.create_task(data.model_dump(), current_user)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return InternalTaskResponse.from_task(task)


@router.patch("/{task_id}", response_model=InternalTaskResponse)
def update_internal_task(
    task_id: int,
    data: InternalTaskUpdate,
    service: InternalTaskService = Depends(_service),
    current_user: User = Depends(get_current_user),
):
    try:
        task = service.update_task(task_id, data.model_dump(exclude_unset=True), current_user)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return InternalTaskResponse.from_task(task)


@router.post("/{task_id}/complete", response_model=InternalTaskResponse)
def complete_internal_task(
    task_id: int,
    service: InternalTaskService = Depends(_service),
    current_user: User = Depends(get_current_user),
):
    try:
        task = service.complete_task(task_id, current_user)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return InternalTaskResponse.from_task(task)


@router.post("/{task_id}/archive", response_model=InternalTaskResponse)
def archive_internal_task(
    task_id: int,
    service: InternalTaskService = Depends(_service),
    current_user: User = Depends(get_current_user),
):
    try:
        task = service.archive_task(task_id, current_user)
    except PermissionError as e:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return InternalTaskResponse.from_task(task)
