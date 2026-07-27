"""
CONV-VAR-01 — Contratos de API do modulo de variaveis.

Regras do token (validadas aqui, antes de tocar o banco):
  1. comeca com `@`;  2. depois do `@`, apenas letras, numeros e underscore;
  3. armazenado em MAIUSCULAS;  4. sem espacos;  5. sem duplicidade (409 no
  router);  6. nunca vazio;  7. nao pode ser um token reservado do sistema.
"""

from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.services.variables import (
    CATALOG,
    RESERVED_TOKENS,
    TOKEN_FULL_PATTERN,
    normalize_token,
)

_TOKEN_HELP = (
    "O token deve começar com @, ter no mínimo 2 caracteres e conter apenas "
    "letras, números e underscore, começando e terminando em letra ou número "
    "(ex.: @PRIMEIRONOMECLIENTE)."
)


def _validate_token(raw: str) -> str:
    token = normalize_token(raw)
    if token in ("", "@"):
        raise ValueError("Informe o token da variável. " + _TOKEN_HELP)
    if not TOKEN_FULL_PATTERN.match(token):
        raise ValueError(f"Token inválido: '{token}'. " + _TOKEN_HELP)
    if token in RESERVED_TOKENS:
        raise ValueError(f"O token {token} é reservado pelo sistema.")
    return token


class _VariableBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    kind: Literal["fixed", "dynamic"]
    fixed_value: Optional[str] = Field(None, max_length=5000)
    source_key: Optional[str] = Field(None, max_length=60)

    @model_validator(mode="after")
    def _check_kind_consistency(self):
        if self.kind == "dynamic":
            if not self.source_key:
                raise ValueError("Escolha a propriedade do sistema para uma variável do tipo Variável.")
            if self.source_key not in CATALOG:
                raise ValueError(f"Propriedade desconhecida: '{self.source_key}'.")
            self.fixed_value = None
        else:
            self.source_key = None
        return self


class MessageVariableCreate(_VariableBase):
    token: str = Field(..., max_length=61)

    @field_validator("token")
    @classmethod
    def _token(cls, value: str) -> str:
        return _validate_token(value)


class MessageVariableUpdate(BaseModel):
    """Atualizacao parcial — apenas os campos enviados sao alterados."""

    name: Optional[str] = Field(None, min_length=1, max_length=100)
    token: Optional[str] = Field(None, max_length=61)
    kind: Optional[Literal["fixed", "dynamic"]] = None
    fixed_value: Optional[str] = Field(None, max_length=5000)
    source_key: Optional[str] = Field(None, max_length=60)
    is_active: Optional[bool] = None

    @field_validator("token")
    @classmethod
    def _token(cls, value: Optional[str]) -> Optional[str]:
        return _validate_token(value) if value is not None else None

    @field_validator("source_key")
    @classmethod
    def _source(cls, value: Optional[str]) -> Optional[str]:
        if value and value not in CATALOG:
            raise ValueError(f"Propriedade desconhecida: '{value}'.")
        return value


class MessageVariableResponse(BaseModel):
    id: int
    name: str
    token: str
    kind: str
    fixed_value: Optional[str] = None
    source_key: Optional[str] = None
    source_label: Optional[str] = None
    is_active: bool
    created_by: Optional[int] = None
    created_by_nome: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

    @classmethod
    def from_model(cls, obj, created_by_nome: Optional[str] = None):
        prop = CATALOG.get(obj.source_key or "")
        return cls(
            id=obj.id,
            name=obj.name,
            token=obj.token,
            kind=obj.kind,
            fixed_value=obj.fixed_value,
            source_key=obj.source_key,
            source_label=prop.label if prop else None,
            is_active=obj.is_active,
            created_by=obj.created_by,
            created_by_nome=created_by_nome,
            created_at=obj.created_at,
            updated_at=obj.updated_at,
        )


class MessageVariableListResponse(BaseModel):
    variables: List[MessageVariableResponse]
    total: int


class CatalogOption(BaseModel):
    key: str
    label: str


class CatalogGroup(BaseModel):
    group: str
    options: List[CatalogOption]


class CatalogResponse(BaseModel):
    groups: List[CatalogGroup]


class PreviewRequest(BaseModel):
    text: str = Field(..., max_length=10000)
    conversation_id: Optional[int] = None


class PreviewProblem(BaseModel):
    token: str
    code: str
    short: str
    message: str


class PreviewResponse(BaseModel):
    rendered: str
    problems: List[PreviewProblem]
    ok: bool
