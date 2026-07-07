from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from app.repositories.operational_checklist_repo import OperationalChecklistRepository
from app.repositories.operational_card_repo import OperationalCardRepository
from app.repositories.operational_flow_repo import OperationalFlowRepository
from app.models.operational.checklist import OperationalChecklist, OperationalChecklistItem


class OperationalChecklistService:
    def __init__(self, db: Session):
        self.db = db
        self.checklist_repo = OperationalChecklistRepository(db)
        self.card_repo = OperationalCardRepository(db)
        self.flow_repo = OperationalFlowRepository(db)

    def list_checklists_by_card(self, card_id: int) -> List[OperationalChecklist]:
        # Valida existência do card
        card = self.card_repo.get_card(card_id)
        if not card:
            raise ValueError(f"Card com ID {card_id} não encontrado")
        return self.checklist_repo.list_checklists_by_card(card_id)

    def create_checklist(self, card_id: int, data: dict, current_user) -> OperationalChecklist:
        card = self.card_repo.get_card(card_id)
        if not card:
            raise ValueError(f"Card com ID {card_id} não encontrado")
        
        # Regra: Card arquivado não recebe checklist novo
        if card.is_archived:
            raise ValueError("Não é possível criar checklist em um card arquivado")

        checklist_data = {**data, "card_id": card_id}
        checklist = self.checklist_repo.create_checklist(checklist_data)
        self.db.flush()

        # Log
        actor_id = getattr(current_user, "id", None)
        self.flow_repo.create_activity_log({
            "card_id": card_id,
            "user_id": actor_id,
            "action": "create_checklist",
            "details": {"checklist_name": checklist.name}
        })

        self.db.commit()
        self.db.refresh(checklist)
        return checklist

    def list_checklist_items(self, checklist_id: int) -> List[OperationalChecklistItem]:
        checklist = self.checklist_repo.get_checklist(checklist_id)
        if not checklist:
            raise ValueError(f"Checklist com ID {checklist_id} não encontrado")
        return self.checklist_repo.list_checklist_items(checklist_id)

    def create_checklist_item(self, checklist_id: int, data: dict, current_user) -> OperationalChecklistItem:
        checklist = self.checklist_repo.get_checklist(checklist_id)
        if not checklist:
            raise ValueError(f"Checklist com ID {checklist_id} não encontrado")

        # Regra: Card arquivado não recebe checklist item novo
        card = self.card_repo.get_card(checklist.card_id)
        if card and card.is_archived:
            raise ValueError("Não é possível criar item em um card arquivado")

        item_data = {**data, "checklist_id": checklist_id}
        item = self.checklist_repo.create_checklist_item(item_data)
        self.db.flush()

        # Log
        actor_id = getattr(current_user, "id", None)
        self.flow_repo.create_activity_log({
            "card_id": checklist.card_id,
            "user_id": actor_id,
            "action": "create_checklist_item",
            "details": {"checklist_id": checklist_id, "item_name": item.name}
        })

        self.db.commit()
        self.db.refresh(item)
        return item

    def update_checklist_item(self, item_id: int, data: dict, current_user) -> OperationalChecklistItem:
        item = self.checklist_repo.get_checklist_item(item_id)
        if not item:
            raise ValueError(f"Item de checklist com ID {item_id} não encontrado")

        # Se is_checked for informado, atualiza checked_by e checked_at
        actor_id = getattr(current_user, "id", None)
        if "is_checked" in data:
            new_checked_state = data["is_checked"]
            if new_checked_state != item.is_checked:
                if new_checked_state:
                    data["checked_by"] = actor_id
                    data["checked_at"] = datetime.now()
                else:
                    data["checked_by"] = None
                    data["checked_at"] = None

        self.checklist_repo.update_checklist_item(item, data)
        self.db.flush()

        # Log
        self.flow_repo.create_activity_log({
            "card_id": item.checklist.card_id,
            "user_id": actor_id,
            "action": "update_checklist_item",
            "details": {"item_id": item_id, "updated_fields": list(data.keys())}
        })

        self.db.commit()
        self.db.refresh(item)
        return item

    def toggle_checklist_item(self, item_id: int, current_user) -> OperationalChecklistItem:
        item = self.checklist_repo.get_checklist_item(item_id)
        if not item:
            raise ValueError(f"Item de checklist com ID {item_id} não encontrado")

        actor_id = getattr(current_user, "id", None)
        new_state = not item.is_checked
        
        update_data = {"is_checked": new_state}
        if new_state:
            update_data["checked_by"] = actor_id
            update_data["checked_at"] = datetime.now()
        else:
            update_data["checked_by"] = None
            update_data["checked_at"] = None

        self.checklist_repo.update_checklist_item(item, update_data)
        self.db.flush()

        # Log
        self.flow_repo.create_activity_log({
            "card_id": item.checklist.card_id,
            "user_id": actor_id,
            "action": "toggle_checklist_item",
            "details": {"item_id": item_id, "is_checked": new_state}
        })

        self.db.commit()
        self.db.refresh(item)
        return item
