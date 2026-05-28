from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class OperationalCard(Base):
    __tablename__ = "operational_cards"

    id = Column(Integer, primary_key=True, index=True)
    board_id = Column(Integer, ForeignKey("operational_boards.id", ondelete="CASCADE"), nullable=False, index=True)
    list_id = Column(Integer, ForeignKey("operational_lists.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    due_date = Column(DateTime(timezone=True), nullable=True, index=True)
    is_archived = Column(Boolean, default=False, nullable=False, index=True)
    created_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships (textual mapping to avoid circular imports)
    board = relationship("OperationalBoard", foreign_keys=[board_id], back_populates="cards")
    list = relationship("OperationalList", foreign_keys=[list_id], back_populates="cards")
    creator = relationship("User", foreign_keys=[created_by])
    
    assignee_links = relationship(
        "OperationalCardAssignee",
        back_populates="card",
        cascade="all, delete-orphan",
        passive_deletes=True
    )
    field_values = relationship(
        "OperationalCardFieldValue",
        back_populates="card",
        cascade="all, delete-orphan",
        passive_deletes=True
    )
    checklists = relationship(
        "OperationalChecklist",
        back_populates="card",
        cascade="all, delete-orphan",
        passive_deletes=True
    )
    comments = relationship(
        "OperationalComment",
        back_populates="card",
        cascade="all, delete-orphan",
        passive_deletes=True
    )
    movements = relationship(
        "OperationalCardMovement",
        back_populates="card",
        cascade="all, delete-orphan",
        passive_deletes=True
    )

    def __repr__(self):
        return f"<OperationalCard(id={self.id}, title='{self.title}', list_id={self.list_id})>"


class OperationalCardAssignee(Base):
    __tablename__ = "operational_card_assignees"

    id = Column(Integer, primary_key=True, index=True)
    card_id = Column(Integer, ForeignKey("operational_cards.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    card = relationship("OperationalCard", back_populates="assignee_links")
    user = relationship("User", foreign_keys=[user_id])

    def __repr__(self):
        return f"<OperationalCardAssignee(card_id={self.card_id}, user_id={self.user_id})>"


class OperationalCardFieldDefinition(Base):
    __tablename__ = "operational_card_field_definitions"

    id = Column(Integer, primary_key=True, index=True)
    board_id = Column(Integer, ForeignKey("operational_boards.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    field_type = Column(String(30), nullable=False)  # short_text, long_text, date, select, boolean
    select_options = Column(JSON, default=list, nullable=True)  # dropdown options: ["Chile", "Uyuni"]
    is_required = Column(Boolean, default=False, nullable=False)
    is_archived = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    board = relationship("OperationalBoard", back_populates="field_definitions")
    values = relationship(
        "OperationalCardFieldValue",
        back_populates="definition",
        cascade="all, delete-orphan",
        passive_deletes=True
    )

    def __repr__(self):
        return f"<OperationalCardFieldDefinition(id={self.id}, name='{self.name}', type='{self.field_type}')>"


class OperationalCardFieldValue(Base):
    __tablename__ = "operational_card_field_values"

    id = Column(Integer, primary_key=True, index=True)
    card_id = Column(Integer, ForeignKey("operational_cards.id", ondelete="CASCADE"), nullable=False, index=True)
    definition_id = Column(Integer, ForeignKey("operational_card_field_definitions.id", ondelete="CASCADE"), nullable=False, index=True)
    value_text = Column(Text, nullable=True)
    value_date = Column(DateTime(timezone=True), nullable=True)
    value_boolean = Column(Boolean, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    card = relationship("OperationalCard", back_populates="field_values")
    definition = relationship("OperationalCardFieldDefinition", back_populates="values")

    def __repr__(self):
        return f"<OperationalCardFieldValue(card_id={self.card_id}, definition_id={self.definition_id})>"
