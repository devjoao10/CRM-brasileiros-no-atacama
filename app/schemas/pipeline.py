from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime, date

from app.schemas.lead import LeadResponse
from app.schemas.tag import TagResponse


# ─── Funnel Stages ───────────────────────────────

class StageSchema(BaseModel):
    id: str = Field(..., description="ID único da etapa (ex: 'novo', 'contato', 'negociacao')")
    nome: str = Field(..., description="Nome exibido da etapa (ex: 'Novo Lead')")


# ─── Funnel ──────────────────────────────────────

class FunnelCreate(BaseModel):
    nome: str = Field(..., min_length=1, max_length=200, description="Nome do funil")
    etapas: list[StageSchema] = Field(..., min_length=1, description="Lista de etapas ordenadas")


class FunnelUpdate(BaseModel):
    nome: Optional[str] = Field(None, min_length=1, max_length=200)
    etapas: Optional[list[StageSchema]] = None
    is_active: Optional[bool] = None


class FunnelResponse(BaseModel):
    id: int
    nome: str
    etapas: list[dict]
    is_active: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class FunnelListResponse(BaseModel):
    total: int
    funnels: list[FunnelResponse]


# ─── Funnel Entry (lead in funnel) ───────────────

class FunnelEntryCreate(BaseModel):
    lead_id: int = Field(..., description="ID do lead")
    etapa_id: str = Field(..., description="ID da etapa onde posicionar o lead")


class FunnelEntryMove(BaseModel):
    etapa_id: str = Field(..., description="ID da nova etapa")
    posicao: Optional[int] = Field(None, description="Posição na coluna (0 = topo)")


class FunnelEntryTransfer(BaseModel):
    destino_funnel_id: int = Field(..., description="ID do funil de destino")
    destino_etapa_id: str = Field(..., description="ID da etapa no funil de destino")


class FunnelEntryResponse(BaseModel):
    id: int
    lead_id: int
    funnel_id: int
    etapa_id: str
    posicao: Optional[int] = 0
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class LeadCardResponse(BaseModel):
    """Lead card with summary info for the Kanban board."""
    entry_id: int
    lead_id: int
    nome: str
    email: Optional[str] = None
    whatsapp: Optional[str] = None
    destinos: Optional[list[str]] = None
    data_chegada: Optional[date] = None
    data_partida: Optional[date] = None
    etapa_id: str
    posicao: Optional[int] = 0
    tags: list[TagResponse] = []


class KanbanStageResponse(BaseModel):
    """A single Kanban column with its leads."""
    id: str
    nome: str
    leads: list[LeadCardResponse] = []


class KanbanBoardResponse(BaseModel):
    """Complete Kanban board for a funnel."""
    funnel: FunnelResponse
    stages: list[KanbanStageResponse]
    total_leads: int


# ─── History ─────────────────────────────────────

class HistoryResponse(BaseModel):
    id: int
    lead_id: int
    evento: str
    descricao: Optional[str] = None
    funnel_id: Optional[int] = None
    etapa_origem: Optional[str] = None
    etapa_destino: Optional[str] = None
    funnel_origem_id: Optional[int] = None
    dados: dict = {}
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class HistoryListResponse(BaseModel):
    total: int
    historico: list[HistoryResponse]
