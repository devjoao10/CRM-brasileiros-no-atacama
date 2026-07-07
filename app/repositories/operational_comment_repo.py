from sqlalchemy.orm import Session
from typing import List, Optional
from app.models.operational.notification import OperationalComment, OperationalMention


class OperationalCommentRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_comment(self, comment_id: int) -> Optional[OperationalComment]:
        return self.db.query(OperationalComment).filter(OperationalComment.id == comment_id).first()

    def list_comments_by_card(self, card_id: int) -> List[OperationalComment]:
        return self.db.query(OperationalComment).filter(OperationalComment.card_id == card_id).order_by(OperationalComment.created_at.asc()).all()

    def create_comment(self, data: dict) -> OperationalComment:
        comment = OperationalComment(**data)
        self.db.add(comment)
        return comment

    def create_mention(self, comment_id: int, user_id: int) -> OperationalMention:
        mention = OperationalMention(comment_id=comment_id, user_id=user_id)
        self.db.add(mention)
        return mention

    def list_mentions_by_comment(self, comment_id: int) -> List[OperationalMention]:
        return self.db.query(OperationalMention).filter(OperationalMention.comment_id == comment_id).all()
