"""
Meta Cloud API Webhook receiver.
Handles verification (GET) and incoming messages (POST).
Includes automatic replies based on business hours and auto-reply settings.
Includes debounce mechanism for batching rapid messages before AI processing.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone as tz

import httpx
from fastapi import APIRouter, Request, HTTPException, Query, Depends
from sqlalchemy import exc as sa_exc, or_
from sqlalchemy.orm import Session

from app.config import META_VERIFY_TOKEN, N8N_BASE_URL, N8N_AGENT_ENABLED, META_APP_SECRET, ENVIRONMENT
from app.database import get_db, SessionLocal
from app.models.conversation import Conversation, Message, service_window_open
from app.models.auto_reply import AutoReply, BusinessHours
from app.models.api_config import ApiConfig
from app.services import whatsapp
from app.services import crm as crm_service
from app.services import variables as variables_service
from app.services.outbound import record_outbound_message, NOT_FAILED_STATUSES
from app.models.media_asset import MediaAsset

logger = logging.getLogger(__name__)

# CONV-AGENT-01 — a Bia (WF-10) e um agente LLM com tools: 1m27s e 2m36s sao
# execucoes NORMAIS, nao travamentos. Com o teto anterior de 60s o Conversas
# desistia da conexao, o n8n concluia com sucesso depois e o cliente ficava
# sem resposta nenhuma.
#
# Timeout SEPARADO por fase (httpx 0.28.1): o primeiro argumento posicional e o
# default aplicado a read/write/pool; `connect` sobrescreve so a conexao.
#   connect=10s -> n8n fora do ar falha rapido, sem segurar o webhook
#   read=240s   -> o agente tem folga para raciocinar e chamar suas tools
AGENT_TIMEOUT = httpx.Timeout(240.0, connect=10.0)

# Resposta unica de degradacao. NAO e uma resposta da Bia: e o que o cliente
# recebe quando nao foi possivel obter resposta alguma do agente. Texto
# generico de proposito — erro tecnico nunca chega ao cliente.
AGENT_FALLBACK_REPLY = (
    "Tive uma instabilidade para processar sua mensagem agora. "
    "Pode me enviar novamente em alguns instantes? 🙂"
)


# AUDIT-2026-08-W1D (F1) — quais erros significam "Meta, tente de novo".
#
# Conservador DE PROPOSITO. Retry da Meta so ajuda quando a falha e do nosso
# lado E transitoria: banco fora do ar, conexao derrubada, pool esgotado, disco.
# Uma mensagem malformada vai falhar identicamente em toda reentrega — pedir
# retry dela so gera tempestade de reentrega e, no limite, a Meta desabilita a
# subscription do webhook. Por isso: erro de infra -> 503; qualquer outro -> 200.
#
# IntegrityError NAO entra aqui de proposito: e subclasse de DBAPIError mas
# significa "estes dados violam uma constraint", nao "o banco caiu".
#
# AUDIT-2026-08-F2 — ProgrammingError e DataError entraram, e a razao importa:
# esta lista era DIALETO-DEPENDENTE, e o dialeto de producao e o que nao estava
# coberto.
#
#   coluna/tabela/funcao inexistente  SQLite -> OperationalError  (na lista)
#                                     Postgres -> ProgrammingError (FORA)
#   valor fora do enum                SQLite -> OperationalError  (na lista)
#                                     Postgres -> DataError       (FORA)
#
# Consequencia em producao, e so em producao: qualquer drift de schema fazia a
# rota devolver 200 a Meta — "entreguei" — e a Meta NUNCA reenvia. Mensagem de
# cliente perdida em definitivo. A suite SQLite demonstrava o comportamento
# oposto (503 + reentrega) e por isso nada nunca acusou.
#
# IntegrityError continua FORA de proposito: e "estes dados violam uma
# constraint", nao "o banco caiu" — pedir reentrega de um dado invalido so gera
# tempestade. A corrida de primeiro contato, que passa a levantar IntegrityError
# quando o indice unico de `conversations.whatsapp` existir, e tratada no ponto
# de criacao da conversa, nao aqui.
_INFRA_ERRORS = (
    sa_exc.OperationalError,
    sa_exc.InterfaceError,
    sa_exc.InternalError,
    sa_exc.DisconnectionError,
    sa_exc.TimeoutError,     # pool exausto
    sa_exc.ResourceClosedError,
    sa_exc.ProgrammingError,  # PostgreSQL: 42703/42P01/42883 (schema drift)
    sa_exc.DataError,         # PostgreSQL: 22P02 (valor fora do enum/tipo)
    OSError,                  # cobre ConnectionError, socket, disco
)

# AUDIT-2026-08-W1D (F5) — precedencia de status de entrega da Meta.
# A Meta NAO garante ordem de callback e reentrega os antigos, entao um
# 'delivered' atrasado chegava DEPOIS do 'read' e regredia a mensagem.
# 'failed' e TERMINAL: nunca pode ser apagado por um 'sent' velho, senao a
# nao-entrega some da tela do operador.
_STATUS_RANK = {"sent": 1, "delivered": 2, "read": 3}


def _is_signature_required() -> bool:
    return ENVIRONMENT != "development" or bool(META_APP_SECRET)


def _verify_meta_signature(raw_body: bytes, signature_header: str) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        META_APP_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature_header, expected)


# ─── Debounce system for batching rapid messages ─────────────
AGENT_DEBOUNCE_SECONDS = 15  # Wait 15s after last message before sending to agent
# AUDIT-2026-08-W1D (F8): teto de mensagens enviadas como historico ao n8n.
# ponytail: corte fixo simples; se a Bia precisar de janela por tempo ou por
# tokens, trocar por um sumarizador — mas nao antes de haver evidencia disso.
AGENT_HISTORY_LIMIT = 30
_debounce_tasks: dict[int, asyncio.Task] = {}  # conversation_id -> scheduled task
# AUDIT-2026-08-W1D (F2): conversation_id -> inicio do lote pendente (ultimo
# outbound ANTES da resposta automatica). Escrito por _remember_agent_cutoff,
# lido por _schedule_agent_debounce, removido quando o lote e consumido.
_debounce_cutoffs: dict = {}
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
    raw_body = await request.body()
    sig = request.headers.get("X-Hub-Signature-256", "")

    if META_APP_SECRET:
        if not _verify_meta_signature(raw_body, sig):
            logger.warning("Assinatura HMAC inválida ou ausente no webhook Meta")
            raise HTTPException(status_code=403, detail="Invalid signature")
    elif _is_signature_required():
        logger.error("META_APP_SECRET não configurado; webhook Meta rejeitado em ambiente não-development")
        raise HTTPException(status_code=500, detail="Webhook signature verification not configured")
    else:
        logger.warning("META_APP_SECRET não configurado; validação HMAC desativada apenas em development")

    try:
        body = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    logger.info("Webhook Meta recebido e validado")

    # AUDIT-2026-08-W1D (F1) — ISOLAMENTO POR ITEM.
    #
    # Antes UM unico try/except envolvia os tres lacos e caia em `return 200`.
    # Tres consequencias, todas confirmadas:
    #   (a) uma mensagem venenosa abortava TODAS as irmas do mesmo lote — a Meta
    #       agrupa varias mensagens por POST, entao perdia-se o lote inteiro;
    #   (b) a Meta recebia 200 ("entreguei") e NUNCA reenviava — perda definitiva;
    #   (c) banco fora do ar era indistinguivel de sucesso.
    #
    # Agora cada mensagem e cada status tem seu proprio try/except, com rollback
    # da sessao (uma transacao suja envenenaria a proxima mensagem do lote) e o
    # laco continua. So erro de INFRA marca o POST inteiro para reentrega.
    infra_failure = False
    processed = 0

    try:
        for entry in body.get("entry", []) or []:
            for change in entry.get("changes", []) or []:
                value = change.get("value", {}) or {}

                for msg in value.get("messages", []) or []:
                    try:
                        await _process_incoming_message(msg, value, db)
                        processed += 1
                    except Exception as e:
                        db.rollback()
                        infra = isinstance(e, _INFRA_ERRORS)
                        infra_failure = infra_failure or infra
                        logger.error(
                            f"Falha ao processar mensagem inbound "
                            f"{msg.get('id') or '(sem id)'} "
                            f"({'INFRA -> pedir reentrega' if infra else 'dados -> descartada'}): "
                            f"{type(e).__name__}",
                            exc_info=True,
                        )

                for status_update in value.get("statuses", []) or []:
                    try:
                        await _process_status_update(status_update, db)
                        processed += 1
                    except Exception as e:
                        db.rollback()
                        infra = isinstance(e, _INFRA_ERRORS)
                        infra_failure = infra_failure or infra
                        logger.error(
                            f"Falha ao processar status "
                            f"{status_update.get('id') or '(sem id)'} "
                            f"({'INFRA -> pedir reentrega' if infra else 'dados -> descartado'}): "
                            f"{type(e).__name__}",
                            exc_info=True,
                        )
    except Exception as e:
        # Envelope malformado (entry/changes que nao sao o que dizem ser).
        # Reentregar nao muda nada: o payload seria igualmente invalido. 200.
        logger.error(
            f"Envelope do webhook Meta invalido, lote descartado: {type(e).__name__}",
            exc_info=True,
        )
        return {"status": "ok"}

    if infra_failure:
        # 503 e o unico caso em que QUEREMOS o retry da Meta. O que ja foi
        # consumido antes da falha e idempotente na reentrega (dedupe por
        # whatsapp_msg_id), entao pedir reentrega nao duplica mensagem.
        logger.error(
            f"Webhook Meta com falha de infraestrutura ({processed} item(ns) consumido(s) "
            f"antes): devolvendo 503 para forcar reentrega."
        )
        raise HTTPException(status_code=503, detail="Temporary processing failure, retry")

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


def _resolve_auto_reply(
    trigger: str, db: Session, conversation: Conversation | None = None
) -> tuple[bool, str | None]:
    """
    Resolve a resposta automatica de um trigger.

    Retorna `(configurada, texto)`:
      - (False, None) -> NAO existe resposta ativa para este trigger;
      - (True,  texto) -> existe e resolveu;
      - (True,  None)  -> existe mas NAO resolveu (variavel sem valor,
                          desconhecida, inativa...) e deve ser PULADA.

    A distincao entre "nao configurada" e "configurada mas pulada" e o que
    impede que pular uma resposta promova OUTRA no lugar dela: se o
    `out_of_hours` do cliente falha na resolucao, ele nao pode receber a
    saudacao de boas-vindas as 3h da manha.

    CONV-VAR-01-HARD-01: resolucao TUDO OU NADA — nunca token literal, texto
    mutilado ou string vazia; nunca levanta excecao.
    """
    reply = db.query(AutoReply).filter(
        AutoReply.trigger == trigger,
        AutoReply.is_active == True,
    ).first()

    if not (reply and reply.message and reply.message.strip()):
        return False, None

    return True, variables_service.render_auto_reply(
        db,
        reply.message,
        variables_service.VariableContext(db, conversation=conversation, user=None),
        trigger=trigger,
    )


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

    # CONV-VAR-01-HARD-01: o `return` acontece quando o trigger ESTA
    # CONFIGURADO, mesmo que a resposta tenha sido pulada por variavel nao
    # resolvida. Pular uma resposta nunca pode promover a resposta seguinte.

    # Check business hours first
    if not _is_within_business_hours(db):
        configured, message = _resolve_auto_reply("out_of_hours", db, conversation)
        if configured:
            if message:
                wa_response = await whatsapp.send_text_message(phone, message, db)
                _save_outbound_message(conversation, message, db, wa_response)
                logger.info(f"Auto-reply (fora do expediente) processado para {phone}")
            return

    # New conversation — send greeting
    if is_new_conversation:
        configured, message = _resolve_auto_reply("greeting", db, conversation)
        if configured:
            if message:
                wa_response = await whatsapp.send_text_message(phone, message, db)
                _save_outbound_message(conversation, message, db, wa_response)
                logger.info(f"Auto-reply (saudação) processado para {phone}")
            return

    # Existing conversation without attendant — send waiting message
    if not conversation.atendente_id:
        _configured, message = _resolve_auto_reply("waiting", db, conversation)
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

            wa_response = await whatsapp.send_text_message(phone, message, db)
            _save_outbound_message(conversation, message, db, wa_response)
            logger.info(f"Auto-reply (aguardando) processado para {phone}")


def _save_outbound_message(conversation: Conversation, content: str, db: Session, wa_response):
    """
    Save an auto-reply message to the database.
    CONV-08b: status fiel ao resultado do envio (nunca 'sent' em falha).
    """
    return record_outbound_message(
        db, conversation, content, "text", wa_response,
        update_preview=False,
    )


def _customer_msg_at(msg: dict) -> datetime:
    """
    AUDIT-2026-08-W1D (F6) — ancora da janela de 24h no relogio da META.

    O `timestamp` (epoch em segundos) vinha sendo lido e jogado fora; a janela era
    ancorada em `datetime.now()`, ou seja, na hora em que NOS recebemos. Numa
    reentrega da Meta ou com fila acumulada a nossa janela fica mais tarde que a
    dela: o sistema mostra "aberta", o operador manda texto livre e a Meta recusa
    com 131047 na cara dele. A Meta e a dona do relogio — usamos o dela.

    Ausente/ilegivel -> now() (nao ha nada melhor, e o comportamento antigo).
    """
    try:
        return datetime.fromtimestamp(int(msg["timestamp"]), tz.utc)
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        return datetime.now(tz.utc)


def _advance_customer_msg_at(conversation: Conversation, msg_at: datetime) -> datetime:
    """
    AUDIT-2026-08-W1D (F6): a ancora NUNCA anda para tras. Reentrega de uma
    mensagem antiga nao pode encolher uma janela que uma mensagem mais nova ja
    abriu. `max()` com normalizacao UTC (SQLite devolve naive, Postgres aware).
    """
    prev = conversation.last_customer_msg_at
    if prev is None:
        return msg_at
    if prev.tzinfo is None:
        prev = prev.replace(tzinfo=tz.utc)
    return max(prev, msg_at)


async def _process_incoming_message(msg: dict, value: dict, db: Session):
    """Process a single incoming WhatsApp message."""
    whatsapp_number = msg.get("from", "")
    # `or None`: sem id, "" colidiria no UNIQUE de whatsapp_msg_id — a segunda
    # mensagem sem id estouraria IntegrityError. NULL nao colide.
    msg_id = msg.get("id") or None
    msg_type = msg.get("type", "text")
    # AUDIT-2026-08-W1D (F6): antes `timestamp` era lido e nunca mais usado.
    msg_at = _customer_msg_at(msg)

    # Extract content based on message type
    content = ""
    media_url = None
    media_meta = None  # CONV-01: metadados Meta p/ media_assets (so tipos de midia)

    if msg_type == "text":
        content = msg.get("text", {}).get("body", "")
    elif msg_type in ("image", "video", "audio", "document"):
        media_data = msg.get(msg_type, {})
        content = media_data.get("caption", f"[{msg_type.upper()}]")
        media_url = media_data.get("id")  # Media ID (needs download via Graph API)
        # CONV-01: o payload da Meta JA traz metadados publicos — capturar em vez
        # de descartar (mime_type/sha256 sempre; filename so em document).
        media_meta = {
            "meta_media_id": media_data.get("id"),
            "meta_mime_type": media_data.get("mime_type"),
            "meta_sha256": media_data.get("sha256"),
            "filename": media_data.get("filename"),
        }
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

    # CONV-WINDOW-01: a documentacao oficial da Meta diz que a janela abre quando
    # "a WhatsApp user messages you or calls you", mas NAO enumera os tipos, e a
    # referencia de webhook de `reaction` nao menciona a customer service window
    # em nenhum ponto. Sem prova oficial, reagir NAO reabre a janela: um
    # falso-ABERTO liberaria texto livre que a Meta recusaria (erro na cara do
    # operador); um falso-FECHADO no maximo exige um template a mais.
    # A reacao continua sendo persistida como Message e continua contando em
    # unread_count — so nao avanca o relogio da janela.
    opens_window = msg_type != "reaction"

    # Extract sender name from contacts
    sender_name = ""
    contacts = value.get("contacts", [])
    if contacts:
        profile = contacts[0].get("profile", {})
        sender_name = profile.get("name", "")

    # Check if message already processed (idempotency).
    # ANTES de tocar na Conversation: um retry da Meta nao pode incrementar
    # unread_count nem sobrescrever ultimo_msg/status/last_customer_msg_at.
    # So dedupa quando HA id: com msg_id None, `== msg_id` viraria `IS NULL` e
    # casaria com QUALQUER mensagem anterior sem id — a segunda seria descartada
    # como duplicata da primeira (perda silenciosa, com 200 devolvido a Meta).
    if msg_id:
        existing = db.query(Message).filter(Message.whatsapp_msg_id == msg_id).first()
        if existing:
            logger.info(f"Mensagem duplicada ignorada: {msg_id}")
            return

    # Find or create conversation
    is_new_conversation = False
    conversation = db.query(Conversation).filter(
        Conversation.whatsapp == whatsapp_number
    ).first()

    if not conversation:
        conversation = Conversation(
            lead_id=0,  # Will be linked later via CRM
            whatsapp=whatsapp_number,
            nome=sender_name or whatsapp_number,
            status="aberta",
            ultimo_msg=content[:200] if content else "",
            unread_count=1,
            last_customer_msg_at=msg_at if opens_window else None,
        )
        db.add(conversation)
        # AUDIT-2026-08-F2 — a corrida de PRIMEIRO CONTATO precisa ser resolvida
        # AQUI, e precisa existir ANTES de a m011 rodar.
        #
        # Isto e um busca-e-cria-se-nao-achar sem lock. Duas mensagens do mesmo
        # numero chegando juntas (a Meta agrupa, e o cliente manda em rajada)
        # entram as duas no ramo de criacao. Hoje, sem o indice unico, o perdedor
        # cria uma SEGUNDA conversa e o historico do cliente nasce partido em
        # dois — ruim, mas visivel e recuperavel.
        #
        # Assim que `uq_conversations_whatsapp` (m011) existir, o perdedor passa
        # a levantar IntegrityError, que NAO esta em _INFRA_ERRORS de proposito:
        # ele cairia no except que devolve 200 a Meta, e a Meta nunca reenvia.
        # A conversa duplicada viraria MENSAGEM PERDIDA — a migration trocaria um
        # defeito visivel por um invisivel.
        #
        # A saida e a canonica: quem perde a corrida releva o erro, volta a
        # buscar e segue com a linha que o vencedor criou.
        try:
            db.flush()
            is_new_conversation = True
            logger.info(f"Nova conversa criada: {sender_name} ({whatsapp_number})")
        except sa_exc.IntegrityError:
            db.rollback()
            conversation = db.query(Conversation).filter(
                Conversation.whatsapp == whatsapp_number
            ).first()
            if conversation is None:
                # Nao foi a corrida do numero: e outra constraint, e nao ha o que
                # reconciliar. Deixa subir para o tratamento por mensagem.
                raise
            logger.info(
                f"Corrida de primeiro contato em {whatsapp_number}: outra "
                f"requisicao criou a conversa {conversation.id}; seguindo com ela"
            )

    # `is_new_conversation` so vira True apos o flush dar certo, entao quem
    # PERDEU a corrida cai aqui junto com quem ja tinha conversa — sem repetir as
    # duas linhas de atualizacao e, mais importante, sem pular a reabertura
    # abaixo. Era `else:` do `if not conversation:` e nao cobria o perdedor.
    if not is_new_conversation:
        # Update existing conversation
        conversation.ultimo_msg = content[:200] if content else conversation.ultimo_msg
        conversation.unread_count = (conversation.unread_count or 0) + 1
        # PACOTE-A: REABERTURA e um ciclo NOVO de atendimento — volta para a
        # BIA e nao herda o atendente anterior. Mensagem em conversa que JA
        # estava aberta nao pode tocar o estado operacional (senao qualquer
        # inbound reativaria a BIA e furaria o FIFO).
        if conversation.status == "encerrada":
            conversation.atendente_id = None
            conversation.is_bot_active = True
            conversation.queued_at = None
        conversation.status = "aberta"
        if opens_window:
            conversation.last_customer_msg_at = _advance_customer_msg_at(conversation, msg_at)
        if sender_name and not conversation.nome:
            conversation.nome = sender_name

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

    # CONV-01: persiste a referencia de midia em transacao PROPRIA — uma falha
    # aqui nunca pode desfazer/perder a mensagem inbound ja commitada.
    if media_meta and media_meta.get("meta_media_id"):
        try:
            db.add(MediaAsset(
                message_id=message.id,
                meta_media_id=media_meta["meta_media_id"],
                meta_mime_type=media_meta["meta_mime_type"],
                meta_sha256=media_meta["meta_sha256"],
                filename=media_meta["filename"],
                status="referenced",
            ))
            db.commit()
        except Exception as e:
            db.rollback()
            logger.warning(
                f"Falha ao registrar media_asset da mensagem {message.id}: {type(e).__name__}"
            )

    logger.info(f"Mensagem recebida de {sender_name} ({whatsapp_number}): {content[:50]}")

    # Mark as read on WhatsApp
    await whatsapp.mark_as_read(msg_id, db)

    # ─── CRM Auto-Link: vincular conversa ao lead do CRM ───
    if is_new_conversation or (conversation.lead_id is None or conversation.lead_id <= 0):
        linked = await crm_service.auto_link_conversation(conversation, db)
        if linked:
            logger.info(f"Conversa auto-vinculada ao lead CRM #{conversation.lead_id}")

    # AUDIT-2026-08-W1D (F7): `opens_window` ja dizia que reagir NAO reabre a
    # janela, mas a resposta automatica e o encaminhamento a Bia rodavam assim
    # mesmo e ninguem consultava a janela. Uma reacao a uma mensagem antiga
    # disparava um envio de texto livre com recusa GARANTIDA (131047) da Meta.
    if not opens_window and not service_window_open(conversation.last_customer_msg_at):
        logger.info(
            f"Janela de 24h fechada e {msg_type} nao reabre: auto-reply e agente "
            f"pulados para conversa {conversation.id} (envio seria recusado pela Meta)"
        )
        return

    # AUDIT-2026-08-W1D (F2): o corte do lote pendente TEM que ser fotografado
    # aqui, ANTES de `_send_auto_reply_if_needed` commitar qualquer outbound.
    forward_to_agent = N8N_AGENT_ENABLED and conversation.is_bot_active
    if forward_to_agent:
        _remember_agent_cutoff(conversation.id, db)

    # Send auto-reply if needed
    await _send_auto_reply_if_needed(conversation, is_new_conversation, db)

    # ─── N8N Agent: encaminhar para IA se bot ativo (com debounce) ───
    if forward_to_agent:
        _schedule_agent_debounce(conversation.id)


async def _process_status_update(status_update: dict, db: Session):
    """Process a message status update (sent, delivered, read, failed)."""
    msg_id = status_update.get("id", "")
    new_status = status_update.get("status", "")

    if not msg_id or not new_status:
        return

    message = db.query(Message).filter(Message.whatsapp_msg_id == msg_id).first()
    if not message:
        # AUDIT-2026-08-W1D (F5): antes isto era um `return` mudo. Status para um
        # wamid desconhecido significa que o Message nao foi persistido, que o
        # wamid nao foi gravado, ou que a Meta esta falando de outro numero —
        # todas hipoteses que so aparecem se alguem puder ver.
        logger.warning(f"Status '{new_status}' para wamid desconhecido no banco: {msg_id}")
        return

    # AUDIT-2026-08-W1D (F5) — SO AVANCA, nunca regride.
    current = message.status or ""
    if current == "failed" and new_status != "failed":
        logger.info(
            f"Status '{new_status}' IGNORADO para {msg_id}: 'failed' e terminal "
            f"(um 'sent' atrasado esconderia a nao-entrega do operador)"
        )
        return
    if new_status != "failed" and _STATUS_RANK.get(new_status, 0) <= _STATUS_RANK.get(current, 0):
        logger.info(
            f"Status '{new_status}' IGNORADO para {msg_id}: nao avanca sobre "
            f"'{current}' (a Meta nao garante ordem de callback)"
        )
        return

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


def _remember_agent_cutoff(conversation_id: int, db: Session):
    """
    AUDIT-2026-08-W1D (F2) — CORTE DO LOTE PENDENTE, FOTOGRAFADO ANTES DA
    RESPOSTA AUTOMATICA.

    O bug: `_debounce_then_forward` deduzia o lote como "inbound mais novo que o
    ULTIMO outbound". Mas `_send_auto_reply_if_needed` COMMITA um outbound logo
    antes do agendamento, e esse outbound e sempre mais novo que a mensagem do
    cliente — `pending_msgs` voltava VAZIO e a funcao retornava no guard. Como a
    saudacao dispara em TODA conversa nova, a primeira mensagem de todo lead novo
    nunca era processada pela Bia. A resposta de "aguardando" e deduplicada por
    hora, entao em producao a falha parecia intermitente.

    A correcao nao infere processamento a partir de timestamp de outbound —
    resposta automatica NAO e processamento do agente. O corte e tirado antes de
    qualquer envio automatico e guardado aqui. (Sem coluna nova: a migration
    pertence a outra wave.)

    `if not in`: se ja existe corte, ha um lote em curso e o corte dele e o mais
    ANTIGO (foi tirado antes, no tempo) — sobrescrever faria o lote esquecer as
    mensagens que ja estavam nele. A entrada e removida quando o lote e
    consumido, em `_debounce_then_forward`.
    """
    if conversation_id in _debounce_cutoffs:
        return
    last_outbound = db.query(Message.created_at).filter(
        Message.conversation_id == conversation_id,
        Message.direction == "outbound",
    ).order_by(Message.created_at.desc()).first()
    # None = nenhum outbound ainda -> TODO o inbound e pendente (conversa nova).
    _debounce_cutoffs[conversation_id] = last_outbound[0] if last_outbound else None


def _schedule_agent_debounce(conversation_id: int):
    """
    Schedule (or reschedule) the agent forwarding with debounce.
    Each new message resets the timer. When the timer expires,
    all accumulated messages are sent at once to the agent.

    AUDIT-2026-08-W1D (F2): a assinatura fica de UM argumento de proposito — o
    corte viaja por `_debounce_cutoffs` (ver `_remember_agent_cutoff`), que e
    quem conhece a regra de preservar o corte mais antigo do lote.
    """
    # Cancel any existing scheduled task for this conversation
    existing_task = _debounce_tasks.get(conversation_id)
    if existing_task and not existing_task.done():
        existing_task.cancel()
        logger.debug(f"Debounce resetado para conversa {conversation_id}")

    # Schedule a new task
    task = asyncio.create_task(
        _debounce_then_forward(conversation_id, _debounce_cutoffs.get(conversation_id))
    )
    _debounce_tasks[conversation_id] = task


async def _debounce_then_forward(conversation_id: int, cutoff=None):
    """
    Wait AGENT_DEBOUNCE_SECONDS, then forward all recent unprocessed
    messages to the AI agent as a single batch.
    """
    try:
        await asyncio.sleep(AGENT_DEBOUNCE_SECONDS)
    except asyncio.CancelledError:
        # Another message arrived — this task was replaced
        return

    # Clean up the task reference
    _debounce_tasks.pop(conversation_id, None)
    _debounce_cutoffs.pop(conversation_id, None)

    # Use a fresh DB session (we're in a background task)
    db = SessionLocal()
    try:
        conversation = db.query(Conversation).filter(
            Conversation.id == conversation_id
        ).first()

        if not conversation:
            logger.warning(f"Conversa {conversation_id} não encontrada para debounce")
            return

        if not conversation.is_bot_active:
            logger.info(f"Bot desativado para conversa {conversation_id}, ignorando")
            return

        # AUDIT-2026-08-W1D (F2) — o lote pendente vem do CORTE fotografado no
        # webhook, nao do ultimo outbound consultado agora. Consultar agora
        # incluiria a resposta automatica que acabou de ser commitada, que e
        # sempre mais nova que a mensagem do cliente: o lote vinha vazio e a Bia
        # nunca respondia. `cutoff is None` = conversa sem outbound nenhum no
        # momento em que a mensagem chegou -> todo o inbound e pendente.
        query = db.query(Message).filter(
            Message.conversation_id == conversation_id,
            Message.direction == "inbound",
        )
        if cutoff is not None:
            query = query.filter(Message.created_at > cutoff)

        pending_msgs = query.order_by(Message.created_at.asc()).all()

        if not pending_msgs:
            logger.debug(f"Nenhuma mensagem pendente para conversa {conversation_id}")
            return

        # Combine all pending messages into one text
        combined_text = "\n".join(m.content for m in pending_msgs if m.content)
        logger.info(
            f"Debounce: enviando {len(pending_msgs)} msg(s) agrupadas "
            f"para agente — conversa {conversation_id}"
        )

        await _forward_to_agent(conversation, combined_text, db)
    except Exception as e:
        logger.error(f"Erro no debounce da conversa {conversation_id}: {e}", exc_info=True)
    finally:
        db.close()


def _split_agent_reply(resposta: str) -> list:
    """`|||` e quebras de paragrafo -> partes, para um ritmo natural no WhatsApp."""
    partes = []
    for raw in (resposta or "").split("|||"):
        partes.extend(p.strip() for p in re.split(r"\n\s*\n", raw) if p.strip())
    return partes


async def _fetch_agent_parts(agent_url: str, payload: dict, conversation_id):
    """
    CONV-AGENT-01 — devolve `(partes, silencio)`.

    AUDIT-2026-08-F2 — antes devolvia so `list`, e a docstring dizia que "quem
    chama nao precisa distinguir os modos de falha". Precisa: sao DOIS
    resultados diferentes, com acoes opostas.

      partes nao vazio        -> responder ao cliente com essas partes
      ([], silencio=True)     -> a Bia DECIDIU nao responder. Nao mandar nada,
                                 nao logar erro.
      ([], silencio=False)    -> a Bia nao CONSEGUIU responder. Fallback.

    O workflow atual da Bia tem um portao para mensagem composta so de emoji
    (node `Ignorar mensagem`), que responde 204 sem corpo. Enquanto todo
    nao-200 era degradacao, um cliente que mandasse um polegar pra cima
    recebia "Tive uma instabilidade para processar sua mensagem agora" — e cada
    reacao gravava uma linha de ERRO no log de um evento perfeitamente normal.

    Tambem aceita `200 {"ignorar": true}` como silencio, para o caso de o
    workflow preferir responder 200 — assim a correcao nao depende de qual das
    duas formas o operador aplicar no n8n.

    Cobre, sem levantar: timeout (leitura e conexao), conexao recusada e demais
    erros de rede, HTTP != 200, corpo que nao e JSON, JSON sem `resposta` e
    `resposta` vazia — todos deixavam o cliente sem resposta nenhuma.

    NAO ha retry, deliberadamente. O n8n usa `responseMode: responseNode`: ao
    abandonarmos a conexao a execucao CONTINUA no servidor dele, entao repetir
    o POST criaria uma segunda execucao da Bia — respostas e acoes de tool
    duplicadas para o mesmo cliente.
    """
    try:
        async with httpx.AsyncClient(timeout=AGENT_TIMEOUT) as client:
            resp = await client.post(agent_url, json=payload)

            # 204/205 = "recebi e nao ha o que responder". E a resposta certa
            # para o portao de emoji, e NAO e degradacao.
            if resp.status_code in (204, 205):
                logger.debug(
                    f"Agente IA sinalizou silencio ({resp.status_code}) "
                    f"para a conversa {conversation_id}"
                )
                return [], True

            if resp.status_code != 200:
                logger.warning(
                    f"Agente IA retornou status {resp.status_code} "
                    f"(conversa {conversation_id}): {resp.text[:200]}"
                )
                return [], False

            corpo = resp.json()
            if corpo.get("ignorar") is True:
                logger.debug(
                    f"Agente IA pediu para ignorar a conversa {conversation_id}"
                )
                return [], True

            partes = _split_agent_reply(corpo.get("resposta", ""))
            if not partes:
                logger.warning(
                    f"Agente IA respondeu 200 sem texto utilizavel "
                    f"(conversa {conversation_id})"
                )
            return partes, False
    except httpx.TimeoutException:
        logger.warning(
            f"Timeout ({AGENT_TIMEOUT.read}s) ao chamar agente IA "
            f"para conversa {conversation_id}"
        )
        return [], False
    except Exception as e:
        # Rede, DNS, conexao recusada, corpo nao-JSON: resumo seguro, sem o
        # texto bruto da excecao (pode carregar URL/payload).
        logger.error(
            f"Erro ao encaminhar para agente IA (conversa {conversation_id}): "
            f"{type(e).__name__}"
        )
        return [], False


async def _forward_to_agent(conversation: Conversation, message_text: str, db: Session):
    """
    Forward the incoming message to the N8N AI Agent (WF-10 Bia).
    The agent processes the message and returns a response that gets sent
    back to the customer via WhatsApp.

    CONV-AGENT-01: obter a resposta e ENVIAR a resposta sao passos separados.
    Se a Bia nao responder, `partes` vira uma unica mensagem generica de
    degradacao — e o MESMO laco de envio/persistencia roda, entao o fallback
    chega a Meta e ao historico exatamente como uma resposta normal chegaria.
    Nao existe caminho em que o cliente escreve e nao recebe nada.
    """
    # AUDIT-2026-08-W1D (F8) — historico LIMITADO e so com o que o cliente viu.
    #
    # A variavel se chamava `recent_msgs` mas a query nao tinha `.limit()` nem
    # janela: em toda rodada o payload carregava a conversa INTEIRA para o n8n.
    # Numa conversa longa isso incha a chamada, o custo de token da Bia e o
    # tempo de resposta, sem ganho — o agente ja tem o contexto recente.
    # Pior: incluia outbound 'failed', mensagens que NUNCA chegaram ao cliente,
    # e a Bia raciocinava como se ele as tivesse lido.
    # Ordena DESC + limit para pegar as N ULTIMAS (e nao as N primeiras) e
    # inverte para a ordem cronologica que o agente espera.
    recent_msgs = db.query(Message).filter(
        Message.conversation_id == conversation.id,
        or_(Message.direction != "outbound", Message.status != "failed"),
    ).order_by(Message.created_at.desc(), Message.id.desc()).limit(AGENT_HISTORY_LIMIT).all()
    recent_msgs.reverse()

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

    partes, silencio = await _fetch_agent_parts(agent_url, payload, conversation.id)

    # AUDIT-2026-08-F2: silencio DELIBERADO da Bia (portao de emoji) nao e
    # degradacao. Sai daqui sem enviar nada e sem marcar falha — mandar o
    # fallback aqui era responder "tive uma instabilidade" a quem so reagiu com
    # um emoji.
    if silencio:
        return

    # Degradado = a Bia nao respondeu. UMA mensagem generica, montada aqui e nao
    # pedida ao n8n de novo: fallback nunca dispara uma segunda execucao da Bia.
    degraded = not partes
    if degraded:
        partes = [AGENT_FALLBACK_REPLY]

    # CONV-08b: cada parte e persistida com status fiel ao envio;
    # preview/unread so mudam se ao menos uma parte foi aceita.
    last_ok_part = None
    for i, parte in enumerate(partes):
        # Send each part as a separate WhatsApp message
        wa_response = await whatsapp.send_text_message(conversation.whatsapp, parte, db)

        # Small delay between messages for natural feel
        if i < len(partes) - 1:
            await asyncio.sleep(1.2)

        # Save each part as outbound message (status real)
        agent_msg = record_outbound_message(
            db, conversation, parte, "text", wa_response,
            update_preview=False, commit=False,
        )
        # AUDIT-2026-08-W1D (F3): em development o envio simulado tambem "passou"
        # — nao e falha. Nao aceitar 'simulated' aqui faria todo dev ver o log de
        # "cliente sem resposta" a cada resposta da Bia.
        if agent_msg.status in NOT_FAILED_STATUSES:
            last_ok_part = parte

    # Update conversation preview only with the last SENT part.
    # NADA de estado operacional aqui: `is_bot_active`, `atendente_id` e
    # `queued_at` seguem intactos — uma falha da Bia nao move a conversa de fila.
    if last_ok_part is not None:
        conversation.ultimo_msg = last_ok_part[:200]
        conversation.unread_count = 0
    db.commit()

    # O fallback tambem pode falhar no envio (Meta fora, credencial ruim). Ele ja
    # fica persistido como 'failed' + last_error por record_outbound_message; o
    # log em nivel ERROR marca o unico caso que exige intervencao humana — o
    # cliente escreveu e nao recebeu nada.
    if degraded and last_ok_part is None:
        logger.error(
            f"Fallback da Bia NAO foi entregue para {conversation.whatsapp} "
            f"(conversa {conversation.id}) — cliente sem resposta"
        )
    elif degraded:
        logger.warning(
            f"Fallback da Bia enviado para {conversation.whatsapp} "
            f"(conversa {conversation.id})"
        )
    else:
        logger.info(f"Resposta da Bia ({len(partes)} msgs) para {conversation.whatsapp}")
