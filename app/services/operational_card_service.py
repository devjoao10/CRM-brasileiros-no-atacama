from sqlalchemy.orm import Session
from typing import List, Optional
from app.repositories.operational_card_repo import OperationalCardRepository
from app.repositories.operational_board_repo import OperationalBoardRepository
from app.repositories.operational_flow_repo import OperationalFlowRepository
from app.models.operational.card import (
    OperationalCard,
    OperationalCardFieldDefinition,
    OperationalCardFieldValue,
)
from app.models.user import User


class OperationalCardService:
    def __init__(self, db: Session):
        self.db = db
        self.card_repo = OperationalCardRepository(db)
        self.board_repo = OperationalBoardRepository(db)
        self.flow_repo = OperationalFlowRepository(db)

    def get_card_or_404(self, card_id: int) -> OperationalCard:
        card = self.card_repo.get_card(card_id)
        if not card:
            raise ValueError(f"Card com ID {card_id} não encontrado")
        return card

    def list_cards_by_board(self, board_id: int, include_archived: bool = False) -> List[OperationalCard]:
        # Valida existência do board
        self.board_repo.get_board(board_id)
        return self.card_repo.list_cards_by_board(board_id, include_archived)

    def list_cards_by_list(self, list_id: int, include_archived: bool = False) -> List[OperationalCard]:
        # Valida existência da lista
        self.board_repo.get_list(list_id)
        return self.card_repo.list_cards_by_list(list_id, include_archived)

    def create_card(self, data: dict, current_user) -> OperationalCard:
        board_id = data.get("board_id")
        list_id = data.get("list_id")

        # Validar existências
        board = self.board_repo.get_board(board_id)
        if not board:
            raise ValueError(f"Quadro com ID {board_id} não encontrado")

        operational_list = self.board_repo.get_list(list_id)
        if not operational_list:
            raise ValueError(f"Lista com ID {list_id} não encontrada")

        # Validar se a lista pertence ao quadro informado
        if operational_list.board_id != board_id:
            raise ValueError("A lista informada não pertence ao quadro informado")

        # Injetar o criador se disponível
        creator_id = getattr(current_user, "id", None)
        card_data = {**data, "created_by": creator_id}

        card = self.card_repo.create_card(card_data)
        self.db.flush()  # Para obter o ID do card antes do commit

        # Registrar log de atividade
        self.flow_repo.create_activity_log({
            "card_id": card.id,
            "user_id": creator_id,
            "action": "create",
            "details": {"title": card.title}
        })

        self.db.commit()
        self.db.refresh(card)
        return card

    def update_card(self, card_id: int, data: dict, current_user) -> OperationalCard:
        card = self.get_card_or_404(card_id)

        # Proibir mudança de list_id ou board_id por aqui (deve passar pelo FlowService)
        data.pop("board_id", None)
        data.pop("list_id", None)

        self.card_repo.update_card(card, data)

        # Registrar log de atividade
        user_id = getattr(current_user, "id", None)
        self.flow_repo.create_activity_log({
            "card_id": card.id,
            "user_id": user_id,
            "action": "update",
            "details": {"updated_fields": list(data.keys())}
        })

        self.db.commit()
        self.db.refresh(card)
        return card

    def archive_card(self, card_id: int, current_user) -> OperationalCard:
        card = self.get_card_or_404(card_id)
        self.card_repo.archive_card(card)

        # Registrar log de atividade
        user_id = getattr(current_user, "id", None)
        self.flow_repo.create_activity_log({
            "card_id": card.id,
            "user_id": user_id,
            "action": "archive",
            "details": {}
        })

        self.db.commit()
        self.db.refresh(card)
        return card

    def add_assignee(self, card_id: int, user_id: int, current_user):
        card = self.get_card_or_404(card_id)

        # Validar se o usuário a ser atribuído existe
        user = self.db.query(User).filter(User.id == user_id).first()
        if not user:
            raise ValueError(f"Usuário com ID {user_id} não encontrado")

        # Validar duplicidade
        existing_link = self.card_repo.get_assignee_link(card_id, user_id)
        if existing_link:
            return existing_link

        assignee = self.card_repo.add_assignee(card_id, user_id)

        # Registrar log de atividade
        actor_id = getattr(current_user, "id", None)
        self.flow_repo.create_activity_log({
            "card_id": card.id,
            "user_id": actor_id,
            "action": "add_assignee",
            "details": {"assigned_user_id": user_id}
        })

        # Notificar o usuário atribuído
        from app.services.operational_notification_service import OperationalNotificationService
        notif_service = OperationalNotificationService(self.db)
        notif_service.create_notification(
            user_id=user_id,
            card_id=card_id,
            event_type="assignee",
            message="Você foi designado para um card."
        )

        self.db.commit()
        return assignee

    def remove_assignee(self, card_id: int, user_id: int, current_user) -> bool:
        card = self.get_card_or_404(card_id)

        success = self.card_repo.remove_assignee(card_id, user_id)
        if not success:
            raise ValueError(f"Usuário {user_id} não está atribuído a este card")

        # Registrar log de atividade
        actor_id = getattr(current_user, "id", None)
        self.flow_repo.create_activity_log({
            "card_id": card.id,
            "user_id": actor_id,
            "action": "remove_assignee",
            "details": {"unassigned_user_id": user_id}
        })

        self.db.commit()
        return True

    def create_field_definition(self, data: dict, current_user) -> OperationalCardFieldDefinition:
        board_id = data.get("board_id")
        self.board_repo.get_board(board_id)

        # Validar tipo de campo permitido no MVP 1
        field_type = data.get("field_type")
        allowed_types = {"short_text", "long_text", "date", "select", "boolean"}
        if field_type not in allowed_types:
            raise ValueError(f"Tipo de campo '{field_type}' não suportado no MVP 1")

        definition = self.card_repo.create_field_definition(data)
        self.db.commit()
        self.db.refresh(definition)
        return definition

    def list_field_definitions(self, board_id: int, include_archived: bool = False) -> List[OperationalCardFieldDefinition]:
        self.board_repo.get_board(board_id)
        return self.card_repo.list_field_definitions(board_id, include_archived)

    def set_field_value(self, card_id: int, definition_id: int, value_data: dict, current_user) -> OperationalCardFieldValue:
        card = self.get_card_or_404(card_id)

        definition = self.card_repo.get_field_definition(definition_id)
        if not definition:
            raise ValueError(f"Definição de campo com ID {definition_id} não encontrada")

        # Validar se a definição pertence ao mesmo quadro do card
        if definition.board_id != card.board_id:
            raise ValueError("Definição de campo não pertence ao quadro deste card")

        # Identificar e validar o valor correto com base no tipo definido
        field_type = definition.field_type
        db_data = {
            "card_id": card_id,
            "definition_id": definition_id,
            "value_text": None,
            "value_date": None,
            "value_boolean": None,
        }

        raw_val = value_data.get("value")
        if field_type in ("short_text", "long_text", "select"):
            db_data["value_text"] = str(raw_val) if raw_val is not None else None
        elif field_type == "date":
            db_data["value_date"] = raw_val  # Espera-se objeto datetime ou None
        elif field_type == "boolean":
            if raw_val is not None:
                db_data["value_boolean"] = bool(raw_val)
            else:
                db_data["value_boolean"] = None

        # Verificar se já existe um valor
        existing_value = self.card_repo.get_field_value(card_id, definition_id)
        if existing_value:
            value_obj = self.card_repo.update_field_value(existing_value, db_data)
        else:
            value_obj = self.card_repo.create_field_value(db_data)

        # Registrar log de atividade
        user_id = getattr(current_user, "id", None)
        self.flow_repo.create_activity_log({
            "card_id": card_id,
            "user_id": user_id,
            "action": "set_field_value",
            "details": {"definition_id": definition_id, "field_name": definition.name}
        })

        self.db.commit()
        self.db.refresh(value_obj)
        return value_obj

    def list_field_values(self, card_id: int) -> List[OperationalCardFieldValue]:
        self.get_card_or_404(card_id)
        return self.card_repo.list_field_values(card_id)
