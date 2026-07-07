from sqlalchemy.orm import Session
from typing import List
from app.repositories.operational_card_repo import OperationalCardRepository
from app.repositories.operational_board_repo import OperationalBoardRepository
from app.repositories.operational_flow_repo import OperationalFlowRepository
from app.models.operational.notification import OperationalActivityLog, OperationalCardMovement


class OperationalFlowService:
    def __init__(self, db: Session):
        self.db = db
        self.card_repo = OperationalCardRepository(db)
        self.board_repo = OperationalBoardRepository(db)
        self.flow_repo = OperationalFlowRepository(db)

    def move_card(self, card_id: int, to_list_id: int, current_user) -> OperationalCardMovement:
        card = self.card_repo.get_card(card_id)
        if not card:
            raise ValueError(f"Card com ID {card_id} não encontrado")

        if card.is_archived:
            raise ValueError("Não é possível mover um card arquivado")

        to_list = self.board_repo.get_list(to_list_id)
        if not to_list:
            raise ValueError(f"Lista de destino com ID {to_list_id} não encontrada")

        if to_list.is_archived:
            raise ValueError("Não é possível mover um card para uma lista arquivada")

        # Capturar estados anteriores para registro
        from_board_id = card.board_id
        from_list_id = card.list_id
        to_board_id = to_list.board_id

        # Se não houve mudança real de coluna, apenas retorna None ou evita registrar redundâncias
        if from_list_id == to_list_id and from_board_id == to_board_id:
            return None

        # Efetuar a transição de estado do card
        self.card_repo.update_card(card, {
            "board_id": to_board_id,
            "list_id": to_list_id
        })

        user_id = getattr(current_user, "id", None)

        # 1. Registrar a movimentação física de tráfego
        movement = self.flow_repo.create_movement({
            "card_id": card_id,
            "user_id": user_id,
            "from_board_id": from_board_id,
            "from_list_id": from_list_id,
            "to_board_id": to_board_id,
            "to_list_id": to_list_id
        })

        # 2. Registrar na trilha geral de auditoria de atividades (Log)
        self.flow_repo.create_activity_log({
            "card_id": card_id,
            "user_id": user_id,
            "action": "move",
            "details": {
                "from_board_id": from_board_id,
                "from_list_id": from_list_id,
                "to_board_id": to_board_id,
                "to_list_id": to_list_id
            }
        })

        # Gerar notificações na central de alertas para os membros atribuídos ao card
        assignees = self.card_repo.list_assignees(card_id)
        from app.services.operational_notification_service import OperationalNotificationService
        notif_service = OperationalNotificationService(self.db)
        for link in assignees:
            if link.user_id != user_id:
                notif_service.create_notification(
                    user_id=link.user_id,
                    card_id=card_id,
                    event_type="movement",
                    message="Um card no qual você está designado foi movido."
                )

        self.db.commit()
        return movement

    def list_movements(self, card_id: int) -> List[OperationalCardMovement]:
        # Valida existência do card
        card = self.card_repo.get_card(card_id)
        if not card:
            raise ValueError(f"Card com ID {card_id} não encontrado")
        return self.flow_repo.list_movements_by_card(card_id)

    def list_activity_logs(self, card_id: int) -> List[OperationalActivityLog]:
        # Valida existência do card
        card = self.card_repo.get_card(card_id)
        if not card:
            raise ValueError(f"Card com ID {card_id} não encontrado")
        return self.flow_repo.list_activity_logs_by_card(card_id)
