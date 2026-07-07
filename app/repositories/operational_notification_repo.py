from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from app.models.operational.notification import OperationalNotification


class OperationalNotificationRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_notification(self, notification_id: int) -> Optional[OperationalNotification]:
        return self.db.query(OperationalNotification).filter(OperationalNotification.id == notification_id).first()

    def list_notifications_by_user(self, user_id: int, only_unread: bool = False) -> List[OperationalNotification]:
        query = self.db.query(OperationalNotification).filter(OperationalNotification.user_id == user_id)
        if only_unread:
            query = query.filter(OperationalNotification.read_at == None)
        return query.order_by(OperationalNotification.created_at.desc()).all()

    def create_notification(self, data: dict) -> OperationalNotification:
        notification = OperationalNotification(**data)
        self.db.add(notification)
        return notification

    def mark_notification_as_read(self, notification: OperationalNotification) -> OperationalNotification:
        notification.read_at = datetime.now()
        return notification

    def mark_all_as_read(self, user_id: int):
        self.db.query(OperationalNotification).filter(
            OperationalNotification.user_id == user_id,
            OperationalNotification.read_at == None
        ).update({"read_at": datetime.now()}, synchronize_session=False)
