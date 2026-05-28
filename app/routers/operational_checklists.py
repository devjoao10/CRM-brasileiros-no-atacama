from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.auth import get_current_user
from app.models.user import User
from app.schemas.operational import (
    ChecklistCreate,
    ChecklistResponse,
    ChecklistItemCreate,
    ChecklistItemResponse,
    ChecklistItemUpdate,
)
from app.services.operational_checklist_service import OperationalChecklistService

router = APIRouter(
    prefix="/api/operational",
    tags=["Operational Checklists"]
)


@router.get("/cards/{card_id}/checklists", response_model=List[ChecklistResponse])
def list_checklists_by_card(
    card_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalChecklistService(db)
    try:
        return service.list_checklists_by_card(card_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao listar checklists do card"
        )


@router.post("/cards/{card_id}/checklists", response_model=ChecklistResponse, status_code=status.HTTP_201_CREATED)
def create_checklist(
    card_id: int,
    checklist_data: ChecklistCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalChecklistService(db)
    try:
        data_dict = checklist_data.model_dump(exclude_unset=True)
        # Força card_id vindo do path parameter
        data_dict["card_id"] = card_id
        return service.create_checklist(card_id, data_dict, current_user)
    except ValueError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro inesperado ao criar checklist"
        )


@router.get("/checklists/{checklist_id}/items", response_model=List[ChecklistItemResponse])
def list_checklist_items(
    checklist_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalChecklistService(db)
    try:
        return service.list_checklist_items(checklist_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao listar itens do checklist"
        )


@router.post("/checklists/{checklist_id}/items", response_model=ChecklistItemResponse, status_code=status.HTTP_201_CREATED)
def create_checklist_item(
    checklist_id: int,
    item_data: ChecklistItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalChecklistService(db)
    try:
        data_dict = item_data.model_dump(exclude_unset=True)
        # Força checklist_id vindo do path parameter
        data_dict["checklist_id"] = checklist_id
        return service.create_checklist_item(checklist_id, data_dict, current_user)
    except ValueError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro inesperado ao criar item de checklist"
        )


@router.put("/checklist-items/{item_id}", response_model=ChecklistItemResponse)
def update_checklist_item(
    item_id: int,
    item_data: ChecklistItemUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalChecklistService(db)
    try:
        data_dict = item_data.model_dump(exclude_unset=True)
        return service.update_checklist_item(item_id, data_dict, current_user)
    except ValueError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro inesperado ao atualizar item de checklist"
        )


@router.post("/checklist-items/{item_id}/toggle", response_model=ChecklistItemResponse)
def toggle_checklist_item(
    item_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalChecklistService(db)
    try:
        return service.toggle_checklist_item(item_id, current_user)
    except ValueError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro inesperado ao marcar/desmarcar item de checklist"
        )
