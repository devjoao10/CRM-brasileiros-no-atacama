from sqlalchemy import Column, Integer, String, DateTime, Table, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


# Many-to-many association table
lead_tags = Table(
    "lead_tags",
    Base.metadata,
    Column("lead_id", Integer, ForeignKey("leads.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", Integer, ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Tag(Base):
    __tablename__ = "tags"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String(100), nullable=False, unique=True, index=True)
    cor = Column(String(7), nullable=False, default="#2B6CB0")  # Hex color
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationship
    leads = relationship("Lead", secondary=lead_tags, back_populates="tags")

    def __repr__(self):
        return f"<Tag(id={self.id}, nome='{self.nome}', cor='{self.cor}')>"
