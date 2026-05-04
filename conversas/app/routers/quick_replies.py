"""
Quick Replies CRUD API — Mensagens rápidas para atendentes.
"""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.database import get_db
from app.auth import get_current_user, User
from app.models.quick_reply import QuickReply
from app.schemas.quick_reply import (
    QuickReplyCreate,
    QuickReplyUpdate,
    QuickReplyResponse,
    QuickReplyListResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/quick-replies", tags=["Mensagens Rápidas"])


@router.post("", response_model=QuickReplyResponse, status_code=201)
async def create_quick_reply(
    data: QuickReplyCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Criar nova mensagem rápida."""
    # Verificar duplicata
    existing = db.query(QuickReply).filter(QuickReply.shortcut == data.shortcut).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Atalho '{data.shortcut}' já existe")

    qr = QuickReply(
        shortcut=data.shortcut,
        title=data.title,
        content=data.content,
        category=data.category,
    )
    db.add(qr)
    db.commit()
    db.refresh(qr)

    logger.info(f"✅ Mensagem rápida criada: {qr.shortcut}")
    return QuickReplyResponse.model_validate(qr)


@router.get("", response_model=QuickReplyListResponse)
async def list_quick_replies(
    search: Optional[str] = Query(None, description="Buscar por atalho, título ou categoria"),
    category: Optional[str] = Query(None),
    active_only: bool = Query(True),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Listar todas as mensagens rápidas."""
    query = db.query(QuickReply)

    if active_only:
        query = query.filter(QuickReply.is_active == True)

    if category:
        query = query.filter(QuickReply.category == category)

    if search:
        term = f"%{search}%"
        query = query.filter(
            or_(
                QuickReply.shortcut.ilike(term),
                QuickReply.title.ilike(term),
                QuickReply.category.ilike(term),
                QuickReply.content.ilike(term),
            )
        )

    total = query.count()
    replies = query.order_by(QuickReply.shortcut).all()

    return QuickReplyListResponse(
        quick_replies=[QuickReplyResponse.model_validate(r) for r in replies],
        total=total,
    )


@router.get("/search", response_model=QuickReplyListResponse)
async def search_quick_replies(
    q: str = Query(..., min_length=1, description="Termo de busca (sem o /)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Busca rápida para autocomplete no chat (quando o atendente digita /)."""
    term = f"%{q}%"
    replies = db.query(QuickReply).filter(
        QuickReply.is_active == True,
        or_(
            QuickReply.shortcut.ilike(f"%/{q}%"),
            QuickReply.shortcut.ilike(term),
            QuickReply.title.ilike(term),
            QuickReply.content.ilike(term),
        )
    ).order_by(QuickReply.shortcut).limit(10).all()

    return QuickReplyListResponse(
        quick_replies=[QuickReplyResponse.model_validate(r) for r in replies],
        total=len(replies),
    )


@router.get("/{qr_id}", response_model=QuickReplyResponse)
async def get_quick_reply(
    qr_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Detalhe de uma mensagem rápida."""
    qr = db.query(QuickReply).filter(QuickReply.id == qr_id).first()
    if not qr:
        raise HTTPException(status_code=404, detail="Mensagem rápida não encontrada")
    return QuickReplyResponse.model_validate(qr)


@router.put("/{qr_id}", response_model=QuickReplyResponse)
async def update_quick_reply(
    qr_id: int,
    data: QuickReplyUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Editar mensagem rápida."""
    qr = db.query(QuickReply).filter(QuickReply.id == qr_id).first()
    if not qr:
        raise HTTPException(status_code=404, detail="Mensagem rápida não encontrada")

    if data.shortcut is not None:
        # Check for duplicate
        existing = db.query(QuickReply).filter(
            QuickReply.shortcut == data.shortcut, QuickReply.id != qr_id
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail=f"Atalho '{data.shortcut}' já existe")
        qr.shortcut = data.shortcut
    if data.title is not None:
        qr.title = data.title
    if data.content is not None:
        qr.content = data.content
    if data.category is not None:
        qr.category = data.category
    if data.is_active is not None:
        qr.is_active = data.is_active

    db.commit()
    db.refresh(qr)
    return QuickReplyResponse.model_validate(qr)


@router.delete("/{qr_id}")
async def delete_quick_reply(
    qr_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Deletar mensagem rápida."""
    qr = db.query(QuickReply).filter(QuickReply.id == qr_id).first()
    if not qr:
        raise HTTPException(status_code=404, detail="Mensagem rápida não encontrada")

    db.delete(qr)
    db.commit()
    logger.info(f"🗑️ Mensagem rápida deletada: {qr.shortcut}")
    return {"message": f"Mensagem rápida '{qr.shortcut}' deletada"}
