from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_user
from app.models.user import User
from app.schemas.operational import MyPendingResponse
from app.services.operational_pending_service import OperationalPendingService

router = APIRouter(
    prefix="/api/operational/my-pending",
    tags=["Operational Pending"]
)


@router.get("", response_model=MyPendingResponse)
def get_my_pending(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalPendingService(db)
    try:
        user_id = getattr(current_user, "id", None)
        return service.get_my_pending(user_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao obter minhas pendências operacionais"
        )
