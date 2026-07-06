"""
CONV-08b — Finalizacao centralizada de envio outbound.

Todo caminho que envia mensagem via Meta e persiste um `Message` outbound DEVE
passar por `record_outbound_message()`. Isso elimina a classe de bug do
falso-'sent' (persistir 'sent' sem a Meta ter aceitado o envio).

Contrato de resposta das funcoes `whatsapp.send_*`:
  - dict com "messages"            -> aceito pela Meta (sucesso real)
  - dict {"simulated": True, ...}  -> sem credenciais (dev; NAO houve envio real)
  - dict {"error": True, "summary": <seguro>, "status_code": ...} -> falha real
  - None                           -> tratado defensivamente como falha
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.conversation import Conversation, Message

logger = logging.getLogger(__name__)


def classify_wa_response(wa_response) -> dict:
    """
    Classifica a resposta do cliente WhatsApp.
    Retorna: {"ok": bool, "simulated": bool, "wamid": str|None, "error_summary": str|None}
    """
    if isinstance(wa_response, dict):
        if wa_response.get("error"):
            return {
                "ok": False,
                "simulated": False,
                "wamid": None,
                "error_summary": wa_response.get("summary") or "Erro na API do WhatsApp",
            }
        if wa_response.get("simulated"):
            # Modo dev sem credenciais: NAO e um envio real, mas tambem nao e falha.
            return {"ok": True, "simulated": True, "wamid": None, "error_summary": None}
        if "messages" in wa_response:
            msgs = wa_response.get("messages") or []
            wamid = msgs[0].get("id") if msgs else None
            return {"ok": True, "simulated": False, "wamid": wamid, "error_summary": None}
    if wa_response is None:
        return {
            "ok": False,
            "simulated": False,
            "wamid": None,
            "error_summary": "Falha no envio: sem resposta do cliente WhatsApp",
        }
    return {
        "ok": False,
        "simulated": False,
        "wamid": None,
        "error_summary": "Resposta inesperada da API do WhatsApp",
    }


def record_outbound_message(
    db: Session,
    conversation: Conversation,
    content: str,
    msg_type: str,
    wa_response,
    *,
    media_url: Optional[str] = None,
    update_preview: bool = True,
    reset_unread: bool = False,
    commit: bool = True,
) -> Message:
    """
    Persiste um Message outbound com status fiel ao resultado do envio.

    - Sucesso real: status='sent' + whatsapp_msg_id; atualiza preview/unread
      conforme flags.
    - Simulado (dev): status='sent' EXPLICITAMENTE logado como simulado
      (sem wamid) — nunca confundir com sucesso de producao.
    - Falha: status='failed' + last_error seguro; preview/unread NAO sao tocados.

    Sempre grava send_attempts=1 e last_attempt_at (base para retry).
    """
    r = classify_wa_response(wa_response)
    now = datetime.now(timezone.utc)

    message = Message(
        conversation_id=conversation.id,
        direction="outbound",
        content=content,
        msg_type=msg_type,
        media_url=media_url,
        whatsapp_msg_id=r["wamid"],
        status="sent" if r["ok"] else "failed",
        last_error=r["error_summary"],
        send_attempts=1,
        last_attempt_at=now,
    )
    db.add(message)

    if r["ok"]:
        if update_preview:
            conversation.ultimo_msg = (content or "")[:200]
        if reset_unread:
            conversation.unread_count = 0
        if r["simulated"]:
            logger.info(
                f"Envio SIMULADO (Meta nao configurada) na conversa {conversation.id} "
                f"(msg_type={msg_type}); mensagem persistida sem wamid."
            )
    else:
        # Log seguro: last_error ja e um resumo sem token/payload sensivel.
        logger.warning(
            f"Envio outbound FALHOU na conversa {conversation.id} "
            f"(msg_type={msg_type}): {r['error_summary']}; persistida como 'failed'."
        )

    if commit:
        db.commit()
        db.refresh(message)
    return message
