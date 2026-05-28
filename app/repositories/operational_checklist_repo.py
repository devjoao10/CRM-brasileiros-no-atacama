from sqlalchemy.orm import Session
from typing import List, Optional
from app.models.operational.checklist import OperationalChecklist, OperationalChecklistItem


class OperationalChecklistRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_checklist(self, checklist_id: int) -> Optional[OperationalChecklist]:
        return self.db.query(OperationalChecklist).filter(OperationalChecklist.id == checklist_id).first()

    def list_checklists_by_card(self, card_id: int) -> List[OperationalChecklist]:
        return self.db.query(OperationalChecklist).filter(OperationalChecklist.card_id == card_id).order_by(OperationalChecklist.position).all()

    def create_checklist(self, data: dict) -> OperationalChecklist:
        checklist = OperationalChecklist(**data)
        self.db.add(checklist)
        return checklist

    def get_checklist_item(self, item_id: int) -> Optional[OperationalChecklistItem]:
        return self.db.query(OperationalChecklistItem).filter(OperationalChecklistItem.id == item_id).first()

    def list_checklist_items(self, checklist_id: int) -> List[OperationalChecklistItem]:
        return self.db.query(OperationalChecklistItem).filter(OperationalChecklistItem.checklist_id == checklist_id).order_by(OperationalChecklistItem.position).all()

    def create_checklist_item(self, data: dict) -> OperationalChecklistItem:
        item = OperationalChecklistItem(**data)
        self.db.add(item)
        return item

    def update_checklist_item(self, item: OperationalChecklistItem, data: dict) -> OperationalChecklistItem:
        for key, value in data.items():
            if hasattr(item, key):
                setattr(item, key, value)
        return item
