from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class SegmentFilters(BaseModel):
    """All possible filter criteria for a segment."""
    search: Optional[str] = None
    destino: Optional[str] = None
    is_active: Optional[bool] = None
    data_chegada_de: Optional[str] = None  # YYYY-MM-DD
    data_chegada_ate: Optional[str] = None
    data_partida_de: Optional[str] = None
    data_partida_ate: Optional[str] = None
    ano_chegada: Optional[int] = None
    mes_chegada: Optional[int] = None
    ano_partida: Optional[int] = None
    mes_partida: Optional[int] = None
    tag_ids: Optional[list[int]] = None
    tag_mode: str = "any"  # "any" or "all"
    funnel_id: Optional[int] = None
    etapa_id: Optional[str] = None
    campo_chave: Optional[str] = None
    campo_valor: Optional[str] = None
    criado_de: Optional[str] = None  # YYYY-MM-DD
    criado_ate: Optional[str] = None


class SegmentCreate(BaseModel):
    nome: str = Field(..., min_length=1, max_length=200)
    descricao: Optional[str] = None
    cor: str = Field(default="#2B6CB0", max_length=7)
    filtros: SegmentFilters = Field(default_factory=SegmentFilters)


class SegmentUpdate(BaseModel):
    nome: Optional[str] = Field(None, min_length=1, max_length=200)
    descricao: Optional[str] = None
    cor: Optional[str] = Field(None, max_length=7)
    filtros: Optional[SegmentFilters] = None
    is_active: Optional[bool] = None


class SegmentResponse(BaseModel):
    id: int
    nome: str
    descricao: Optional[str] = None
    cor: str
    filtros: dict = {}
    is_active: bool
    lead_count: int = 0  # Populated dynamically
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class SegmentListResponse(BaseModel):
    total: int
    segments: list[SegmentResponse]
