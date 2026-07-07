from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.auth import get_current_user, require_admin
from app.models.user import User
from app.schemas.operational import (
    BoardCreate,
    BoardUpdate,
    BoardResponse,
    ListCreate,
    ListUpdate,
    ListResponse
)
from app.services.operational_board_service import OperationalBoardService

router = APIRouter(
    prefix="/api/operational",
    tags=["Operational Boards"]
)


# ==============================================================================
# --- Board Endpoints ---
# ==============================================================================

@router.get("/boards", response_model=List[BoardResponse])
def list_operational_boards(
    include_archived: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalBoardService(db)
    try:
        return service.list_boards(include_archived=include_archived)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao listar quadros operacionais"
        )


@router.post("/boards", response_model=BoardResponse, status_code=status.HTTP_201_CREATED)
def create_operational_board(
    board_data: BoardCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    service = OperationalBoardService(db)
    try:
        data_dict = board_data.model_dump()
        board = service.create_board(data_dict, current_user)
        return board
    except ValueError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro inesperado ao criar quadro operacional"
        )


@router.get("/boards/{board_id}", response_model=BoardResponse)
def get_operational_board(
    board_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalBoardService(db)
    try:
        return service.get_board_or_404(board_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao buscar quadro operacional"
        )


@router.put("/boards/{board_id}", response_model=BoardResponse)
def update_operational_board(
    board_id: int,
    board_data: BoardUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    service = OperationalBoardService(db)
    try:
        data_dict = board_data.model_dump(exclude_unset=True)
        board = service.update_board(board_id, data_dict, current_user)
        return board
    except ValueError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro inesperado ao atualizar quadro operacional"
        )


@router.post("/boards/{board_id}/archive", response_model=BoardResponse)
def archive_operational_board(
    board_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    service = OperationalBoardService(db)
    try:
        board = service.archive_board(board_id, current_user)
        return board
    except ValueError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro inesperado ao arquivar quadro operacional"
        )


# ==============================================================================
# --- List Endpoints ---
# ==============================================================================

@router.get("/boards/{board_id}/lists", response_model=List[ListResponse])
def list_operational_lists(
    board_id: int,
    include_archived: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalBoardService(db)
    try:
        return service.list_lists_by_board(board_id, include_archived=include_archived)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao listar colunas do quadro"
        )


@router.post("/lists", response_model=ListResponse, status_code=status.HTTP_201_CREATED)
def create_operational_list(
    list_data: ListCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    service = OperationalBoardService(db)
    try:
        data_dict = list_data.model_dump()
        operational_list = service.create_list(data_dict, current_user)
        return operational_list
    except ValueError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro inesperado ao criar coluna"
        )


@router.put("/lists/{list_id}", response_model=ListResponse)
def update_operational_list(
    list_id: int,
    list_data: ListUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    service = OperationalBoardService(db)
    try:
        data_dict = list_data.model_dump(exclude_unset=True)
        operational_list = service.update_list(list_id, data_dict, current_user)
        return operational_list
    except ValueError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro inesperado ao atualizar coluna"
        )


@router.post("/lists/{list_id}/archive", response_model=ListResponse)
def archive_operational_list(
    list_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    service = OperationalBoardService(db)
    try:
        operational_list = service.archive_list(list_id, current_user)
        return operational_list
    except ValueError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro inesperado ao arquivar coluna"
        )
