"""
Meta Cloud API Webhook receiver.
Handles verification (GET) and incoming messages (POST).
Includes automatic replies based on business hours and auto-reply settings.
"""

import logging
from datetime import datetime, timezone as tz

from fastapi import APIRouter, Request, HTTPException, Query, Depends
from sqlalchemy.orm import Session

from app.config import META_VERIFY_TOKEN, N8N_BASE_URL, N8N_AGENT_ENABLED
from app.database import get_db
from app.models.conversation import Conversation, Message
from app.models.auto_reply import AutoReply, BusinessHours
from app.models.api_config import ApiConfig
from app.services import whatsapp
from app.services import crm as crm_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Webhook"])


def _get_verify_token(db: Session) -> str:
    """Get verify token — DB config takes priority over env var."""
    config = db.query(ApiConfig).filter(ApiConfig.id == 1).first()
    if config and config.meta_verify_token:
        return config.meta_verify_token
    return META_VERIFY_TOKEN


@router.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    db: Session = Depends(get_db),
):
    """
    Meta Cloud API webhook verification.
    Meta sends a GET request to verify the webhook URL.
    """
    expected_token = _get_verify_token(db)

    if hub_mode == "subscribe" and hub_verify_token == expected_token and expected_token:
        logger.info("Webhook verificado com sucesso!")
        return int(hub_challenge)

    logger.warning(f"Webhook verification failed: mode={hub_mode}, token={hub_verify_token}")
    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/webhook")
