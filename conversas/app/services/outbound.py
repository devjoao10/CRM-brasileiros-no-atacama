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
from app.models.media_asset import MediaAsset

logger = logging.getLogger(__name__)


def _sanitize_filename(filename: Optional[str]) -> Optional[str]:
    """Filename e METADADO (nunca vira path) — mas sanitizamos mesmo assim."""
    if not filename:
        return None
    base = filename.replace("\\", "/").split("/")[-1].strip()
    return base[:255] or None


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


class MediaRejection(Exception):
    """CONV-03: upload rejeitado pela politica ANTES de qualquer persistencia."""

    def __init__(self, status_code: int, reason: str):
        self.status_code = status_code  # 415 (tipo) ou 413 (tamanho)
        self.reason = reason
        super().__init__(reason)


async def send_media_upload(
    db: Session,
    conversation: Conversation,
    *,
    content: bytes,
    mime_type: str,
    caption: str = "",
    filename: Optional[str] = None,
):
    """
    CONV-03 — envio outbound de midia por upload (generico: audio/imagem/video/
    documento; a UI decide o que aceitar por pacote).

    Fluxo: politica -> upload a Meta -> send por media_id -> record_outbound_message
    (integridade CONV-08b: nunca 'sent' sem aceite) -> MediaAsset com espelho
    local do arquivo do operador (permite retry e preview imediato).

    Politica REJEITA antes de persistir qualquer coisa (MediaRejection ->
    415/413); falha de provider PERSISTE Message 'failed' + asset com espelho
    local (retry possivel).
    Retorna (message, asset).
    """
    from app.services import whatsapp, media_policy, media_storage

    kind = media_policy.classify_mime(mime_type)
    if kind is None or kind == "sticker":
        raise MediaRejection(415, f"tipo de midia nao suportado: {mime_type or '(vazio)'}")
    ok, reason = media_policy.validate(kind, mime_type, len(content))
    if not ok:
        status = 413 if "limite" in (reason or "") or "tamanho" in (reason or "") else 415
        raise MediaRejection(status, reason or "midia rejeitada pela politica")

    # 1) upload a Meta
    up = await whatsapp.upload_media(content, mime_type, db)
    media_id = up.get("id") if isinstance(up, dict) else None

    # 2) send por media_id (upload falho/simulado propaga como resposta do send)
    if not isinstance(up, dict) or up.get("error"):
        wa_response = {
            "error": True,
            "summary": (up.get("summary") if isinstance(up, dict) else None)
            or "falha no upload da midia a Meta",
        }
    elif up.get("simulated"):
        wa_response = {"simulated": True}
    else:
        wa_response = await whatsapp.send_media_message(
            conversation.whatsapp, kind, caption=caption or "", db=db, media_id=media_id
        )

    message = record_outbound_message(
        db, conversation, caption or f"[{kind.upper()}]", kind, wa_response,
        media_url=None, update_preview=True, reset_unread=True,
    )

    # 3) asset com espelho LOCAL do arquivo do operador (preview + retry)
    asset = MediaAsset(
        message_id=message.id,
        meta_media_id=media_id,
        meta_mime_type=mime_type,
        filename=_sanitize_filename(filename),
        status="referenced",
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    media_storage.store_bytes(asset, content, mime_type, db)  # -> 'downloaded'

    return message, asset
