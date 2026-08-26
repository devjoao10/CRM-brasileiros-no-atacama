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
from app.services import crm as crm_service

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
    # CONV-TAGS-SYNC-01: conversa vinculada replica no lead do CRM
    # (cria a tag por NOME no CRM se faltar; falha em dev isolado so loga)
    if conv.lead_id and conv.lead_id > 0:
        crm_service.add_tag_to_lead(conv.lead_id, tag.nome, tag.cor, db)
    return conv.tags


@router.delete("/conversations/{conversation_id}/tags/{tag_id}", response_model=list[TagResponse])
async def remove_tag(
    conversation_id: int,
    tag_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conv, tag = _get_conv_and_tag(conversation_id, tag_id, db)
    # AUDIT-2026-08-WC (F-529): o CRM e sincronizado ANTES do commit local, e
    # nao depois. Antes disto o commit local acontecia primeiro e o resultado
    # de crm_service.remove_tag_from_lead era descartado (a funcao ja fazia o
    # DELETE e o commit de verdade — so o retorno True/False era ignorado
    # aqui). Se essa chamada falhasse, a tag reaparecia sozinha na proxima
    # abertura da conversa, porque sync_lead_tags_to_conversation espelha o
    # conjunto de tags do lead a cada abertura (fonte de verdade = CRM).
    # Sincronizando primeiro e falhando a request se nao der certo, a remocao
    # local so acontece quando ela vai sobreviver ao proximo reabrir.
    #
    # AUDIT-2026-08-WD — a recusa vale SO quando o espelho pode ressuscitar a
    # tag. `sync_lead_tags_to_conversation` desiste sem tocar nas tags locais
    # quando o CRM esta inacessivel (`get_lead_tags` devolve None) — nesse caso
    # a remocao local NAO reaparece, e recusar seria quebrar o inbox por um
    # risco que nao existe ali (foi o que aconteceu em dev isolado, onde as
    # tabelas do CRM nem existem). A sonda extra so roda no caminho de falha.
    if conv.lead_id and conv.lead_id > 0:
        removida_no_crm = crm_service.remove_tag_from_lead(conv.lead_id, tag.nome, db)
        if not removida_no_crm and crm_service.get_lead_tags(conv.lead_id, db) is not None:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Não foi possível remover a tag no CRM. A remoção foi "
                    "cancelada para evitar que a tag reaparecesse sozinha ao "
                    "reabrir a conversa."
                ),
            )
        if not removida_no_crm:
            logger.warning(
                "Tag %r removida so no Conversas: o CRM esta inacessivel "
                "(conversa %s, lead %s). O espelho tambem nao roda nesse "
                "estado, entao a tag nao volta sozinha — mas o CRM segue com "
                "ela ate a proxima sincronizacao.",
                tag.nome, conv.id, conv.lead_id,
            )
    if tag in conv.tags:
        conv.tags.remove(tag)
        db.commit()
    return conv.tags
