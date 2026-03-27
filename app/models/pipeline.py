from sqlalchemy import Column, Integer, String, Boolean, DateTime, JSON, Text, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class Funnel(Base):
    """Sales funnel with ordered stages."""
    __tablename__ = "funnels"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String(200), nullable=False, unique=True, index=True)
    # Stages stored as ordered JSON list: [{"id": "stage-1", "nome": "Novo Lead"}, ...]
    etapas = Column(JSON, nullable=False, default=list)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    entries = relationship("FunnelEntry", back_populates="funnel", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Funnel(id={self.id}, nome='{self.nome}')>"


class FunnelEntry(Base):
    """A lead's placement in a specific funnel and stage."""
    __tablename__ = "funnel_entries"

    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    funnel_id = Column(Integer, ForeignKey("funnels.id", ondelete="CASCADE"), nullable=False, index=True)
    etapa_id = Column(String(100), nullable=False, index=True)  # Matches stage id in funnel.etapas
    posicao = Column(Integer, default=0)  # Order within the stage column
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    funnel = relationship("Funnel", back_populates="entries")
    lead = relationship("Lead", backref="funnel_entries")

    def __repr__(self):
        return f"<FunnelEntry(lead_id={self.lead_id}, funnel_id={self.funnel_id}, etapa='{self.etapa_id}')>"


class LeadHistory(Base):
    """
    Complete event log for a lead.
    Tracks: funnel entries, stage moves, funnel transfers, creation, etc.
    """
    __tablename__ = "lead_history"

    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)

    # Event type: 'created', 'entered_funnel', 'stage_moved', 'left_funnel', 'transferred', 'tag_changed', 'note'
    evento = Column(String(50), nullable=False, index=True)

    # Context
    descricao = Column(Text, nullable=True)  # Human-readable description
    funnel_id = Column(Integer, ForeignKey("funnels.id", ondelete="SET NULL"), nullable=True)
    etapa_origem = Column(String(100), nullable=True)
    etapa_destino = Column(String(100), nullable=True)
    funnel_origem_id = Column(Integer, nullable=True)  # For transfers between funnels

    # Metadata
    dados = Column(JSON, default=dict)  # Any extra data (automation source, user who triggered, etc.)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    lead = relationship("Lead", backref="history")
    funnel = relationship("Funnel")

    def __repr__(self):
        return f"<LeadHistory(lead_id={self.lead_id}, evento='{self.evento}')>"
