from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, ForeignKey, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class OperationalBoard(Base):
    __tablename__ = "operational_boards"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    is_archived = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships (textual mapping to avoid circular imports)
    lists = relationship(
        "OperationalList",
        back_populates="board",
        cascade="all, delete-orphan",
        passive_deletes=True
    )
    field_definitions = relationship(
        "OperationalCardFieldDefinition",
        back_populates="board",
        cascade="all, delete-orphan",
        passive_deletes=True
    )
    cards = relationship(
        "OperationalCard",
        back_populates="board",
        cascade="all, delete-orphan",
        passive_deletes=True
    )

    def __repr__(self):
        return f"<OperationalBoard(id={self.id}, name='{self.name}', is_archived={self.is_archived})>"


class OperationalList(Base):
    __tablename__ = "operational_lists"

    id = Column(Integer, primary_key=True, index=True)
    board_id = Column(Integer, ForeignKey("operational_boards.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    position = Column(Integer, default=0, nullable=False)
    is_archived = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    board = relationship("OperationalBoard", back_populates="lists")
    cards = relationship(
        "OperationalCard",
        back_populates="list",
        cascade="all, delete-orphan",
        passive_deletes=True
    )

    def __repr__(self):
        return f"<OperationalList(id={self.id}, name='{self.name}', position={self.position})>"


class OperationalBoardTemplate(Base):
    __tablename__ = "operational_board_templates"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    lists_schema = Column(JSON, default=list, nullable=True)  # List of column names: ["A", "B", "C"]
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    checklist_templates = relationship(
        "OperationalChecklistTemplate",
        back_populates="board_template",
        cascade="all, delete-orphan",
        passive_deletes=True
    )

    def __repr__(self):
        return f"<OperationalBoardTemplate(id={self.id}, name='{self.name}')>"
