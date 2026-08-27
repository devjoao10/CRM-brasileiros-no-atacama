"""
CONV-08b — Finalizacao centralizada de envio outbound.

Todo caminho que envia mensagem via Meta e persiste um `Message` outbound DEVE
passar por `record_outbound_message()`. Isso elimina a classe de bug do
falso-'sent' (persistir 'sent' sem a Meta ter aceitado o envio).

Contrato de resposta das funcoes `whatsapp.send_*`:
  - dict com "messages"            -> aceito pela Meta (sucesso real)
  - dict {"simulated": True, ...}  -> sem credenciais, SO em development
                                      (NAO houve envio real -> status 'simulated')
  - dict {"error": True, "summary": <seguro>, "status_code": ...} -> falha real
  - None                           -> tratado defensivamente como falha
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models.conversation import Conversation, Message
from app.models.media_asset import MediaAsset

logger = logging.getLogger(__name__)

# AUDIT-2026-08-W1D (F3): status de outbound que NAO sao falha. 'simulated' so
# ocorre em development. Existe como constante para que quem precisa perguntar
# "esta parte saiu?" nao reescreva a lista de strings e esqueca uma delas.
NOT_FAILED_STATUSES = ("sent", "simulated")

# AUDIT-2026-08-WD (D2): precedencia de status de entrega da Meta — MOVIDA
# para ca (antes vivia so em routers/webhook.py) porque agora DUAS partes do
# sistema precisam da MESMA regra: o webhook, quando o Message ja existe, e
# este modulo, ao reconciliar um status que chegou ORFAO (callback antes do
# commit do envio, ver `remember_pending_status`/`consume_pending_status`
# abaixo). "Regra que precisa valer no envio E no recebimento mora aqui,
# nunca duplicada na rota" (services/CLAUDE.md). A Meta NAO garante ordem de
# callback e reentrega os antigos: um 'delivered' atrasado nao pode regredir
# um 'read', e 'failed' e TERMINAL (nunca apagado por um status posterior).
_STATUS_RANK = {"sent": 1, "delivered": 2, "read": 3}


def apply_status_rank(current: str, new_status: str) -> Optional[str]:
    """
    Decide se `new_status` deve substituir `current`, pela MESMA regra de
    precedencia em toda parte do sistema que recebe status da Meta.

    Retorna o status resultante, ou None se `new_status` deve ser IGNORADO
    (nao avanca sobre o atual, ou tenta apagar um 'failed' terminal).
    """
    current = current or ""
    if current == "failed" and new_status != "failed":
        return None
    if new_status != "failed" and _STATUS_RANK.get(new_status, 0) <= _STATUS_RANK.get(current, 0):
        return None
    return new_status


# AUDIT-2026-08-WD (D2): status ORFAO — callback de status da Meta para um
# wamid cujo Message AINDA nao foi commitado. O loop de resposta da Bia
# (webhook.py:_forward_to_agent) persiste cada parte com commit=False e um
# sleep de 1.2s entre elas; nesse intervalo a Meta ja pode estar entregando
# a mensagem e mandando o callback de status, que chegava para um wamid
# "desconhecido no banco" e era so logado e descartado — delivered/read
# nunca aparecia, e uma falha real ficava invisivel.
#
# Em memoria, DE PROPOSITO: a corrida e de SEGUNDOS (o commit que falta e o
# da MESMA resposta que acabou de gerar o wamid), nao minutos. Um reinicio de
# processo bem no meio dessa janela perde o status pendente — volta ao
# comportamento antigo (log e descarte), nunca pior que hoje. Mesma familia
# de solucao ja usada neste mesmo pacote de servicos/routers: `_debounce_cutoffs`
# (routers/webhook.py) e o cache de catalogo (services/meta_templates.py) —
# estado curto, nunca fonte de verdade.
# ponytail: dict em memoria de UM processo; se o Conversas passar a rodar com
# mais de um worker, isto precisa virar tabela (a corrida so fecha hoje
# porque INSERT e callback caem no mesmo worker).
_PENDING_STATUS_TTL = timedelta(minutes=10)
_PENDING_STATUS_MAX = 500
_pending_statuses: dict[str, dict] = {}  # wamid -> {"status": str, "at": datetime}


def _prune_pending_statuses() -> None:
    """Remove entradas mais velhas que `_PENDING_STATUS_TTL` — nunca cresce sem limite."""
    cutoff = datetime.now(timezone.utc) - _PENDING_STATUS_TTL
    stale = [k for k, v in _pending_statuses.items() if v["at"] < cutoff]
    for k in stale:
        _pending_statuses.pop(k, None)


def remember_pending_status(wamid: str, status: str) -> None:
    """
    Guarda um status da Meta para um wamid cujo Message ainda nao existe no
    banco. Reconciliado pela MESMA `apply_status_rank` contra qualquer status
    ja pendente do MESMO wamid — dois callbacks orfaos fora de ordem (ex.:
    'delivered' e depois 'read', ambos antes do INSERT) nao podem regredir um
    para o outro.
    """
    if not wamid:
        return
    _prune_pending_statuses()
    existing = _pending_statuses.get(wamid)
    current = existing["status"] if existing else ""
    resolved = apply_status_rank(current, status)
    if resolved is None:
        return
    if wamid not in _pending_statuses and len(_pending_statuses) >= _PENDING_STATUS_MAX:
        # Teto de seguranca: descarta o pendente mais velho em vez de crescer
        # sem limite (wamid que nunca vira Message — ex.: numero errado no
        # payload de status, ou reentrega de um evento muito antigo).
        oldest = min(_pending_statuses, key=lambda k: _pending_statuses[k]["at"])
        _pending_statuses.pop(oldest, None)
    _pending_statuses[wamid] = {"status": resolved, "at": datetime.now(timezone.utc)}


def consume_pending_status(wamid: str) -> Optional[dict]:
    """Remove e devolve o status pendente de um wamid (chamado ao inserir o Message correspondente)."""
    if not wamid:
        return None
    return _pending_statuses.pop(wamid, None)


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
    autor_user_id: Optional[int] = None,
) -> Message:
    """
    Persiste um Message outbound com status fiel ao resultado do envio.

    - Sucesso real: status='sent' + whatsapp_msg_id; atualiza preview/unread
      conforme flags.
    - Simulado (dev): status='simulated' (sem wamid). AUDIT-2026-08-W1D (F3):
      antes era 'sent', indistinguivel de um envio real no banco, no inbox e em
      qualquer relatorio. Um status PROPRIO e o unico jeito de a linha nunca
      poder ser lida como entregue. Fora de development o caminho simulado nem
      existe mais (whatsapp._unconfigured_result devolve erro).
    - Falha: status='failed' + last_error seguro; preview/unread NAO sao tocados.

    Sempre grava send_attempts=1 e last_attempt_at (base para retry).

    AUDIT-2026-08-WA — `autor_user_id` distingue QUEM enviou.

    `Message` nao tem coluna de autoria: Bia, auto-resposta e humano passam
    todos por aqui com `direction='outbound'`. Sem um discriminador, "primeira
    resposta humana" — o evento que tira a conversa da FILA DE ESPERA — e
    indecidivel.

    As rotas autenticadas por uma pessoa passam `current_user.id`; a Bia
    (`webhook._forward_to_agent`) e as auto-respostas passam `None` e nunca
    encerram a espera do cliente por um humano. O envio precisa ter dado certo:
    uma tentativa que falhou na Meta nao atendeu ninguem.
    """
    r = classify_wa_response(wa_response)
    now = datetime.now(timezone.utc)

    status = ("simulated" if r["simulated"] else "sent") if r["ok"] else "failed"
    # AUDIT-2026-08-WD (D2): o wamid que acabamos de receber pode JA ter um
    # status pendente (ver `remember_pending_status`) — o callback da Meta
    # chegou antes deste INSERT. Este e o UNICO ponto em que o wamid passa a
    # existir como linha, entao e aqui que o status guardado e consumido e
    # reconciliado, pela MESMA regra de precedencia de sempre.
    if r["wamid"]:
        pending = consume_pending_status(r["wamid"])
        if pending:
            resolved = apply_status_rank(status, pending["status"])
            if resolved:
                logger.info(
                    f"Status pendente '{pending['status']}' (wamid={r['wamid']}) "
                    f"aplicado ao registrar a mensagem: '{status}' -> '{resolved}'"
                )
                status = resolved

    message = Message(
        conversation_id=conversation.id,
        direction="outbound",
        content=content,
        msg_type=msg_type,
        media_url=media_url,
        whatsapp_msg_id=r["wamid"],
        status=status,
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
                f"Envio SIMULADO (development, Meta nao configurada) na conversa "
                f"{conversation.id} (msg_type={msg_type}); persistida como "
                f"status='simulated', sem wamid."
            )
        # AUDIT-2026-08-WA — a transicao FILA -> EM ATENDIMENTO acontece aqui,
        # e so aqui, porque este e o unico ponto por onde passa todo envio.
        if autor_user_id is not None:
            from app.services.atendimento import marcar_atendimento_humano

            if marcar_atendimento_humano(conversation, autor_user_id):
                logger.info(
                    f"Primeira resposta humana na conversa {conversation.id} "
                    f"(atendente={conversation.atendente_id}); saiu da fila."
                )
    else:
        # Log seguro: last_error ja e um resumo sem token/payload sensivel.
        logger.warning(
            f"Envio outbound FALHOU na conversa {conversation.id} "
            f"(msg_type={msg_type}): {r['error_summary']}; persistida como 'failed'."
        )

    if commit:
        try:
            db.commit()
            db.refresh(message)
        except Exception:
            # AUDIT-2026-08-WD (D4, "related and in scope") — a Meta pode JA
            # ter aceitado o envio (r["ok"]) quando o COMMIT falha aqui: o
            # cliente recebeu a mensagem e o banco nao vai ter a linha (ou vai
            # ficar com estado inconsistente). Nao construimos idempotency
            # key nem retry de commit — so garantimos que isto NUNCA seja um
            # 500 mudo: o wamid (unica pista para reconciliar manualmente) e
            # logado em nivel ERROR antes de propagar.
            db.rollback()
            if r["ok"] and r["wamid"]:
                logger.error(
                    f"FALHA AO PERSISTIR mensagem outbound JA ACEITA pela Meta "
                    f"(conversa {conversation.id}, msg_type={msg_type}, "
                    f"wamid={r['wamid']}): o cliente recebeu a mensagem mas o "
                    f"banco nao tem o registro — reconciliar manualmente pelo wamid."
                )
            raise
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
    autor_user_id: Optional[int] = None,
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
        autor_user_id=autor_user_id,
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
