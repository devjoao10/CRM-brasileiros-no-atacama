from pydantic import BaseModel, Field, model_validator
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
    """
    AUDIT-2026-08-WC (C1): dois modos, mutuamente exclusivos.

    - `tag_ids`: SUBSTITUI a lista inteira de tags do lead (modo original,
      usado pelo n8n — `Tool Definir Tags Lead` continua enviando isto e nao
      pode quebrar).
    - `adicionar`/`remover`: altera so os IDs informados, sem tocar no resto.
      Existe porque `tag_ids` a partir de um snapshot desatualizado (editor
      aberto ha um tempo, outra origem mexeu nas tags nesse meio-tempo) apaga
      em silencio qualquer tag que o snapshot nao conhecia.
    """
    tag_ids: Optional[list[int]] = Field(
        None, description="Lista COMPLETA de tag_ids para substituir todas as tags do lead."
    )
    adicionar: Optional[list[int]] = Field(
        None, description="IDs de tags para adicionar, sem remover as demais."
    )
    remover: Optional[list[int]] = Field(
        None, description="IDs de tags para remover, sem tocar nas demais."
    )

    @model_validator(mode="after")
    def _valida_modo_exclusivo(self):
        incremental = self.adicionar is not None or self.remover is not None
        if self.tag_ids is not None and incremental:
            raise ValueError(
                "Use 'tag_ids' (lista completa) OU 'adicionar'/'remover' "
                "(incremental) — nunca os dois juntos na mesma chamada."
            )
        if self.tag_ids is None and not incremental:
            raise ValueError(
                "Envie 'tag_ids' para substituir a lista completa, ou "
                "'adicionar'/'remover' para alteração incremental."
            )
        return self
