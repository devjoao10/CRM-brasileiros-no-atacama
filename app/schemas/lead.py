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
    total_dias: Optional[int] = Field(None, description="Total de dias da viagem (alternativa a datas fixas)")
    datas_destinos: Optional[dict] = Field(default_factory=dict, description="Datas por destino: {'Atacama': {'chegada':'...','partida':'...'}}")
    dias_por_destino: Optional[dict] = Field(default_factory=dict, description="Dias por destino: {'Atacama': 6, 'Santiago': 4}")
    num_viajantes: Optional[int] = Field(None, description="Número de viajantes adultos")
    num_criancas: Optional[int] = Field(0, description="Número de crianças (default 0)")
    idades_criancas: Optional[str] = Field(None, description="Idades das crianças separadas por vírgula: '6, 6, 3'")
    campos_personalizados: Optional[dict] = Field(default_factory=dict, description="Campos personalizados (JSON livre)")
    status_venda: str = Field("em_negociacao", description="Status geral: em_negociacao, venda, perda")
    responsavel_id: Optional[int] = Field(None, description="ID do usuário responsável (null = Agente IA)")

    @field_validator("destinos", mode="before")
    @classmethod
    def normalize_destinos(cls, v):
        """Accept either a single string or a list of strings."""
        if v is None:
            return None
        if isinstance(v, str):
            if not v.strip():
                return None
            # Comma-separated or single value
            return [d.strip() for d in v.split(",") if d.strip()]
        if isinstance(v, list):
            return [str(d).strip() for d in v if str(d).strip()]
        return v

    @field_validator("total_dias", "num_viajantes", "num_criancas", "responsavel_id", mode="before")
    @classmethod
    def empty_str_to_none_int(cls, v):
        """Convert empty strings to None for int fields (N8N sends '' when no data)."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("datas_destinos", "dias_por_destino", "campos_personalizados", mode="before")
    @classmethod
    def empty_str_to_none_dict(cls, v):
        """Convert empty strings to None/empty dict for dict fields."""
        if isinstance(v, str):
            if not v.strip():
                return None
            import json
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return v
        return v

    @field_validator("data_chegada", "data_partida", mode="before")
    @classmethod
    def empty_str_to_none_date(cls, v):
        """Convert empty strings to None for date fields."""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("email", "idades_criancas", mode="before")
    @classmethod
    def empty_str_to_none_str(cls, v):
        """Convert empty strings to None for optional string fields."""
        if isinstance(v, str) and not v.strip():
            return None
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
    total_dias: Optional[int] = None
    datas_destinos: Optional[dict] = None
    dias_por_destino: Optional[dict] = None
    num_viajantes: Optional[int] = None
    num_criancas: Optional[int] = None
    idades_criancas: Optional[str] = None
    campos_personalizados: Optional[dict] = None
    status_venda: Optional[str] = None
    is_active: Optional[bool] = None
    responsavel_id: Optional[int] = None

    @field_validator("destinos", mode="before")
    @classmethod
    def normalize_destinos(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            if not v.strip():
                return None
            return [d.strip() for d in v.split(",") if d.strip()]
        if isinstance(v, list):
            return [str(d).strip() for d in v if str(d).strip()]
        return v

    @field_validator("total_dias", "num_viajantes", "num_criancas", "responsavel_id", mode="before")
    @classmethod
    def empty_str_to_none_int(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("datas_destinos", "dias_por_destino", "campos_personalizados", mode="before")
    @classmethod
    def empty_str_to_none_dict(cls, v):
        if isinstance(v, str):
            if not v.strip():
                return None
            import json
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return v
        return v

    @field_validator("data_chegada", "data_partida", mode="before")
    @classmethod
    def empty_str_to_none_date(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("email", "idades_criancas", mode="before")
    @classmethod
    def empty_str_to_none_str(cls, v):
        if isinstance(v, str) and not v.strip():
            return None
        return v


class LeadResponse(BaseModel):
    id: int
    nome: str
    email: Optional[str] = None
    whatsapp: Optional[str] = None
    destinos: Optional[list[str]] = None
    data_chegada: Optional[date] = None
    data_partida: Optional[date] = None
    total_dias: Optional[int] = None
    datas_destinos: Optional[dict] = None
    dias_por_destino: Optional[dict] = None
    num_viajantes: Optional[int] = None
    num_criancas: Optional[int] = 0
    idades_criancas: Optional[str] = None
    campos_personalizados: dict = {}
    status_venda: str = "em_negociacao"
    is_active: bool
    responsavel_id: Optional[int] = None
    responsavel_nome: Optional[str] = None
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
