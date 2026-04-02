from pydantic import BaseModel, Field, field_validator
from typing import Optional, Union
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
    destinos: Optional[list[str]] = Field(None, description="Lista de destinos: Atacama, Uyuni, Santiago ou outros")
    data_chegada: Optional[date] = Field(None, description="Data de chegada no destino (YYYY-MM-DD)")
    data_partida: Optional[date] = Field(None, description="Data de partida do destino (YYYY-MM-DD)")
    campos_personalizados: Optional[dict] = Field(default_factory=dict, description="Campos personalizados (JSON livre)")
    status_venda: str = Field("em_negociacao", description="Status geral: em_negociacao, venda, perda")

    @field_validator("destinos", mode="before")
    @classmethod
    def normalize_destinos(cls, v):
        """Accept either a single string or a list of strings."""
        if v is None:
            return None
        if isinstance(v, str):
            # Comma-separated or single value
            return [d.strip() for d in v.split(",") if d.strip()]
        if isinstance(v, list):
            return [str(d).strip() for d in v if str(d).strip()]
        return v


class LeadCreate(LeadBase):
    pass


class LeadUpdate(BaseModel):
    nome: Optional[str] = Field(None, min_length=1, max_length=200)
    email: Optional[str] = None
    whatsapp: Optional[str] = Field(None, max_length=30)
    destinos: Optional[list[str]] = None
    data_chegada: Optional[date] = None
    data_partida: Optional[date] = None
    campos_personalizados: Optional[dict] = None
    status_venda: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("destinos", mode="before")
    @classmethod
    def normalize_destinos(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            return [d.strip() for d in v.split(",") if d.strip()]
        if isinstance(v, list):
            return [str(d).strip() for d in v if str(d).strip()]
        return v


class LeadResponse(BaseModel):
    id: int
    nome: str
    email: Optional[str] = None
    whatsapp: Optional[str] = None
    destinos: Optional[list[str]] = None
    data_chegada: Optional[date] = None
    data_partida: Optional[date] = None
    campos_personalizados: dict = {}
    status_venda: str = "em_negociacao"
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
