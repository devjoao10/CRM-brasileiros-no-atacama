"""
Conversations CRUD API.
Includes responsavel (owner) management and CRM integration.
"""

import logging
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
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
    InitiateConversation,
    AssignRequest,
)
from app.services import whatsapp
from app.services import crm as crm_service
from app.services.outbound import (
    record_outbound_message,
    classify_wa_response,
    send_media_upload,
    MediaRejection,
)

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

    # CONV-08b: falha real agora vem como dict {"error": True, "summary": <seguro>}
    if result is None or (isinstance(result, dict) and result.get("error")):
        summary = result.get("summary") if isinstance(result, dict) else None
        detail = "Falha ao enviar mensagem via WhatsApp"
        if summary:
            detail += f": {summary}"
        raise HTTPException(status_code=502, detail=detail)

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


# ─── Initiate New Conversation ────────────────────────────────────────

@router.post("/initiate", summary="Iniciar conversa com lead via template")
async def initiate_conversation(
    data: InitiateConversation,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Inicia uma conversa com um número WhatsApp.
    
    - Se já existir uma conversa com esse número, retorna a conversa existente.
    - Se não existir, cria uma nova conversa e envia o template especificado.
    - Vincula ao lead_id se fornecido.
    
    Retorna: { conversation_id, created, message_sent }
    """
    # Normaliza o número (remove não-dígitos)
    wpp_clean = ''.join(c for c in data.whatsapp if c.isdigit())
    if not wpp_clean.startswith('55'):
        wpp_clean = '55' + wpp_clean

    # Busca conversa existente por WhatsApp
    conversation = db.query(Conversation).filter(
        Conversation.whatsapp == wpp_clean
    ).first()

    created = False
    message_sent = False

    if not conversation:
        # Cria nova conversa
        conversation = Conversation(
            whatsapp=wpp_clean,
            nome=data.nome or wpp_clean,
            lead_id=data.lead_id or 0,
            status='aberta',
            is_bot_active=False,  # humano inicia o contato
            unread_count=0,
        )
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
        created = True
        logger.info(f"Nova conversa criada para {wpp_clean} (lead_id={data.lead_id})")
    else:
        # Vincula ao lead_id se ainda não vinculada e foi fornecido
        if data.lead_id and (not conversation.lead_id or conversation.lead_id <= 0):
            conversation.lead_id = data.lead_id
            if data.nome and not conversation.nome:
                conversation.nome = data.nome
            db.commit()
            db.refresh(conversation)

    # Envia o template se fornecido
    if data.template_name:
        try:
            from app.models.template import MessageTemplate
            t = db.query(MessageTemplate).filter(
                MessageTemplate.name == data.template_name
            ).first()
            lang = t.language if t else (data.template_language or 'pt_BR')
            body_text = t.body_text if t else data.template_name

            wa_response = await whatsapp.send_template_message(
                to=wpp_clean,
                template_name=data.template_name,
                language=lang,
                components=[],
                db=db,
            )

            # CONV-08b: persiste com status fiel ao resultado (nunca 'sent' em falha);
            # preview so e atualizado em sucesso (dentro do helper).
            msg = record_outbound_message(
                db, conversation, body_text, 'template', wa_response,
                update_preview=True,
            )
            message_sent = (msg.status == 'sent')
            if message_sent:
                logger.info(f"Template '{data.template_name}' enviado para {wpp_clean}")
        except Exception as e:
            logger.error(f"Erro ao enviar template para {wpp_clean}: {e}")

    return {
        "conversation_id": conversation.id,
        "created": created,
        "message_sent": message_sent,
        "whatsapp": wpp_clean,
    }


# ─── Conversations CRUD ─────────────────────────────────────────────

@router.get("", response_model=ConversationListResponse)
async def list_conversations(
    status: Optional[str] = Query(None, description="Filtrar por status: aberta, encerrada, aguardando"),
    search: Optional[str] = Query(None, description="Buscar por nome ou WhatsApp"),
    responsavel_id: Optional[int] = Query(None, description="Filtrar por responsavel (0 = Agente IA)"),
    tag_id: Optional[int] = Query(None, description="CONV-05: filtrar por tag aplicada"),
    queue: Optional[str] = Query(None, description="CONV-06: 'fila' (aberta sem atendente, mais antiga primeiro) | 'em_atendimento'"),
    atendente_id: Optional[int] = Query(None, description="CONV-07: filtrar por atendente (0 = sem atendente)"),
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

    if tag_id is not None:
        # CONV-05: join no link table N:N
        from app.models.tag import ConversationTag
        query = query.filter(Conversation.tags.any(ConversationTag.id == tag_id))

    if atendente_id is not None:
        # CONV-07: filtro por atendente ("minhas" = frontend passa o proprio id)
        if atendente_id == 0:
            query = query.filter(Conversation.atendente_id.is_(None))
        else:
            query = query.filter(Conversation.atendente_id == atendente_id)

    # CONV-06: fila e ESTADO DERIVADO (fonte unica: status + atendente_id).
    # 'aguardando' nao existe como valor persistido — elimina ambiguidade.
    order_clause = desc(Conversation.updated_at)
    if queue == "fila":
        query = query.filter(
            Conversation.status == "aberta",
            Conversation.atendente_id.is_(None),
        )
        # quem espera ha mais tempo primeiro
        order_clause = Conversation.last_customer_msg_at.asc()
    elif queue == "em_atendimento":
        query = query.filter(
            Conversation.status == "aberta",
            Conversation.atendente_id.isnot(None),
        )
    elif queue is not None:
        raise HTTPException(status_code=422, detail="queue invalida (use 'fila' ou 'em_atendimento')")

    total = query.count()
    conversations = query.order_by(order_clause).offset(offset).limit(limit).all()

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

    # CONV-TAGS-SYNC-01: espelha as tags do lead (CRM) ao abrir a conversa
    # (read-repair; no-op se conversa sem lead ou CRM inacessivel em dev)
    crm_service.sync_lead_tags_to_conversation(conversation, db)

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
        # CONV-06: whitelist — 'aguardando' e derivado (aberta+sem atendente),
        # nunca persistido; transicao invalida e rejeitada explicitamente.
        if data.status not in ("aberta", "encerrada"):
            raise HTTPException(
                status_code=422,
                detail="Status invalido (use 'aberta' ou 'encerrada'; 'aguardando' e derivado)",
            )
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
            # CONV-08b: status fiel ao resultado do envio (nunca 'sent' em falha).
            wa_response = await whatsapp.send_text_message(conversation.whatsapp, reply.message, db)
            record_outbound_message(
                db, conversation, reply.message, "text", wa_response,
                update_preview=False,
            )

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

    if data.msg_type == "text":
        wa_response = await whatsapp.send_text_message(conversation.whatsapp, data.content, db)
    elif data.msg_type == "template" and data.template_name:
        # Puxa o template do banco para checar o idioma (default pt_BR)
        from app.models.template import MessageTemplate
        t = db.query(MessageTemplate).filter(MessageTemplate.name == data.template_name).first()
        lang = t.language if t else "pt_BR"
        wa_response = await whatsapp.send_template_message(
            to=conversation.whatsapp,
            template_name=data.template_name,
            language=lang,
            components=[],  # TODO: extract variables from content if needed
            db=db
        )
    elif data.media_url:
        wa_response = await whatsapp.send_media_message(
            conversation.whatsapp, data.msg_type, data.media_url, data.content, db
        )

    # CONV-08/CONV-08b: persistencia centralizada com status fiel ao resultado.
    # Sucesso -> 'sent' + wamid + preview/unread; falha -> 'failed' + last_error
    # seguro, preview intacto; simulado (dev) -> 'sent' explicito sem wamid.
    message = record_outbound_message(
        db, conversation, data.content, data.msg_type, wa_response,
        media_url=data.media_url, update_preview=True, reset_unread=True,
    )

    if message.status == "failed":
        raise HTTPException(
            status_code=502,
            detail="Nao foi possivel enviar a mensagem pelo WhatsApp. Tente novamente.",
        )

    logger.info(f"Mensagem enviada para {conversation.nome} ({conversation.whatsapp})")
    return MessageResponse.model_validate(message)


@router.post("/{conversation_id}/claim", response_model=ConversationResponse)
async def claim_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    CONV-06: assumir a conversa (tira da fila). TRAVA anti-duplo-atendimento:
    se outro atendente ja assumiu, 409. O atendente e SEMPRE o usuario
    autenticado (nunca vem do request).
    """
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversa nao encontrada")
    if conversation.status != "aberta":
        raise HTTPException(status_code=409, detail="Conversa encerrada — reabra antes de assumir")
    if conversation.atendente_id and conversation.atendente_id != current_user.id:
        raise HTTPException(status_code=409, detail="Conversa ja esta em atendimento por outro usuario")
    conversation.atendente_id = current_user.id
    db.commit()
    db.refresh(conversation)
    logger.info(f"Conversa {conversation_id} assumida pelo usuario {current_user.id}")
    return ConversationResponse.model_validate(conversation)


@router.post("/{conversation_id}/assign", response_model=ConversationResponse)
async def assign_conversation(
    conversation_id: int,
    data: AssignRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    CONV-07: atribuicao dirigida/handoff — atribui a QUALQUER usuario ativo
    (diferente do claim, que atribui a si mesmo com trava). Reatribuicao e
    permitida por design (handoff entre atendentes).
    """
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversa nao encontrada")
    if conversation.status != "aberta":
        raise HTTPException(status_code=409, detail="Conversa encerrada — reabra antes de atribuir")
    target = db.query(User).filter(User.id == data.user_id, User.is_active == True).first()  # noqa: E712
    if not target:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado ou inativo")
    conversation.atendente_id = target.id
    db.commit()
    db.refresh(conversation)
    logger.info(
        f"Conversa {conversation_id} atribuida ao usuario {target.id} por {current_user.id} (handoff)"
    )
    return ConversationResponse.model_validate(conversation)


@router.post("/{conversation_id}/release", response_model=ConversationResponse)
async def release_conversation(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    CONV-06: liberar a conversa de volta para a fila (idempotente).
    Handoff dirigido (reatribuir a alguem) = CONV-07.
    """
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversa nao encontrada")
    if conversation.atendente_id is not None:
        conversation.atendente_id = None
        db.commit()
        db.refresh(conversation)
        logger.info(f"Conversa {conversation_id} devolvida a fila pelo usuario {current_user.id}")
    return ConversationResponse.model_validate(conversation)


@router.post("/{conversation_id}/messages/media", response_model=MessageResponse)
async def send_media_message_upload(
    conversation_id: int,
    file: UploadFile = File(...),
    caption: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    CONV-03: envio outbound de midia por upload (multipart).
    Rota fina: valida conversa -> delega a outbound.send_media_upload
    (politica -> upload Meta -> send por media_id -> integridade CONV-08b).
    Politica rejeita com 415/413 SEM persistir nada; falha de provider
    persiste 'failed' (retry possivel) e responde 502 seguro.
    """
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversa nao encontrada")

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Arquivo vazio")

    try:
        message, _asset = await send_media_upload(
            db, conversation,
            content=content,
            mime_type=file.content_type or "",
            caption=caption or "",
            filename=file.filename,
        )
    except MediaRejection as e:
        raise HTTPException(status_code=e.status_code, detail=e.reason)

    if message.status == "failed":
        raise HTTPException(
            status_code=502,
            detail="Nao foi possivel enviar a midia pelo WhatsApp. Tente novamente.",
        )
    logger.info(f"Midia enviada para {conversation.nome} ({conversation.whatsapp})")
    return MessageResponse.model_validate(message)


@router.post("/{conversation_id}/messages/{message_id}/retry", response_model=MessageResponse)
async def retry_message(
    conversation_id: int,
    message_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    CONV-08b: reenvio MANUAL de uma mensagem outbound que falhou.

    Regras estritas:
    - so mensagens outbound;
    - so status 'failed' (nunca reenvia 'sent'/'delivered'/'read' — sem duplicar);
    - so texto ou midia com media_url (template nao e reenviavel: o conteudo
      salvo e o corpo renderizado, nao os parametros do template).
    Atualiza a MESMA linha (sem duplicar mensagem), incrementa send_attempts
    e last_attempt_at, e limpa/atualiza last_error conforme o resultado.
    """
    conversation = db.query(Conversation).filter(
        Conversation.id == conversation_id
    ).first()
    if not conversation:
        raise HTTPException(status_code=404, detail="Conversa nao encontrada")

    message = db.query(Message).filter(
        Message.id == message_id,
        Message.conversation_id == conversation_id,
    ).first()
    if not message:
        raise HTTPException(status_code=404, detail="Mensagem nao encontrada")

    if message.direction != "outbound":
        raise HTTPException(status_code=400, detail="Apenas mensagens enviadas podem ser reenviadas")
    if message.status != "failed":
        raise HTTPException(status_code=409, detail="Apenas mensagens com falha podem ser reenviadas")

    if message.msg_type == "text":
        wa_response = await whatsapp.send_text_message(conversation.whatsapp, message.content, db)
    elif message.msg_type in ("image", "audio", "document", "video"):
        # CONV-03: midia com espelho local (upload do operador) -> re-upload + send
        from app.services import media_storage
        asset = message.media_asset
        local = media_storage.resolve_local_file(asset) if asset else None
        if local is not None:
            up = await whatsapp.upload_media(local.read_bytes(), asset.meta_mime_type or "application/octet-stream", db)
            if not isinstance(up, dict) or up.get("error"):
                wa_response = {
                    "error": True,
                    "summary": (up.get("summary") if isinstance(up, dict) else None)
                    or "falha no re-upload da midia",
                }
            elif up.get("simulated"):
                wa_response = {"simulated": True}
            else:
                asset.meta_media_id = up.get("id")
                wa_response = await whatsapp.send_media_message(
                    conversation.whatsapp, message.msg_type,
                    caption=message.content or "", db=db, media_id=up.get("id"),
                )
        elif message.media_url:
            wa_response = await whatsapp.send_media_message(
                conversation.whatsapp, message.msg_type, message.media_url, message.content, db
            )
        else:
            raise HTTPException(status_code=400, detail="Reenvio nao suportado para este tipo de mensagem")
    else:
        raise HTTPException(status_code=400, detail="Reenvio nao suportado para este tipo de mensagem")

    r = classify_wa_response(wa_response)
    message.send_attempts = (message.send_attempts or 0) + 1
    message.last_attempt_at = datetime.now(timezone.utc)

    if r["ok"]:
        message.status = "sent"
        message.whatsapp_msg_id = r["wamid"]
        message.last_error = None
        conversation.ultimo_msg = (message.content or "")[:200]
        db.commit()
        db.refresh(message)
        logger.info(f"Reenvio OK da mensagem {message.id} (conversa {conversation.id})")
        return MessageResponse.model_validate(message)

    message.last_error = r["error_summary"]
    db.commit()
    logger.warning(
        f"Reenvio FALHOU da mensagem {message.id} (conversa {conversation.id}): {r['error_summary']}"
    )
    raise HTTPException(
        status_code=502,
        detail="Reenvio falhou. Tente novamente.",
    )


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
