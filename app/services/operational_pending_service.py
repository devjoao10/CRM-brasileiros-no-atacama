from sqlalchemy.orm import Session
from typing import Dict, List
from app.models.operational.card import OperationalCard, OperationalCardAssignee
from app.models.operational.notification import OperationalNotification


class OperationalPendingService:
    def __init__(self, db: Session):
        self.db = db

    def get_my_pending(self, user_id: int) -> Dict:
        # 1. Cards atribuídos a mim (não arquivados)
        assigned_cards = self.db.query(OperationalCard).join(
            OperationalCardAssignee,
            OperationalCardAssignee.card_id == OperationalCard.id
        ).filter(
            OperationalCardAssignee.user_id == user_id,
            OperationalCard.is_archived == False
        ).order_by(OperationalCard.due_date.asc()).all()

        # 2. Notificações não lidas
        unread_notifications = self.db.query(OperationalNotification).filter(
            OperationalNotification.user_id == user_id,
            OperationalNotification.read_at == None
        ).order_by(OperationalNotification.created_at.desc()).all()

        return {
            "assigned_cards": assigned_cards,
            "unread_notifications": unread_notifications
        }
