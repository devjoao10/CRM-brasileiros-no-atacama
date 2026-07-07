from sqlalchemy.orm import Session
from typing import List, Optional
from app.repositories.operational_notification_repo import OperationalNotificationRepository
from app.models.operational.notification import OperationalNotification


class OperationalNotificationService:
    def __init__(self, db: Session):
        self.db = db
        self.notification_repo = OperationalNotificationRepository(db)

    def list_notifications(self, user_id: int, only_unread: bool = False) -> List[OperationalNotification]:
        return self.notification_repo.list_notifications_by_user(user_id, only_unread)

    def create_notification(self, user_id: int, card_id: Optional[int], event_type: str, message: str) -> OperationalNotification:
        notification_data = {
            "user_id": user_id,
            "card_id": card_id,
            "event_type": event_type,
            "message": message
        }
        notification = self.notification_repo.create_notification(notification_data)
        # Flush to obtain ID, but commit is handled by the calling service or endpoint
        self.db.flush()
        return notification

    def mark_as_read(self, notification_id: int, user_id: int) -> OperationalNotification:
        notification = self.notification_repo.get_notification(notification_id)
        if not notification:
            raise ValueError(f"Notificação com ID {notification_id} não encontrada")
        
        # Regra de segurança: Usuário só pode marcar suas próprias notificações
        if notification.user_id != user_id:
            raise ValueError("Você não tem permissão para alterar esta notificação")

        self.notification_repo.mark_notification_as_read(notification)
        self.db.commit()
        self.db.refresh(notification)
        return notification

    def mark_all_as_read(self, user_id: int):
        self.notification_repo.mark_all_as_read(user_id)
        self.db.commit()
