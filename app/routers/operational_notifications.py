from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.auth import get_current_user
from app.models.user import User
from app.schemas.operational import NotificationResponse
from app.services.operational_notification_service import OperationalNotificationService

router = APIRouter(
    prefix="/api/operational/notifications",
    tags=["Operational Notifications"]
)


@router.get("", response_model=List[NotificationResponse])
def list_notifications(
    only_unread: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalNotificationService(db)
    try:
        user_id = getattr(current_user, "id", None)
        return service.list_notifications(user_id, only_unread)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao listar notificações"
        )


@router.post("/{notification_id}/read", response_model=NotificationResponse)
def mark_notification_as_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalNotificationService(db)
    try:
        user_id = getattr(current_user, "id", None)
        return service.mark_as_read(notification_id, user_id)
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
            detail="Erro inesperado ao marcar notificação como lida"
        )


@router.post("/read-all")
def mark_all_notifications_as_read(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalNotificationService(db)
    try:
        user_id = getattr(current_user, "id", None)
        service.mark_all_as_read(user_id)
        return {"detail": "Todas as notificações foram marcadas como lidas"}
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro inesperado ao marcar todas as notificações como lidas"
        )
