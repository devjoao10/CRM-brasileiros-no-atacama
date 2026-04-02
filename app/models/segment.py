from sqlalchemy import Column, Integer, String, Boolean, DateTime, JSON, Text
from sqlalchemy.sql import func

from app.database import Base


class Segment(Base):
    """
    A saved segmentation list — stores filter criteria as JSON.
    When 'resolved', returns the leads matching those criteria at that moment.
    Can be used by automations (N8N) to target specific audiences.
    """
    __tablename__ = "segments"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String(200), nullable=False, unique=True, index=True)
    descricao = Column(Text, nullable=True)

    # Filter criteria stored as JSON dict
    # Example: {
    #   "destino": "Atacama",
    #   "tag_ids": [1, 3],
    #   "tag_mode": "any",
    #   "funnel_id": 2,
    #   "etapa_id": "contato",
    #   "ano_chegada": 2026,
    #   "mes_chegada": 7,
    #   "campo_chave": "origem",
    #   "campo_valor": "Instagram",
    #   "is_active": true
    # }
    filtros = Column(JSON, nullable=False, default=dict)

    # Metadata
    cor = Column(String(7), nullable=False, default="#2B6CB0")  # Hex color for visual ID
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<Segment(id={self.id}, nome='{self.nome}')>"
