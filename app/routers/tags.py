from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.tag import Tag, lead_tags
from app.models.lead import Lead
from app.models.user import User
from app.schemas.tag import (
    TagCreate,
    TagUpdate,
    TagResponse,
    TagListResponse,
    LeadTagsUpdate,
)
from app.auth import get_current_user

router = APIRouter(prefix="/api/tags", tags=["Tags"])


# ─── Tags CRUD ───────────────────────────────────────────

@router.get("", response_model=TagListResponse, summary="Listar todas as tags")
async def list_tags(
    search: Optional[str] = Query(None, description="Busca por nome"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Lista todas as tags disponíveis.
    
    **N8N**: Use para obter a lista de tags antes de associar a um lead.
    """
    query = db.query(Tag)
    if search:
        query = query.filter(Tag.nome.ilike(f"%{search}%"))
    
    tags = query.order_by(Tag.nome).all()
    return TagListResponse(
        total=len(tags),
        tags=[TagResponse.model_validate(t) for t in tags],
    )


@router.get("/{tag_id}", response_model=TagResponse, summary="Detalhes de uma tag")
async def get_tag(
    tag_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tag = db.query(Tag).filter(Tag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag não encontrada")
    return TagResponse.model_validate(tag)


@router.post("", response_model=TagResponse, status_code=201, summary="Criar tag")
async def create_tag(
    data: TagCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Cria uma nova tag com nome e cor.
    
    **N8N**: Crie tags dinamicamente antes de associá-las a leads.
    """
    existing = db.query(Tag).filter(Tag.nome == data.nome).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Já existe uma tag com este nome"
        )

    tag = Tag(nome=data.nome, cor=data.cor)
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return TagResponse.model_validate(tag)


@router.put("/{tag_id}", response_model=TagResponse, summary="Atualizar tag")
async def update_tag(
    tag_id: int,
    data: TagUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tag = db.query(Tag).filter(Tag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag não encontrada")

    update_data = data.model_dump(exclude_unset=True)

    if "nome" in update_data and update_data["nome"] != tag.nome:
        existing = db.query(Tag).filter(Tag.nome == update_data["nome"]).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Já existe uma tag com este nome"
            )

    for field, value in update_data.items():
        setattr(tag, field, value)

    db.commit()
    db.refresh(tag)
    return TagResponse.model_validate(tag)


@router.delete("/{tag_id}", summary="Excluir tag")
async def delete_tag(
    tag_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Exclui a tag e remove todas as associações com leads."""
    tag = db.query(Tag).filter(Tag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag não encontrada")

    db.delete(tag)
    db.commit()
    return {"message": f"Tag '{tag.nome}' excluída"}


# ─── Lead-Tag Association ────────────────────────────────

@router.put("/lead/{lead_id}", summary="Definir tags de um lead")
async def set_lead_tags(
    lead_id: int,
    data: LeadTagsUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Define as tags de um lead (substitui todas as tags existentes).
    
    **N8N**: Envie a lista completa de tag_ids para associar ao lead.
    
    ```json
    {"tag_ids": [1, 3, 5]}
    ```
    """
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    tags = db.query(Tag).filter(Tag.id.in_(data.tag_ids)).all()
    if len(tags) != len(data.tag_ids):
        found_ids = {t.id for t in tags}
        missing = [tid for tid in data.tag_ids if tid not in found_ids]
        raise HTTPException(status_code=404, detail=f"Tags não encontradas: {missing}")

    lead.tags = tags
    db.commit()

    return {
        "lead_id": lead_id,
        "tags": [TagResponse.model_validate(t) for t in lead.tags],
    }


@router.get("/lead/{lead_id}", summary="Tags de um lead")
async def get_lead_tags(
    lead_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retorna as tags associadas a um lead."""
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    return {
        "lead_id": lead_id,
        "tags": [TagResponse.model_validate(t) for t in lead.tags],
    }
