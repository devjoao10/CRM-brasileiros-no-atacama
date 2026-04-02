from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime

class TeamBase(BaseModel):
    nome: str = Field(..., min_length=1, max_length=100, description="Nome da equipe")
    descricao: Optional[str] = Field(None, max_length=255)
    cor: str = Field(default="#2B6CB0", max_length=7, description="Cor em HEX")

class TeamCreate(TeamBase):
    pass

class TeamUpdate(BaseModel):
    nome: Optional[str] = Field(None, min_length=1, max_length=100)
    descricao: Optional[str] = Field(None, max_length=255)
    cor: Optional[str] = Field(None, max_length=7)

class TeamResponse(BaseModel):
    id: int
    nome: str
    descricao: Optional[str] = None
    cor: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class TeamListResponse(BaseModel):
    total: int
    teams: list[TeamResponse]
