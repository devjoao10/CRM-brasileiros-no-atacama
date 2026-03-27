from sqlalchemy import Column, Integer, String, Boolean, DateTime, Date, Text, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)

    # Core fields
    nome = Column(String(200), nullable=False, index=True)
    email = Column(String(255), nullable=True, index=True)
    whatsapp = Column(String(30), nullable=True, index=True)

    # Travel details
    destino = Column(String(100), nullable=True, index=True)  # Atacama, Uyuni, Santiago, or custom
    data_chegada = Column(Date, nullable=True)
    data_partida = Column(Date, nullable=True)

    # Custom fields — JSON dict for unlimited customization
    # Example: {"origem": "Instagram", "idioma": "pt-BR", "observacoes": "VIP"}
    campos_personalizados = Column(JSON, default=dict, nullable=False)

    # Status
    is_active = Column(Boolean, default=True, nullable=False)

    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    tags = relationship("Tag", secondary="lead_tags", back_populates="leads")

    def __repr__(self):
        return f"<Lead(id={self.id}, nome='{self.nome}', destino='{self.destino}')>"

