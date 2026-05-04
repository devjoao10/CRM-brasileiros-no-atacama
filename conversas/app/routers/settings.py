"""
Settings API — Frases Automáticas + Horário Comercial.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_user, User
from app.models.auto_reply import AutoReply, BusinessHours
from app.schemas.settings import (
    AutoReplyUpdate,
    AutoReplyResponse,
    AutoReplyListResponse,
    BusinessHoursUpdate,
    BusinessHoursResponse,
    BusinessHoursListResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["Configurações"])


# ─── Auto Replies ────────────────────────────────

@router.get("/auto-replies", response_model=AutoReplyListResponse)
async def list_auto_replies(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Listar todas as frases automáticas."""
    replies = db.query(AutoReply).order_by(AutoReply.id).all()
    return AutoReplyListResponse(
        auto_replies=[AutoReplyResponse.model_validate(r) for r in replies],
        total=len(replies),
    )


@router.put("/auto-replies/{trigger}", response_model=AutoReplyResponse)
async def update_auto_reply(
    trigger: str,
    data: AutoReplyUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Editar uma frase automática pelo trigger."""
    reply = db.query(AutoReply).filter(AutoReply.trigger == trigger).first()
    if not reply:
        raise HTTPException(status_code=404, detail=f"Frase automática '{trigger}' não encontrada")

    if data.message is not None:
        reply.message = data.message
    if data.is_active is not None:
        reply.is_active = data.is_active

    db.commit()
    db.refresh(reply)
    logger.info(f"✅ Frase automática '{trigger}' atualizada")
    return AutoReplyResponse.model_validate(reply)


# ─── Business Hours ──────────────────────────────

@router.get("/business-hours", response_model=BusinessHoursListResponse)
async def list_business_hours(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Listar horário de todos os 7 dias da semana."""
    hours = db.query(BusinessHours).order_by(BusinessHours.weekday).all()
    return BusinessHoursListResponse(
        hours=[BusinessHoursResponse.model_validate(h) for h in hours],
    )


@router.put("/business-hours", response_model=BusinessHoursListResponse)
async def update_business_hours(
    data: BusinessHoursUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Atualizar horário comercial (batch — 7 dias de uma vez)."""
    for day in data.days:
        hours = db.query(BusinessHours).filter(BusinessHours.weekday == day.weekday).first()
        if hours:
            hours.is_open = day.is_open
            hours.open_time = day.open_time
            hours.close_time = day.close_time
        else:
            hours = BusinessHours(
                weekday=day.weekday,
                is_open=day.is_open,
                open_time=day.open_time,
                close_time=day.close_time,
            )
            db.add(hours)

    db.commit()
    logger.info("✅ Horário comercial atualizado")

    updated = db.query(BusinessHours).order_by(BusinessHours.weekday).all()
    return BusinessHoursListResponse(
        hours=[BusinessHoursResponse.model_validate(h) for h in updated],
    )
