"""
Settings API — Frases Automáticas + Horário Comercial.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_user, User
from app.models.auto_reply import AUTO_REPLY_PADROES, AutoReply, BusinessHours
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
    """Listar as frases automáticas: o catálogo inteiro (com linha ou não) + linhas fora dele."""
    replies = db.query(AutoReply).order_by(AutoReply.id).all()

    # CONV-EXPEDIENTE-01: em produção o seed não roda e a tabela pode estar vazia. Os gatilhos do
    # catálogo vêm SEMPRE na ordem do catálogo — com a linha do banco quando existe, ou como
    # placeholder INATIVO, sem id e com o texto padrão (a linha nasce no 1º PUT). Assim a frase
    # não muda de posição ao ganhar a primeira linha. Linha fora do catálogo vai depois, por id.
    por_gatilho = {r.trigger: r for r in replies}
    itens = [
        AutoReplyResponse.model_validate(por_gatilho[padrao["trigger"]])
        if padrao["trigger"] in por_gatilho
        else AutoReplyResponse(
            trigger=padrao["trigger"],
            title=padrao["title"],
            message=padrao["message"],
            is_active=False,
        )
        for padrao in AUTO_REPLY_PADROES
    ]
    gatilhos_do_catalogo = {padrao["trigger"] for padrao in AUTO_REPLY_PADROES}
    itens += [AutoReplyResponse.model_validate(r) for r in replies if r.trigger not in gatilhos_do_catalogo]
    return AutoReplyListResponse(auto_replies=itens, total=len(itens))


@router.put("/auto-replies/{trigger}", response_model=AutoReplyResponse)
async def update_auto_reply(
    trigger: str,
    data: AutoReplyUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Editar uma frase automática pelo trigger (cria a linha se for um gatilho do catálogo)."""
    reply = db.query(AutoReply).filter(AutoReply.trigger == trigger).first()
    criada = reply is None
    if criada:
        # CONV-EXPEDIENTE-01: só gatilho do catálogo vira linha; qualquer outro continua 404.
        # Nasce com título/texto padrão e INATIVA; os campos do payload são aplicados abaixo.
        padrao = next((p for p in AUTO_REPLY_PADROES if p["trigger"] == trigger), None)
        if padrao is None:
            raise HTTPException(status_code=404, detail=f"Frase automática '{trigger}' não encontrada")
        reply = AutoReply(
            trigger=trigger,
            title=padrao["title"],
            message=padrao["message"],
            is_active=False,
        )
        db.add(reply)

    if data.message is not None:
        reply.message = data.message
    if data.is_active is not None:
        reply.is_active = data.is_active

    # CONV-EXPEDIENTE-01: frase ativa sem texto é frase que o webhook pula em silêncio.
    # Barra aqui e desfaz o que foi aplicado (a linha nova some; a existente mantém o texto).
    if reply.is_active and not (reply.message or "").strip():
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail="Escreva a mensagem antes de ativar a frase automatica.",
        )

    db.commit()
    db.refresh(reply)
    logger.info(f"✅ Frase automática '{trigger}' {'criada' if criada else 'atualizada'}")
    return AutoReplyResponse.model_validate(reply)


# ─── Business Hours ──────────────────────────────

def _business_hours_da_semana(db: Session) -> BusinessHoursListResponse:
    """Sempre os 7 dias (0=segunda ... 6=domingo), na ordem.

    CONV-EXPEDIENTE-01: dia sem linha no banco = aberto, sem horário — é o que o webhook já
    considera em `_is_within_business_hours` (sem configuração, assume aberto). Assim a tela
    mostra e salva a semana inteira mesmo com a tabela vazia (produção não roda o seed).
    """
    por_dia = {h.weekday: h for h in db.query(BusinessHours).all()}
    return BusinessHoursListResponse(
        hours=[
            BusinessHoursResponse.model_validate(por_dia[weekday])
            if weekday in por_dia
            else BusinessHoursResponse(weekday=weekday, is_open=True)
            for weekday in range(7)
        ],
    )


@router.get("/business-hours", response_model=BusinessHoursListResponse)
async def list_business_hours(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Listar horário de todos os 7 dias da semana."""
    return _business_hours_da_semana(db)


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

    return _business_hours_da_semana(db)
