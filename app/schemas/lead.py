from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, Union
from datetime import date, datetime

from app.schemas.tag import TagResponse

DESTINOS_PRINCIPAIS = ["Atacama", "Uyuni", "Santiago"]


# AUDIT-2026-08-W0 — NUL em JSON derruba o filtro de campo personalizado.
#
# `Lead.campos_personalizados` e declarado `Column(JSON)`, que no PostgreSQL
# compila para o tipo `json` (texto). O tipo `json` ACEITA a sequencia \u0000;
# o tipo `jsonb` NAO. E app/query_filters.py faz `cast(coluna, JSONB)` em TODA
# linha para poder expandir os pares com jsonb_each_text.
#
# Consequencia: UMA unica linha envenenada faz `GET /api/leads?campo_chave=...`
# e TODO segmento que use campo personalizado responderem 500 para TODOS os
# usuarios, ate alguem achar e consertar a linha na mao. O CASE de guarda que
# existe no filtro nao salva — ele roda DEPOIS do cast.
#
# A correcao estrutural e migrar a coluna para JSONB (que valida na escrita) e
# limpar as linhas existentes; isso mexe em dados de producao e esta fora do
# escopo desta auditoria. O que da para fechar aqui e a PORTA DE ENTRADA: a API
# e o n8n param de conseguir gravar o caractere.
def _rejeita_nul(valor, campo: str):
    """Levanta ValueError se houver NUL em qualquer chave ou valor do dict."""
    if valor is None:
        return valor
    pilha = [valor]
    while pilha:
        atual = pilha.pop()
        if isinstance(atual, str):
            if "\u0000" in atual:
                raise ValueError(
                    f"{campo}: caractere NUL (\u0000) nao e aceito — ele torna a "
                    "linha impossivel de filtrar no PostgreSQL"
                )
        elif isinstance(atual, dict):
            pilha.extend(atual.keys())
            pilha.extend(atual.values())
        elif isinstance(atual, (list, tuple)):
            pilha.extend(atual)
    return valor



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
        return _rejeita_nul(v, "campo JSON")

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
        return _rejeita_nul(v, "campo JSON")

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

    # AUDIT-2026-08-F2 — STRING VAZIA e NULL EXPLICITO querem coisas OPOSTAS,
    # e os dois consumidores deste endpoint ja os usam assim.
    #
    #   n8n  `Tool Atualizar Lead` tem `jsonBody` FIXO: manda as doze chaves em
    #        TODA chamada, com "" no que nao foi coletado ("Campos sem informacao
    #        devem ficar vazios", diz o proprio toolDescription).
    #        "" significa NAO INFORMADO -> nao encoste no campo.
    #   UI   templates/partials/_lead_edit_modal.html:685-700 manda
    #        `|| null` em todo campo opcional. `null` significa LIMPE ESTE CAMPO.
    #
    # `exclude_unset` no router remove o que nao foi ENVIADO — nao remove o que
    # foi enviado como "". Sem isto, uma atualizacao rotineira da Bia APAGAVA o
    # whatsapp e os destinos do lead (colunas anulaveis) e devolvia 500 no
    # `nome` (NOT NULL). Descartar a chave aqui e o que faz "" virar de fato
    # "nao informado": o campo deixa de estar em `model_fields_set`, e o
    # `exclude_unset` do router o ignora sozinho — sem lista de campos escrita a
    # mao e sem mudar o significado de `null`, que continua limpando.
    @model_validator(mode="before")
    @classmethod
    def descartar_strings_vazias(cls, dados):
        if not isinstance(dados, dict):
            return dados
        return {
            k: v for k, v in dados.items()
            if not (isinstance(v, str) and not v.strip())
        }


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
    # null so quando o caller pede include_total=false; quem nao envia o
    # parametro continua recebendo inteiro.
    total: Optional[int]
    skip: int
    limit: int
    leads: list[LeadResponse]
    # Aditivos: quem ja consome total/skip/limit/leads segue igual.
    next_cursor: Optional[str] = None
    has_more: bool = False


class ImportResponse(BaseModel):
    total_linhas: int
    importados: int
    erros: int
    detalhes_erros: list[str] = []