async def receive_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Receive incoming messages from Meta Cloud API.
    Parses the webhook payload and stores messages in the database.
    Sends auto-replies based on business hours and configuration.
    """
    body = await request.json()
    logger.info(f"Webhook recebido: {body}")

    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                # Process incoming messages
                if "messages" in value:
                    for msg in value["messages"]:
                        await _process_incoming_message(msg, value, db)

                # Process status updates (delivered, read)
                if "statuses" in value:
                    for status_update in value["statuses"]:
                        await _process_status_update(status_update, db)

    except Exception as e:
        logger.error(f"Erro ao processar webhook: {e}", exc_info=True)

    # Always return 200 to Meta (otherwise they retry)
    return {"status": "ok"}


def _is_within_business_hours(db: Session) -> bool:
    """
    Check if current time falls within configured business hours.
    Uses UTC-3 (Brasilia timezone) for checking.
    """
    from datetime import timedelta

    now_utc = datetime.now(tz.utc)
    # Convert to BRT (UTC-3)
    brt_offset = timedelta(hours=-3)
    now_brt = now_utc + brt_offset

    weekday = now_brt.weekday()  # 0=Monday, 6=Sunday
    current_time = now_brt.strftime("%H:%M")

    hours = db.query(BusinessHours).filter(BusinessHours.weekday == weekday).first()
    if not hours:
        return True  # If no config, assume open

    if not hours.is_open:
        return False

    if hours.open_time and hours.close_time:
        return hours.open_time <= current_time <= hours.close_time

    return True


def _get_auto_reply(trigger: str, db: Session) -> str | None:
    """Get an active auto-reply message by trigger."""
    reply = db.query(AutoReply).filter(
        AutoReply.trigger == trigger,
        AutoReply.is_active == True,
    ).first()

    if reply and reply.message and reply.message.strip():
        return reply.message
    return None


async def _send_auto_reply_if_needed(
    conversation: Conversation,
    is_new_conversation: bool,
    db: Session,
):
    """
    Determine and send the appropriate auto-reply based on:
    1. Business hours (out_of_hours if outside)
    2. New conversation greeting
    3. Waiting for attendant
    """
    phone = conversation.whatsapp

    # Check business hours first
    if not _is_within_business_hours(db):
        message = _get_auto_reply("out_of_hours", db)
        if message:
            await whatsapp.send_text_message(phone, message, db)
            _save_outbound_message(conversation, message, db)
            logger.info(f"Auto-reply (fora do expediente) enviado para {phone}")
            return

    # New conversation — send greeting
    if is_new_conversation:
        message = _get_auto_reply("greeting", db)
        if message:
            await whatsapp.send_text_message(phone, message, db)
            _save_outbound_message(conversation, message, db)
            logger.info(f"Auto-reply (saudação) enviado para {phone}")
            return

    # Existing conversation without attendant — send waiting message
    if not conversation.atendente_id:
        message = _get_auto_reply("waiting", db)
        if message:
            # Only send if we haven't sent a waiting message recently (avoid spam)
            recent_outbound = db.query(Message).filter(
                Message.conversation_id == conversation.id,
                Message.direction == "outbound",
                Message.content == message,
            ).order_by(Message.created_at.desc()).first()

            if recent_outbound:
                from datetime import timedelta
                time_since = datetime.now(tz.utc) - (recent_outbound.created_at.replace(tzinfo=tz.utc) if recent_outbound.created_at.tzinfo is None else recent_outbound.created_at)
                if time_since < timedelta(hours=1):
                    return  # Don't spam the same waiting message

            await whatsapp.send_text_message(phone, message, db)
            _save_outbound_message(conversation, message, db)
            logger.info(f"Auto-reply (aguardando) enviado para {phone}")


def _save_outbound_message(conversation: Conversation, content: str, db: Session):
    """Save an auto-reply message to the database."""
    message = Message(
        conversation_id=conversation.id,
        direction="outbound",
        content=content,
        msg_type="text",
        status="sent",
    )
    db.add(message)
    db.commit()


async def _process_incoming_message(msg: dict, value: dict, db: Session):
    """Process a single incoming WhatsApp message."""
    whatsapp_number = msg.get("from", "")
    msg_id = msg.get("id", "")
    msg_type = msg.get("type", "text")
    timestamp = msg.get("timestamp", "")

    # Extract content based on message type
    content = ""
    media_url = None

    if msg_type == "text":
        content = msg.get("text", {}).get("body", "")
    elif msg_type in ("image", "video", "audio", "document"):
        media_data = msg.get(msg_type, {})
        content = media_data.get("caption", f"[{msg_type.upper()}]")
        media_url = media_data.get("id")  # Media ID (needs download via Graph API)
    elif msg_type == "location":
        loc = msg.get("location", {})
        content = f"Localização: {loc.get('latitude', '')}, {loc.get('longitude', '')}"
    elif msg_type == "contacts":
        content = "Contato compartilhado"
    elif msg_type == "sticker":
        content = "Sticker"
    elif msg_type == "reaction":
        reaction = msg.get("reaction", {})
        content = f"Reação: {reaction.get('emoji', '')}"
    elif msg_type == "interactive":
        interactive = msg.get("interactive", {})
        int_type = interactive.get("type", "")
        if int_type == "button_reply":
            content = interactive.get("button_reply", {}).get("title", "[Botão]")
        elif int_type == "list_reply":
            content = interactive.get("list_reply", {}).get("title", "[Lista]")
        else:
            content = f"[INTERACTIVE: {int_type}]"
    else:
        content = f"[{msg_type.upper()}]"

    # Extract sender name from contacts
    sender_name = ""
    contacts = value.get("contacts", [])
    if contacts:
        profile = contacts[0].get("profile", {})
        sender_name = profile.get("name", "")

    # Find or create conversation
    is_new_conversation = False
    conversation = db.query(Conversation).filter(
        Conversation.whatsapp == whatsapp_number
    ).first()

    if not conversation:
        is_new_conversation = True
        conversation = Conversation(
            lead_id=0,  # Will be linked later via CRM
            whatsapp=whatsapp_number,
            nome=sender_name or whatsapp_number,
            status="aberta",
            ultimo_msg=content[:200] if content else "",
            unread_count=1,
            last_customer_msg_at=datetime.now(tz.utc),
        )
        db.add(conversation)
        db.flush()
        logger.info(f"Nova conversa criada: {sender_name} ({whatsapp_number})")
    else:
        # Update existing conversation
        conversation.ultimo_msg = content[:200] if content else conversation.ultimo_msg
        conversation.unread_count = (conversation.unread_count or 0) + 1
        conversation.status = "aberta"
        conversation.last_customer_msg_at = datetime.now(tz.utc)
        if sender_name and not conversation.nome:
            conversation.nome = sender_name

    # Check if message already processed (idempotency)
    existing = db.query(Message).filter(Message.whatsapp_msg_id == msg_id).first()
    if existing:
        logger.info(f"Mensagem duplicada ignorada: {msg_id}")
        return

    # Save message
    message = Message(
        conversation_id=conversation.id,
        direction="inbound",
        content=content,
        msg_type=msg_type,
        media_url=media_url,
        whatsapp_msg_id=msg_id,
        status="received",
    )
    db.add(message)
    db.commit()

    logger.info(f"Mensagem recebida de {sender_name} ({whatsapp_number}): {content[:50]}")

    # Mark as read on WhatsApp
    await whatsapp.mark_as_read(msg_id, db)

    # ─── CRM Auto-Link: vincular conversa ao lead do CRM ───
    if is_new_conversation or (conversation.lead_id is None or conversation.lead_id <= 0):
        linked = await crm_service.auto_link_conversation(conversation, db)
        if linked:
            logger.info(f"Conversa auto-vinculada ao lead CRM #{conversation.lead_id}")

    # Send auto-reply if needed
    await _send_auto_reply_if_needed(conversation, is_new_conversation, db)

    # ─── N8N Agent: encaminhar para IA se bot ativo ───
    if N8N_AGENT_ENABLED and conversation.is_bot_active:
        await _forward_to_agent(conversation, content, db)


async def _process_status_update(status_update: dict, db: Session):
    """Process a message status update (sent, delivered, read, failed)."""
    msg_id = status_update.get("id", "")
    new_status = status_update.get("status", "")

    if not msg_id or not new_status:
        return

    message = db.query(Message).filter(Message.whatsapp_msg_id == msg_id).first()
    if message:
        message.status = new_status
        db.commit()
        logger.info(f"Status atualizado: {msg_id} -> {new_status}")

        # If message failed, log the error details
        if new_status == "failed":
            errors = status_update.get("errors", [])
            if errors:
                error_detail = errors[0]
                logger.error(
                    f"Mensagem falhou: {msg_id} - "
                    f"code={error_detail.get('code')}, "
                    f"title={error_detail.get('title')}"
                )


async def _forward_to_agent(conversation: Conversation, message_text: str, db: Session):
    """
    Forward the incoming message to the N8N AI Agent (WF-10 Bia).
    The agent processes the message and returns a response that gets sent
    back to the customer via WhatsApp.
    """
    import httpx

    # Build ALL messages as context for the agent (full conversation history)
    recent_msgs = db.query(Message).filter(
        Message.conversation_id == conversation.id,
    ).order_by(Message.created_at.asc()).all()

    historico = [
        {"direction": m.direction, "content": m.content, "type": m.msg_type}
        for m in recent_msgs
    ]

    payload = {
        "conversation_id": conversation.id,
        "lead_id": conversation.lead_id,
        "whatsapp": conversation.whatsapp,
        "nome": conversation.nome or conversation.whatsapp,
        "mensagem": message_text,
        "historico": historico,
    }

    agent_url = f"{N8N_BASE_URL}/webhook/agent-bia"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(agent_url, json=payload)

            if resp.status_code == 200:
                data = resp.json()
                resposta = data.get("resposta", "")

                if resposta:
                    # Split response by ||| for multiple messages (natural WhatsApp feel)
                    partes = [p.strip() for p in resposta.split("|||") if p.strip()]

                    for i, parte in enumerate(partes):
                        # Send each part as a separate WhatsApp message
                        await whatsapp.send_text_message(conversation.whatsapp, parte, db)

                        # Small delay between messages for natural feel
                        if i < len(partes) - 1:
                            import asyncio
                            await asyncio.sleep(1.2)

                        # Save each part as outbound message
                        agent_msg = Message(
                            conversation_id=conversation.id,
                            direction="outbound",
                            content=parte,
                            msg_type="text",
                            status="sent",
                        )
                        db.add(agent_msg)

                    # Update conversation preview with last part
                    conversation.ultimo_msg = partes[-1][:200]
                    conversation.unread_count = 0
                    db.commit()

                    logger.info(f"Resposta da Bia ({len(partes)} msgs) para {conversation.whatsapp}")
            else:
                logger.warning(f"Agente IA retornou status {resp.status_code}: {resp.text[:200]}")
    except httpx.TimeoutException:
        logger.warning(f"Timeout ao chamar agente IA para conversa {conversation.id}")
    except Exception as e:
        logger.error(f"Erro ao encaminhar para agente IA: {e}")
