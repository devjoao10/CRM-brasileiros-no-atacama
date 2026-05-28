from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.auth import get_current_user
from app.models.user import User
from app.schemas.operational import (
    CardMoveRequest,
    CardMovementResponse,
    ActivityLogResponse,
)
from app.services.operational_flow_service import OperationalFlowService

router = APIRouter(
    prefix="/api/operational",
    tags=["Operational Flow"]
)


# ==============================================================================
# --- Move Card Endpoint ---
# ==============================================================================

@router.post("/cards/{card_id}/move", response_model=CardMovementResponse)
def move_operational_card(
    card_id: int,
    move_data: CardMoveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalFlowService(db)
    try:
        movement = service.move_card(card_id, move_data.to_list_id, current_user)

        # Se não houve mudança real (card já está na mesma posição),
        # o service retorna None. Retornamos 200 com mensagem informativa.
        if movement is None:
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content={
                    "status": "no_change",
                    "detail": "Card já se encontra na posição informada. Nenhuma movimentação registrada.",
                },
            )

        return movement
    except HTTPException:
        # Re-lançar HTTPExceptions já tratadas (como o caso de "já está na posição")
        raise
    except ValueError as e:
        db.rollback()
        error_msg = str(e)
        # Distinguir entre "não encontrado" e "regra de negócio"
        if "não encontrado" in error_msg or "não encontrada" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error_msg
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg
        )
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro inesperado ao mover card operacional"
        )


# ==============================================================================
# --- Movement History Endpoint ---
# ==============================================================================

@router.get("/cards/{card_id}/movements", response_model=List[CardMovementResponse])
def list_card_movements(
    card_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalFlowService(db)
    try:
        return service.list_movements(card_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao listar movimentações do card"
        )


# ==============================================================================
# --- Activity Log Endpoint ---
# ==============================================================================

@router.get("/cards/{card_id}/activity-logs", response_model=List[ActivityLogResponse])
def list_card_activity_logs(
    card_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalFlowService(db)
    try:
        return service.list_activity_logs(card_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao listar activity logs do card"
        )
