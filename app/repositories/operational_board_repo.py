from sqlalchemy.orm import Session
from typing import List, Optional
from app.models.operational.board import OperationalBoard, OperationalList, OperationalBoardTemplate


class OperationalBoardRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_board(self, board_id: int) -> Optional[OperationalBoard]:
        return self.db.query(OperationalBoard).filter(OperationalBoard.id == board_id).first()

    def list_boards(self, include_archived: bool = False) -> List[OperationalBoard]:
        query = self.db.query(OperationalBoard)
        if not include_archived:
            query = query.filter(OperationalBoard.is_archived == False)
        return query.all()

    def create_board(self, data: dict) -> OperationalBoard:
        board = OperationalBoard(**data)
        self.db.add(board)
        return board

    def update_board(self, board: OperationalBoard, data: dict) -> OperationalBoard:
        for key, value in data.items():
            if hasattr(board, key):
                setattr(board, key, value)
        return board

    def archive_board(self, board: OperationalBoard) -> OperationalBoard:
        board.is_archived = True
        return board

    def get_list(self, list_id: int) -> Optional[OperationalList]:
        return self.db.query(OperationalList).filter(OperationalList.id == list_id).first()

    def list_lists_by_board(self, board_id: int, include_archived: bool = False) -> List[OperationalList]:
        query = self.db.query(OperationalList).filter(OperationalList.board_id == board_id)
        if not include_archived:
            query = query.filter(OperationalList.is_archived == False)
        return query.order_by(OperationalList.position.asc()).all()

    def create_list(self, data: dict) -> OperationalList:
        new_list = OperationalList(**data)
        self.db.add(new_list)
        return new_list

    def update_list(self, operational_list: OperationalList, data: dict) -> OperationalList:
        for key, value in data.items():
            if hasattr(operational_list, key):
                setattr(operational_list, key, value)
        return operational_list

    def archive_list(self, operational_list: OperationalList) -> OperationalList:
        operational_list.is_archived = True
        return operational_list

    def get_board_template(self, template_id: int) -> Optional[OperationalBoardTemplate]:
        return self.db.query(OperationalBoardTemplate).filter(OperationalBoardTemplate.id == template_id).first()

    def list_board_templates(self) -> List[OperationalBoardTemplate]:
        return self.db.query(OperationalBoardTemplate).all()
