"""
Configuração da API WhatsApp (Meta Cloud API).
Armazena credenciais no banco — configurável via painel de Settings.
"""

from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.sql import func

from app.database import Base


class ApiConfig(Base):
    """Singleton — sempre ID=1. Credenciais da Meta Cloud API."""
    __tablename__ = "api_config"

    id = Column(Integer, primary_key=True, default=1)
    meta_access_token = Column(String(500), nullable=True)
    meta_phone_number_id = Column(String(50), nullable=True)
    meta_waba_id = Column(String(50), nullable=True)
    meta_verify_token = Column(String(100), nullable=True)
    meta_api_version = Column(String(10), default="v21.0", nullable=False)
    webhook_url = Column(String(300), nullable=True)
    is_connected = Column(Boolean, default=False, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<ApiConfig(connected={self.is_connected}, phone={self.meta_phone_number_id})>"
