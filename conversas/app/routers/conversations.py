"""
Conversations CRUD API.
Includes responsavel (owner) management and CRM integration.
"""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.database import get_db
from app.auth import get_current_user, User
from app.models.conversation import Conversation, Message
from app.schemas.conversation import (
    ConversationResponse,
    ConversationDetail,
    ConversationListResponse,
    ConversationUpdate,
    MessageCreate,
    MessageResponse,
    NotificationCreate,
)
from app.services import whatsapp
from app.services import crm as crm_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/conversations", tags=["Conversas"])


# ─── N8N Integration: Send Notification ──────────────────────────────

@router.post("/send-notification", summary="Enviar notificação via WhatsApp")
async def send_notification(
    data: NotificationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Send a notification message via WhatsApp.
    Used by N8N workflows (WF-06, WF-08, WF-09) to send alerts and reports.

    Payload: {"to": "5511999999999", "message": "Texto da notificação"}
    """
    result = await whatsapp.send_text_message(data.to, data.message, db)

    if result is None:
        raise HTTPException(status_code=502, detail="Falha ao enviar mensagem via WhatsApp")

    # Simulated mode (API not configured)
    if result.get("simulated"):
        logger.info(f"Notificação simulada para {data.to}: {data.message[:50]}...")
        return {
            "success": True,
            "simulated": True,
            "message": "API WhatsApp não configurada — mensagem simulada",
        }

    # Real send
    msg_id = None
    messages = result.get("messages", [])
    if messages:
        msg_id = messages[0].get("id")

    logger.info(f"Notificação enviada para {data.to}: wamid={msg_id}")
    return {
        "success": True,
        "message_id": msg_id,
    }


# ─── Conversations CRUD ─────────────────────────────────────────────

@router.get("", response_model=ConversationListResponse)
async def list_conversations(
    status: Optional[str] = Query(None, description="Filtrar por status: aberta, encerrada, aguardando"),
    search: Optional[str] = Query(None, description="Buscar por nome ou WhatsApp"),
    responsavel_id: Optional[int] = Query(None, description="Filtrar por responsavel (0 = Agente IA)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List all conversations with optional filters."""
    query = db.query(Conversation)

    if status:
        query = query.filter(Conversation.status == status)

    if search:
        search_term = f"%{search}%"
        query = query.filter(
            (Conversation.nome.ilike(search_term)) |
            (Conversation.whatsapp.ilike(search_term))
        )

    if responsavel_id is not None:
        if responsavel_id == 0:
            query = query.filter(Conversation.responsavel_id.is_(None))
        else:
            query = query.filter(Conversation.responsavel_id == responsavel_id)

    total = query.count()
    conversations = query.order_by(desc(Conversation.updated_at)).offset(offset).limit(limit).all()

    return ConversationListResponse(
        conversations=[ConversationResponse.model_validate(c) for c in conversations],
        total=total,
    )


@router.get("/by-lead/{lead_id}", response_model=Optional[ConversationResponse])
async def get_conversation_by_lead(
    lead_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Find a conversation linked to a specific lead."""
    conversation = db.query(Conversation).filter(
        Conversation.lead_id == lead_id
    ).first()

    if not conversation:
        raise HTTPException(status_code=404, detail="Nenhuma conversa encontrada para este lead")

    return ConversationResponse.model_validate(conversation)


@router.get("/users", summary="Listar usuarios para seletor de responsavel")
async def list_users_for_responsavel(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List active users from the shared users table."""
    users = await crm_service.get_users_list(db)
    return {"users": users}


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get a conversation with all its messages."""
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id
    ).first()

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversa nao encontrada")

    # Mark as read
    conversation.unread_count = 0
    db.commit()

    return ConversationDetail.model_validate(conversation)


@router.put("/{conversation_id}", response_model=ConversationResponse)
async def update_conversation(
    conversation_id: int,
    data: ConversationUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update conversation status, assignee, or responsavel."""
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id
    ).first()

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversa nao encontrada")

    old_status = conversation.status

    if data.status is not None:
        conversation.status = data.status
    if data.atendente_id is not None:
        conversation.atendente_id = data.atendente_id
    if data.is_bot_active is not None:
        conversation.is_bot_active = data.is_bot_active

    # Update responsavel
    if data.responsavel_id is not None:
        conversation.responsavel_id = data.responsavel_id if data.responsavel_id != 0 else None
        # Get name
        if data.responsavel_id == 0 or data.responsavel_id is None:
            conversation.responsavel_nome = "Agente IA"
        else:
            user = db.query(User).filter(User.id == data.responsavel_id).first()
            conversation.responsavel_nome = user.nome if user else None

    db.commit()
    db.refresh(conversation)

    # Sync responsavel to CRM
    if data.responsavel_id is not None and conversation.lead_id and conversation.lead_id > 0:
        real_resp_id = None if data.responsavel_id == 0 else data.responsavel_id
        await crm_service.sync_responsavel_to_crm(conversation.lead_id, real_resp_id, db)

    # Send auto-reply for status changes
    from app.models.auto_reply import AutoReply

    if data.status == "encerrada" and old_status != "encerrada":
        reply = db.query(AutoReply).filter(
            AutoReply.trigger == "end_service",
            AutoReply.is_active == True,
        ).first()
        if reply and reply.message and reply.message.strip():
            await whatsapp.send_text_message(conversation.whatsapp, reply.message, db)
            msg = Message(
                conversation_id=conversation.id,
                direction="outbound",
                content=reply.message,
                msg_type="text",
                status="sent",
            )
            db.add(msg)
            db.commit()

    return ConversationResponse.model_validate(conversation)


@router.put("/{conversation_id}/responsavel")
async def update_responsavel(
    conversation_id: int,
    responsavel_id: Optional[int] = Query(None, description="ID do responsavel (null ou 0 = Agente IA)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update conversation responsavel and sync to CRM."""
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id
    ).first()

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversa nao encontrada")

    real_resp_id = None if (responsavel_id is None or responsavel_id == 0) else responsavel_id

    conversation.responsavel_id = real_resp_id
    if real_resp_id is None:
        conversation.responsavel_nome = "Agente IA"
    else:
        user = db.query(User).filter(User.id == real_resp_id).first()
        conversation.responsavel_nome = user.nome if user else None

    db.commit()

    # Sync to CRM
    if conversation.lead_id and conversation.lead_id > 0:
        await crm_service.sync_responsavel_to_crm(conversation.lead_id, real_resp_id, db)

    return {
        "message": f"Responsavel atualizado para {conversation.responsavel_nome or 'Agente IA'}",
        "responsavel_id": conversation.responsavel_id,
        "responsavel_nome": conversation.responsavel_nome,
    }


@router.get("/{conversation_id}/crm-link")
async def get_crm_link(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Get CRM pipeline link for a conversation's lead."""
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id
    ).first()

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversa nao encontrada")

    if not conversation.lead_id or conversation.lead_id <= 0:
        # Try to auto-link first
        linked = await crm_service.auto_link_conversation(conversation, db)
        if not linked:
            raise HTTPException(status_code=404, detail="Lead nao vinculado ao CRM")

    # Get pipeline info
    pipeline_info = await crm_service.get_lead_pipeline_info(conversation.lead_id, db)

    from app.config import CRM_BASE_URL
    crm_url = CRM_BASE_URL.replace(":8000", ":8000")  # Same base URL

    return {
        "lead_id": conversation.lead_id,
        "crm_base_url": crm_url,
        "pipeline_url": f"{crm_url}/pipeline" + (f"?lead_id={conversation.lead_id}" if pipeline_info else ""),
        "lead_url": f"{crm_url}/leads?search={conversation.whatsapp}",
        "pipeline_info": pipeline_info,
    }


@router.post("/{conversation_id}/messages", response_model=MessageResponse)
async def send_message(
    conversation_id: int,
    data: MessageCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Send a message in a conversation (outbound)."""
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id
    ).first()

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversa nao encontrada")

    # Send via WhatsApp API
    wa_response = None
    wa_msg_id = None

    if data.msg_type == "text":
        wa_response = await whatsapp.send_text_message(conversation.whatsapp, data.content, db)
    elif data.media_url:
        wa_response = await whatsapp.send_media_message(
            conversation.whatsapp, data.msg_type, data.media_url, data.content, db
        )

    if wa_response and "messages" in wa_response:
        wa_msg_id = wa_response["messages"][0].get("id")

    # Save message in DB
    message = Message(
        conversation_id=conversation.id,
        direction="outbound",
        content=data.content,
        msg_type=data.msg_type,
        media_url=data.media_url,
        whatsapp_msg_id=wa_msg_id,
        status="sent",
    )
    db.add(message)

    # Update conversation preview
    conversation.ultimo_msg = data.content[:200]
    conversation.unread_count = 0

    db.commit()
    db.refresh(message)

    logger.info(f"Mensagem enviada para {conversation.nome} ({conversation.whatsapp})")
    return MessageResponse.model_validate(message)


@router.post("/{conversation_id}/auto-link")
async def auto_link_to_crm(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manually trigger auto-linking a conversation to a CRM lead."""
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id
    ).first()

    if not conversation:
        raise HTTPException(status_code=404, detail="Conversa nao encontrada")

    linked = await crm_service.auto_link_conversation(conversation, db)
    if linked:
        return {
            "message": f"Conversa vinculada ao lead CRM #{conversation.lead_id}",
            "lead_id": conversation.lead_id,
            "responsavel_nome": conversation.responsavel_nome,
        }
    else:
        raise HTTPException(status_code=404, detail="Nenhum lead encontrado no CRM com este WhatsApp")
