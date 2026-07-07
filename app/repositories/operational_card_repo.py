from sqlalchemy.orm import Session
from typing import List, Optional
from app.models.operational.card import (
    OperationalCard,
    OperationalCardAssignee,
    OperationalCardFieldDefinition,
    OperationalCardFieldValue,
)


class OperationalCardRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_card(self, card_id: int) -> Optional[OperationalCard]:
        return self.db.query(OperationalCard).filter(OperationalCard.id == card_id).first()

    def list_cards_by_board(self, board_id: int, include_archived: bool = False) -> List[OperationalCard]:
        query = self.db.query(OperationalCard).filter(OperationalCard.board_id == board_id)
        if not include_archived:
            query = query.filter(OperationalCard.is_archived == False)
        return query.all()

    def list_cards_by_list(self, list_id: int, include_archived: bool = False) -> List[OperationalCard]:
        query = self.db.query(OperationalCard).filter(OperationalCard.list_id == list_id)
        if not include_archived:
            query = query.filter(OperationalCard.is_archived == False)
        return query.all()

    def create_card(self, data: dict) -> OperationalCard:
        card = OperationalCard(**data)
        self.db.add(card)
        return card

    def update_card(self, card: OperationalCard, data: dict) -> OperationalCard:
        for key, value in data.items():
            if hasattr(card, key):
                setattr(card, key, value)
        return card

    def archive_card(self, card: OperationalCard) -> OperationalCard:
        card.is_archived = True
        return card

    def get_assignee_link(self, card_id: int, user_id: int) -> Optional[OperationalCardAssignee]:
        return self.db.query(OperationalCardAssignee).filter(
            OperationalCardAssignee.card_id == card_id,
            OperationalCardAssignee.user_id == user_id
        ).first()

    def add_assignee(self, card_id: int, user_id: int) -> OperationalCardAssignee:
        assignee = OperationalCardAssignee(card_id=card_id, user_id=user_id)
        self.db.add(assignee)
        return assignee

    def remove_assignee(self, card_id: int, user_id: int) -> bool:
        link = self.get_assignee_link(card_id, user_id)
        if link:
            self.db.delete(link)
            return True
        return False

    def list_assignees(self, card_id: int) -> List[OperationalCardAssignee]:
        return self.db.query(OperationalCardAssignee).filter(OperationalCardAssignee.card_id == card_id).all()

    def get_field_definition(self, definition_id: int) -> Optional[OperationalCardFieldDefinition]:
        return self.db.query(OperationalCardFieldDefinition).filter(OperationalCardFieldDefinition.id == definition_id).first()

    def create_field_definition(self, data: dict) -> OperationalCardFieldDefinition:
        definition = OperationalCardFieldDefinition(**data)
        self.db.add(definition)
        return definition

    def list_field_definitions(self, board_id: int, include_archived: bool = False) -> List[OperationalCardFieldDefinition]:
        query = self.db.query(OperationalCardFieldDefinition).filter(OperationalCardFieldDefinition.board_id == board_id)
        if not include_archived:
            query = query.filter(OperationalCardFieldDefinition.is_archived == False)
        return query.all()

    def get_field_value(self, card_id: int, definition_id: int) -> Optional[OperationalCardFieldValue]:
        return self.db.query(OperationalCardFieldValue).filter(
            OperationalCardFieldValue.card_id == card_id,
            OperationalCardFieldValue.definition_id == definition_id
        ).first()

    def create_field_value(self, data: dict) -> OperationalCardFieldValue:
        value = OperationalCardFieldValue(**data)
        self.db.add(value)
        return value

    def update_field_value(self, value_obj: OperationalCardFieldValue, data: dict) -> OperationalCardFieldValue:
        for key, value in data.items():
            if hasattr(value_obj, key):
                setattr(value_obj, key, value)
        return value_obj

    def list_field_values(self, card_id: int) -> List[OperationalCardFieldValue]:
        return self.db.query(OperationalCardFieldValue).filter(OperationalCardFieldValue.card_id == card_id).all()
