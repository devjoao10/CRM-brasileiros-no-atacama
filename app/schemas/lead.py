from pydantic import BaseModel, Field
from typing import Optional
from datetime import date, datetime

from app.schemas.tag import TagResponse

DESTINOS_PRINCIPAIS = ["Atacama", "Uyuni", "Santiago"]


class LeadFunnelInfo(BaseModel):
    """Summary of a lead's placement in a funnel."""
    funnel_id: int
    funnel_nome: str
    etapa_id: str
    etapa_nome: str
    entry_id: int


class LeadBase(BaseModel):
    nome: str = Field(..., min_length=1, max_length=200, description="Nome do lead")
    email: Optional[str] = Field(None, description="Email do lead")
    whatsapp: Optional[str] = Field(None, max_length=30, description="Número de WhatsApp com código do país")
    destino: Optional[str] = Field(None, max_length=100, description="Destino: Atacama, Uyuni, Santiago ou outro")
    data_chegada: Optional[date] = Field(None, description="Data de chegada no destino (YYYY-MM-DD)")
    data_partida: Optional[date] = Field(None, description="Data de partida do destino (YYYY-MM-DD)")
    campos_personalizados: Optional[dict] = Field(default_factory=dict, description="Campos personalizados (JSON livre)")


class LeadCreate(LeadBase):
    pass


class LeadUpdate(BaseModel):
    nome: Optional[str] = Field(None, min_length=1, max_length=200)
    email: Optional[str] = None
    whatsapp: Optional[str] = Field(None, max_length=30)
    destino: Optional[str] = Field(None, max_length=100)
    data_chegada: Optional[date] = None
    data_partida: Optional[date] = None
    campos_personalizados: Optional[dict] = None
    is_active: Optional[bool] = None


class LeadResponse(BaseModel):
    id: int
    nome: str
    email: Optional[str] = None
    whatsapp: Optional[str] = None
    destino: Optional[str] = None
    data_chegada: Optional[date] = None
    data_partida: Optional[date] = None
    campos_personalizados: dict = {}
    is_active: bool
    tags: list[TagResponse] = []
    funis: list[LeadFunnelInfo] = []
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class LeadListResponse(BaseModel):
    total: int
    skip: int
    limit: int
    leads: list[LeadResponse]


class ImportResponse(BaseModel):
    total_linhas: int
    importados: int
    erros: int
    detalhes_erros: list[str] = []
