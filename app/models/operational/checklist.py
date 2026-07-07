from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class OperationalChecklist(Base):
    __tablename__ = "operational_checklists"

    id = Column(Integer, primary_key=True, index=True)
    card_id = Column(Integer, ForeignKey("operational_cards.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    position = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    card = relationship("OperationalCard", back_populates="checklists")
    items = relationship(
        "OperationalChecklistItem",
        back_populates="checklist",
        cascade="all, delete-orphan",
        passive_deletes=True
    )

    def __repr__(self):
        return f"<OperationalChecklist(id={self.id}, name='{self.name}', card_id={self.card_id})>"


class OperationalChecklistItem(Base):
    __tablename__ = "operational_checklist_items"

    id = Column(Integer, primary_key=True, index=True)
    checklist_id = Column(Integer, ForeignKey("operational_checklists.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(200), nullable=False)
    is_checked = Column(Boolean, default=False, nullable=False, index=True)
    checked_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    checked_at = Column(DateTime(timezone=True), nullable=True)
    position = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    checklist = relationship("OperationalChecklist", back_populates="items")
    user_checker = relationship("User", foreign_keys=[checked_by])

    def __repr__(self):
        return f"<OperationalChecklistItem(id={self.id}, name='{self.name}', is_checked={self.is_checked})>"


class OperationalChecklistTemplate(Base):
    __tablename__ = "operational_checklist_templates"

    id = Column(Integer, primary_key=True, index=True)
    board_template_id = Column(Integer, ForeignKey("operational_board_templates.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    items_schema = Column(JSON, default=list, nullable=True)  # List of items: ["A", "B", "C"]
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    board_template = relationship("OperationalBoardTemplate", back_populates="checklist_templates")

    def __repr__(self):
        return f"<OperationalChecklistTemplate(id={self.id}, name='{self.name}')>"
