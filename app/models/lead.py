from sqlalchemy import Column, Integer, String, Boolean, DateTime, Date, Text, JSON, ForeignKey
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

    # Travel details — destinos is a JSON list like ["Atacama", "Uyuni"]
    destinos = Column(JSON, default=list, nullable=True)
    data_chegada = Column(Date, nullable=True)
    data_partida = Column(Date, nullable=True)
    total_dias = Column(Integer, nullable=True)  # Alternativa a datas fixas
    datas_destinos = Column(JSON, default=dict, nullable=True)  # {"Atacama": {"chegada":"...","partida":"..."}}
    num_viajantes = Column(Integer, nullable=True)  # Adultos
    num_criancas = Column(Integer, default=0, nullable=True)
    idades_criancas = Column(String(200), nullable=True)  # "6, 6, 3"

    # Custom fields — JSON dict for unlimited customization
    # Example: {"origem": "Instagram", "idioma": "pt-BR", "observacoes": "VIP"}
    campos_personalizados = Column(JSON, default=dict, nullable=False)

    # Status
    status_venda = Column(String(30), default="em_negociacao", nullable=False, index=True) # em_negociacao, venda, perda
    is_active = Column(Boolean, default=True, nullable=False)

    # Responsável (owner) — FK to users table, 0 = Agente IA
    responsavel_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    # Metadata
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    tags = relationship("Tag", secondary="lead_tags", back_populates="leads")
    responsavel = relationship("User", foreign_keys=[responsavel_id])

    def __repr__(self):
        return f"<Lead(id={self.id}, nome='{self.nome}', destinos={self.destinos})>"

