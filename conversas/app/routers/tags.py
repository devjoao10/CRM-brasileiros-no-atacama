"""
CONV-05 — Tags do Conversas.

CRUD de tags + aplicar/remover em conversas. Rotas finas, todas autenticadas
(get_current_user — mesmo modelo do restante do app; sem papel de admin
dedicado no Conversas hoje, decisao documentada no vault).

Seguranca: `nome` e escapado no frontend; `cor` e VALIDADA aqui (^#hex6$ via
schema) porque o frontend a usa em atributo style.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_user, User
from app.models.conversation import Conversation
from app.models.tag import ConversationTag
from app.schemas.conversation import TagResponse, TagCreate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["tags"])


# ─── CRUD de tags ────────────────────────────────────────────────────

@router.get("/tags", response_model=list[TagResponse])
async def list_tags(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return db.query(ConversationTag).order_by(ConversationTag.nome).all()


@router.post("/tags", response_model=TagResponse, status_code=201)
async def create_tag(
    data: TagCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    nome = data.nome.strip()
    if not nome:
        raise HTTPException(status_code=422, detail="Nome da tag vazio")
    existing = db.query(ConversationTag).filter(ConversationTag.nome == nome).first()
    if existing:
        raise HTTPException(status_code=409, detail="Ja existe uma tag com esse nome")
    tag = ConversationTag(nome=nome, cor=data.cor)
    db.add(tag)
    db.commit()
    db.refresh(tag)
    logger.info(f"Tag criada: {tag.nome}")
    return tag


@router.put("/tags/{tag_id}", response_model=TagResponse)
async def update_tag(
    tag_id: int,
    data: TagCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tag = db.query(ConversationTag).filter(ConversationTag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag nao encontrada")
    nome = data.nome.strip()
    dup = db.query(ConversationTag).filter(
        ConversationTag.nome == nome, ConversationTag.id != tag_id
    ).first()
    if dup:
        raise HTTPException(status_code=409, detail="Ja existe uma tag com esse nome")
    tag.nome = nome
    tag.cor = data.cor
    db.commit()
    db.refresh(tag)
    return tag


@router.delete("/tags/{tag_id}", status_code=204)
async def delete_tag(
    tag_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tag = db.query(ConversationTag).filter(ConversationTag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag nao encontrada")
    tag.conversations = []  # limpa links explicitamente (portavel; FK CASCADE cobre o resto)
    db.delete(tag)
    db.commit()
    logger.info(f"Tag removida: {tag_id}")


# ─── Aplicar/remover tag em conversa ─────────────────────────────────

def _get_conv_and_tag(conversation_id: int, tag_id: int, db: Session):
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversa nao encontrada")
    tag = db.query(ConversationTag).filter(ConversationTag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag nao encontrada")
    return conv, tag


@router.post("/conversations/{conversation_id}/tags/{tag_id}", response_model=list[TagResponse])
async def apply_tag(
    conversation_id: int,
    tag_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Aplica a tag (idempotente — aplicar 2x nao duplica)."""
    conv, tag = _get_conv_and_tag(conversation_id, tag_id, db)
    if tag not in conv.tags:
        conv.tags.append(tag)
        db.commit()
    return conv.tags


@router.delete("/conversations/{conversation_id}/tags/{tag_id}", response_model=list[TagResponse])
async def remove_tag(
    conversation_id: int,
    tag_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conv, tag = _get_conv_and_tag(conversation_id, tag_id, db)
    if tag in conv.tags:
        conv.tags.remove(tag)
        db.commit()
    return conv.tags
