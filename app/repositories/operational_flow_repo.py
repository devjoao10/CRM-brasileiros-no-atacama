from sqlalchemy.orm import Session
from typing import List
from app.models.operational.notification import OperationalActivityLog, OperationalCardMovement


class OperationalFlowRepository:
    def __init__(self, db: Session):
        self.db = db

    def create_movement(self, data: dict) -> OperationalCardMovement:
        movement = OperationalCardMovement(**data)
        self.db.add(movement)
        return movement

    def create_activity_log(self, data: dict) -> OperationalActivityLog:
        log = OperationalActivityLog(**data)
        self.db.add(log)
        return log

    def list_movements_by_card(self, card_id: int) -> List[OperationalCardMovement]:
        return self.db.query(OperationalCardMovement).filter(
            OperationalCardMovement.card_id == card_id
        ).order_by(OperationalCardMovement.created_at.asc()).all()

    def list_activity_logs_by_card(self, card_id: int) -> List[OperationalActivityLog]:
        return self.db.query(OperationalActivityLog).filter(
            OperationalActivityLog.card_id == card_id
        ).order_by(OperationalActivityLog.created_at.asc()).all()
