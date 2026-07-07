from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class OperationalComment(Base):
    __tablename__ = "operational_comments"

    id = Column(Integer, primary_key=True, index=True)
    card_id = Column(Integer, ForeignKey("operational_cards.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    card = relationship("OperationalCard", back_populates="comments")
    user = relationship("User", foreign_keys=[user_id])
    mentions = relationship(
        "OperationalMention",
        back_populates="comment",
        cascade="all, delete-orphan",
        passive_deletes=True
    )

    def __repr__(self):
        return f"<OperationalComment(id={self.id}, card_id={self.card_id}, user_id={self.user_id})>"


class OperationalMention(Base):
    __tablename__ = "operational_mentions"

    id = Column(Integer, primary_key=True, index=True)
    comment_id = Column(Integer, ForeignKey("operational_comments.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    comment = relationship("OperationalComment", back_populates="mentions")
    user = relationship("User", foreign_keys=[user_id])

    def __repr__(self):
        return f"<OperationalMention(comment_id={self.comment_id}, user_id={self.user_id})>"


class OperationalNotification(Base):
    __tablename__ = "operational_notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    card_id = Column(Integer, ForeignKey("operational_cards.id", ondelete="CASCADE"), nullable=True, index=True)
    event_type = Column(String(50), nullable=False)  # e.g., 'mention', 'movement', 'assignee'
    message = Column(Text, nullable=False)
    read_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    # Relationships
    user = relationship("User", foreign_keys=[user_id])
    card = relationship("OperationalCard")

    def __repr__(self):
        return f"<OperationalNotification(id={self.id}, user_id={self.user_id}, event='{self.event_type}', read={self.read_at is not None})>"


class OperationalActivityLog(Base):
    __tablename__ = "operational_activity_logs"

    id = Column(Integer, primary_key=True, index=True)
    card_id = Column(Integer, ForeignKey("operational_cards.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    action = Column(String(100), nullable=False)  # e.g., 'create', 'edit', 'move', 'archive', 'checklist_toggle'
    details = Column(JSON, default=dict, nullable=True)  # Details: {"from_list": 1, "to_list": 2}
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    # Relationships
    card = relationship("OperationalCard")
    user = relationship("User", foreign_keys=[user_id])

    def __repr__(self):
        return f"<OperationalActivityLog(id={self.id}, card_id={self.card_id}, action='{self.action}')>"


class OperationalCardMovement(Base):
    __tablename__ = "operational_card_movements"

    id = Column(Integer, primary_key=True, index=True)
    card_id = Column(Integer, ForeignKey("operational_cards.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    from_board_id = Column(Integer, ForeignKey("operational_boards.id", ondelete="SET NULL"), nullable=True)
    from_list_id = Column(Integer, ForeignKey("operational_lists.id", ondelete="SET NULL"), nullable=True)
    to_board_id = Column(Integer, ForeignKey("operational_boards.id", ondelete="CASCADE"), nullable=False)
    to_list_id = Column(Integer, ForeignKey("operational_lists.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    # Relationships
    card = relationship("OperationalCard", back_populates="movements")
    user = relationship("User", foreign_keys=[user_id])
    
    from_board = relationship("OperationalBoard", foreign_keys=[from_board_id])
    from_list = relationship("OperationalList", foreign_keys=[from_list_id])
    to_board = relationship("OperationalBoard", foreign_keys=[to_board_id])
    to_list = relationship("OperationalList", foreign_keys=[to_list_id])

    def __repr__(self):
        return f"<OperationalCardMovement(card_id={self.card_id}, to_list_id={self.to_list_id})>"
