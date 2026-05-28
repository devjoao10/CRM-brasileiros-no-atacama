from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.auth import get_current_user, require_admin
from app.models.user import User
from app.schemas.operational import (
    CardCreate,
    CardUpdate,
    CardResponse,
    CardAssigneeResponse,
    CardFieldDefinitionCreate,
    CardFieldDefinitionResponse,
    CardFieldValueCreate,
    CardFieldValueResponse,
)
from app.services.operational_card_service import OperationalCardService

router = APIRouter(
    prefix="/api/operational",
    tags=["Operational Cards"]
)


# ==============================================================================
# --- Card Endpoints ---
# ==============================================================================

@router.get("/boards/{board_id}/cards", response_model=List[CardResponse])
def list_cards_by_board(
    board_id: int,
    include_archived: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalCardService(db)
    try:
        return service.list_cards_by_board(board_id, include_archived)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao listar cards do quadro"
        )


@router.get("/lists/{list_id}/cards", response_model=List[CardResponse])
def list_cards_by_list(
    list_id: int,
    include_archived: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalCardService(db)
    try:
        return service.list_cards_by_list(list_id, include_archived)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao listar cards da coluna"
        )


@router.post("/cards", response_model=CardResponse, status_code=status.HTTP_201_CREATED)
def create_operational_card(
    card_data: CardCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalCardService(db)
    try:
        data_dict = card_data.model_dump()
        card = service.create_card(data_dict, current_user)
        return card
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
            detail="Erro inesperado ao criar card operacional"
        )


@router.get("/cards/{card_id}", response_model=CardResponse)
def get_operational_card(
    card_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalCardService(db)
    try:
        return service.get_card_or_404(card_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao buscar card operacional"
        )


@router.put("/cards/{card_id}", response_model=CardResponse)
def update_operational_card(
    card_id: int,
    card_data: CardUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalCardService(db)
    try:
        data_dict = card_data.model_dump(exclude_unset=True)
        card = service.update_card(card_id, data_dict, current_user)
        return card
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
            detail="Erro inesperado ao atualizar card operacional"
        )


@router.post("/cards/{card_id}/archive", response_model=CardResponse)
def archive_operational_card(
    card_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # TODO: Futuramente, aplicar regra admin + responsável para arquivamento.
    # Por enquanto, qualquer membro autenticado pode arquivar colaborativamente.
    service = OperationalCardService(db)
    try:
        card = service.archive_card(card_id, current_user)
        return card
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
            detail="Erro inesperado ao arquivar card operacional"
        )


# ==============================================================================
# --- Assignee Endpoints ---
# ==============================================================================

@router.get("/cards/{card_id}/assignees", response_model=List[CardAssigneeResponse])
def list_card_assignees(
    card_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalCardService(db)
    try:
        service.get_card_or_404(card_id)
        return service.card_repo.list_assignees(card_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao listar responsáveis do card"
        )


@router.post("/cards/{card_id}/assignees/{user_id}", response_model=CardAssigneeResponse)
def add_card_assignee(
    card_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalCardService(db)
    try:
        assignee = service.add_assignee(card_id, user_id, current_user)
        return assignee
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
            detail="Erro inesperado ao atribuir responsável ao card"
        )


@router.post("/cards/{card_id}/assignees/{user_id}/remove")
def remove_card_assignee(
    card_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalCardService(db)
    try:
        service.remove_assignee(card_id, user_id, current_user)
        return {"status": "removed", "card_id": card_id, "user_id": user_id}
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
            detail="Erro inesperado ao remover responsável do card"
        )


# ==============================================================================
# --- Field Definition Endpoints ---
# ==============================================================================

@router.get("/boards/{board_id}/field-definitions", response_model=List[CardFieldDefinitionResponse])
def list_field_definitions(
    board_id: int,
    include_archived: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalCardService(db)
    try:
        return service.list_field_definitions(board_id, include_archived)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao listar definições de campos"
        )


@router.post("/field-definitions", response_model=CardFieldDefinitionResponse, status_code=status.HTTP_201_CREATED)
def create_field_definition(
    definition_data: CardFieldDefinitionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    service = OperationalCardService(db)
    try:
        data_dict = definition_data.model_dump(exclude_unset=True)
        definition = service.create_field_definition(data_dict, current_user)
        return definition
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
            detail="Erro inesperado ao criar definição de campo"
        )


# ==============================================================================
# --- Field Value Endpoints ---
# ==============================================================================

@router.get("/cards/{card_id}/field-values", response_model=List[CardFieldValueResponse])
def list_field_values(
    card_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalCardService(db)
    try:
        return service.list_field_values(card_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao listar valores de campos do card"
        )


@router.post("/cards/{card_id}/field-values/{definition_id}", response_model=CardFieldValueResponse)
def set_card_field_value(
    card_id: int,
    definition_id: int,
    value_data: CardFieldValueCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalCardService(db)
    try:
        # Converter os campos separados do schema para o formato
        # esperado pelo service (dict com chave "value" genérica).
        # O service internamente redireciona para value_text/value_date/value_boolean
        # com base no field_type da definição.
        value_dict = value_data.model_dump()

        # O service.set_field_value espera value_data com chave "value".
        # Como o schema já traz os valores separados, passamos diretamente
        # e deixamos o service identificar qual campo preencher via definition.field_type.
        # Selecionar explicitamente o primeiro campo não-None,
        # preservando boolean False e string vazia como valores válidos:
        generic_value = None
        for key in ("value_text", "value_date", "value_boolean"):
            val = value_dict.get(key)
            if val is not None:
                generic_value = val
                break
        service_data = {"value": generic_value}

        result = service.set_field_value(card_id, definition_id, service_data, current_user)
        return result
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
            detail="Erro inesperado ao definir valor de campo"
        )
