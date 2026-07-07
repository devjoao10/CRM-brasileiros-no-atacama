from sqlalchemy.orm import Session
from app.repositories.operational_board_repo import OperationalBoardRepository
from app.models.operational.board import OperationalBoard, OperationalList


class OperationalBoardService:
    def __init__(self, db: Session):
        self.db = db
        self.board_repo = OperationalBoardRepository(db)

    def list_boards(self, include_archived: bool = False):
        return self.board_repo.list_boards(include_archived)

    def get_board_or_404(self, board_id: int) -> OperationalBoard:
        board = self.board_repo.get_board(board_id)
        if not board:
            raise ValueError(f"Quadro com ID {board_id} não encontrado")
        return board

    def create_board(self, data: dict, current_user) -> OperationalBoard:
        board = self.board_repo.create_board(data)
        self.db.commit()
        self.db.refresh(board)
        return board

    def update_board(self, board_id: int, data: dict, current_user) -> OperationalBoard:
        board = self.get_board_or_404(board_id)
        self.board_repo.update_board(board, data)
        self.db.commit()
        self.db.refresh(board)
        return board

    def archive_board(self, board_id: int, current_user) -> OperationalBoard:
        board = self.get_board_or_404(board_id)
        self.board_repo.archive_board(board)
        self.db.commit()
        self.db.refresh(board)
        return board

    def get_list_or_404(self, list_id: int) -> OperationalList:
        operational_list = self.board_repo.get_list(list_id)
        if not operational_list:
            raise ValueError(f"Lista com ID {list_id} não encontrada")
        return operational_list

    def list_lists_by_board(self, board_id: int, include_archived: bool = False):
        self.get_board_or_404(board_id)
        return self.board_repo.list_lists_by_board(board_id, include_archived)

    def create_list(self, data: dict, current_user) -> OperationalList:
        board_id = data.get("board_id")
        self.get_board_or_404(board_id)

        operational_list = self.board_repo.create_list(data)
        self.db.commit()
        self.db.refresh(operational_list)
        return operational_list

    def update_list(self, list_id: int, data: dict, current_user) -> OperationalList:
        operational_list = self.get_list_or_404(list_id)
        self.board_repo.update_list(operational_list, data)
        self.db.commit()
        self.db.refresh(operational_list)
        return operational_list

    def archive_list(self, list_id: int, current_user) -> OperationalList:
        operational_list = self.get_list_or_404(list_id)
        self.board_repo.archive_list(operational_list)
        self.db.commit()
        self.db.refresh(operational_list)
        return operational_list
