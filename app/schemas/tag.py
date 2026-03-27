from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime


class TagBase(BaseModel):
    nome: str = Field(..., min_length=1, max_length=100, description="Nome da tag")
    cor: str = Field(default="#2B6CB0", max_length=7, description="Cor hex da tag (ex: #FF5733)")


class TagCreate(TagBase):
    pass


class TagUpdate(BaseModel):
    nome: Optional[str] = Field(None, min_length=1, max_length=100)
    cor: Optional[str] = Field(None, max_length=7)


class TagResponse(BaseModel):
    id: int
    nome: str
    cor: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TagListResponse(BaseModel):
    total: int
    tags: list[TagResponse]


class LeadTagsUpdate(BaseModel):
    tag_ids: list[int] = Field(..., description="Lista de IDs das tags para associar ao lead")
