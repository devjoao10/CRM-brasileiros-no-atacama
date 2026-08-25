from pydantic import BaseModel, Field, field_validator
from typing import Optional
from datetime import datetime, date

from app.schemas.lead import LeadResponse
from app.schemas.tag import TagResponse


# ─── Funnel Stages ───────────────────────────────

class StageSchema(BaseModel):
    # AUDIT-2026-08-W2B-orq: este campo NAO era validado. Ele e escolhido pelo
    # cliente, guardado em funnels.etapas (JSON) e depois interpolado em atributo
    # HTML e em handler inline no board do Pipeline. Escapar no template e a
    # defesa correta e ja foi feita; validar aqui e o que impede a proxima tela
    # de reabrir o buraco. O slug cobre todo id que o repositorio usa hoje
    # ('novo', 'contato', 'negociacao', 'e1', 'm1'...) e o limite de 64 respeita
    # funnel_entries.etapa_id, que e String(100).
    id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        # AUDIT-2026-08-F2 — o padrao ERA `^[A-Za-z0-9_-]+$`, e isso era risco de
        # DISPONIBILIDADE disfarcado de seguranca.
        #
        # `FunnelUpdate.etapas` revalida a lista INTEIRA. Entao qualquer funil de
        # producao cuja etapa ja tenha id com espaco ou acento passaria a dar 422
        # em QUALQUER edicao daquele funil — inclusive so renomear o funil. E o
        # system message do proprio "Agente Gerenciador de Leads" chama a etapa
        # de "Sem Contato", com espaco. Nao consigo ver o banco de producao para
        # saber se isso acontece, e a correcao certa e nao depender disso.
        #
        # O que protege de verdade e o esc() no template (nove interpolacoes em
        # templates/pipeline.html), travado por
        # tests/test_frontend_injection_contract.py. Este padrao e defesa em
        # profundidade — e defesa em profundidade que derruba funcionalidade
        # legitima nao e defesa, e uma segunda falha.
        #
        # Passa a rejeitar exatamente o que quebra atributo HTML ou literal JS:
        # aspa simples e dupla, menor/maior, & e barra invertida, mais todos os
        # caracteres de controle. Espaco e acento sao aceitos — inofensivos
        # depois de escapados.
        pattern="^[^\"'<>&\\\\\\x00-\\x1f\\x7f]+$",
        description="ID unico da etapa (ex: 'novo', 'sem_contato', 'Sem Contato')",
    )
    nome: str = Field(..., description="Nome exibido da etapa (ex: 'Novo Lead')")
    dias_limite: int = Field(7, ge=1, description="Dias máximos antes de considerar o lead estagnado nesta etapa")


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

    @field_validator("lead_id", mode="before")
    @classmethod
    def coerce_lead_id(cls, v):
        if isinstance(v, str):
            return int(v)
        return v


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
    num_viajantes: Optional[int] = None
    etapa_id: str
    posicao: Optional[int] = 0
    tags: list[TagResponse] = []
    responsavel_id: Optional[int] = None
    responsavel_nome: Optional[str] = None
    entry_created_at: Optional[datetime] = None  # Quando o lead entrou neste funil
    entry_updated_at: Optional[datetime] = None  # Última movimentação (mover etapa atualiza)


class KanbanStageResponse(BaseModel):
    """A single Kanban column with its leads."""
    id: str
    nome: str
    dias_limite: int = 7  # Dias máximos antes de considerar estagnado
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


# ─── Board paginado por etapa (PERF-PIPE-01) ─────
# O board completo (KanbanBoardResponse) continua existindo e inalterado.
# Estes schemas servem ao caminho novo: esqueleto primeiro, cards por coluna.

class KanbanStageMeta(BaseModel):
    """Etapa SEM os cards — só o necessário para desenhar a coluna."""
    id: str
    nome: str
    dias_limite: int = 7
    total: int = 0          # total de cards ATIVOS na etapa, sem filtro


class KanbanBoardMeta(BaseModel):
    """Esqueleto do board: funil + etapas + contagens. Nenhum card."""
    funnel: FunnelResponse
    stages: list[KanbanStageMeta]
    total_leads: int


class StageCardsResponse(BaseModel):
    """Uma página de cards de UMA etapa."""
    etapa_id: str
    items: list[LeadCardResponse] = []
    total: int = 0                          # total que casa o filtro atual
    has_more: bool = False
    next_cursor: Optional[str] = None       # "<updated_at_iso>|<entry_id>"
    # Card alvo do deep-link quando ele está fora desta página (include_lead_id).
    target: Optional[LeadCardResponse] = None


class LeadLocationResponse(BaseModel):
    """Onde um lead está no pipeline — sem carregar o board."""
    lead_id: int
    funnel_id: int
    funnel_nome: str
    etapa_id: str
    etapa_nome: str
    entry_id: int
