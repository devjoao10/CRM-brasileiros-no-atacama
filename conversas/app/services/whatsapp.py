"""
Meta Cloud API (WhatsApp Business) client.
Handles sending messages via the official API.

Credentials are read from the database (ApiConfig table)
so they can be configured via the Settings panel at runtime.
Falls back to environment variables if DB config is not available.
"""

import asyncio
import logging
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.config import (
    ENVIRONMENT,
    META_ACCESS_TOKEN,
    META_PHONE_NUMBER_ID,
    META_API_BASE,
    META_WABA_ID,
)
from app.models.api_config import ApiConfig

logger = logging.getLogger(__name__)


def _get_credentials(db: Optional[Session] = None) -> tuple[str, str, str]:
    """
    Get API credentials. Priority: DB config > environment variables.
    Returns: (access_token, phone_number_id, api_base_url)
    """
    if db:
        config = db.query(ApiConfig).filter(ApiConfig.id == 1).first()
        if config and config.is_connected and config.meta_access_token and config.meta_phone_number_id:
            version = config.meta_api_version or "v21.0"
            base = f"https://graph.facebook.com/{version}"
            return config.meta_access_token, config.meta_phone_number_id, base

    # Fallback to env vars
    return META_ACCESS_TOKEN, META_PHONE_NUMBER_ID, META_API_BASE


def get_waba_credentials(db: Optional[Session] = None) -> Optional[tuple[str, str, str]]:
    """
    Credenciais de NIVEL WABA (gestao de templates), com a MESMA prioridade do
    envio: DB primeiro, env como fallback.

    Existe porque `_get_credentials` devolve o phone_number_id (envio) e a Graph
    API de templates e enderecada pelo WABA_ID. Antes deste pacote o servico de
    templates lia SO do banco, entao um deploy configurado por variavel de
    ambiente — que e como producao roda (docker-compose.yml passa META_WABA_ID) —
    respondia "Meta API nao configurada" mesmo com o envio de mensagens funcionando.

    Retorna (access_token, waba_id, api_base) ou None se faltar qualquer um.
    """
    if db:
        config = db.query(ApiConfig).filter(ApiConfig.id == 1).first()
        if config and config.is_connected and config.meta_access_token and config.meta_waba_id:
            version = config.meta_api_version or "v21.0"
            return config.meta_access_token, config.meta_waba_id, f"https://graph.facebook.com/{version}"

    if META_ACCESS_TOKEN and META_WABA_ID:
        return META_ACCESS_TOKEN, META_WABA_ID, META_API_BASE
    return None


def is_configured(db: Optional[Session] = None) -> bool:
    """Check if Meta Cloud API credentials are configured."""
    token, phone_id, _ = _get_credentials(db)
    return bool(token and phone_id)


def _error_result(status_code: Optional[int], summary: str) -> dict:
    """
    CONV-08b: resultado padronizado de falha de envio.
    `summary` deve ser SEGURO: nunca incluir token, headers, phone_number_id
    ou payload bruto. Callers persistem isso em Message.last_error.
    """
    return {"error": True, "status_code": status_code, "summary": summary[:300]}


def _unconfigured_result(what: str, **simulated_extra) -> dict:
    """
    AUDIT-2026-08-W1D (F3/F4) — resposta unica para "credenciais Meta ausentes".

    POR QUE: antes, TODO `send_*` devolvia `{"simulated": True}` quando faltava
    token/phone_number_id. Em producao — token rotacionado, .env vazio, ApiConfig
    desconectado — `outbound.classify_wa_response` lia isso como sucesso e o
    `record_outbound_message` gravava 'sent'. Resultado: CADA resposta ao cliente
    ficava marcada como entregue enquanto NADA saia do servidor, e o unico sinal
    era uma linha de log em nivel INFO. Falha silenciosa de negocio.

    Fora de development isto passa a ser uma FALHA REAL: a mensagem e persistida
    como 'failed' com last_error, aparece para o operador e fica reenviavel.
    Em development o modo simulado continua existindo (nao ha credencial em dev),
    mas com marcador proprio — quem persiste grava status 'simulated', nunca 'sent'.

    F4: `send_reaction` devolvia `None` cru aqui, quebrando o contrato que o
    `outbound.py` documenta; agora usa exatamente o mesmo resultado.
    """
    if ENVIRONMENT != "development":
        logger.error(
            f"Meta Cloud API NAO configurada em ambiente '{ENVIRONMENT}': {what} "
            f"NAO foi enviado. Verifique META_ACCESS_TOKEN/META_PHONE_NUMBER_ID."
        )
        return _error_result(
            None,
            "Meta Cloud API nao configurada (token/phone_number_id ausentes): "
            "nenhuma mensagem foi transmitida",
        )

    logger.warning(f"Meta Cloud API não configurada (development). {what} simulado.")
    return {"simulated": True, **simulated_extra}


def _http_error_summary(e: httpx.HTTPStatusError) -> dict:
    """Extrai um resumo seguro de um erro HTTP da Meta (status + error.message/code)."""
    summary = f"HTTP {e.response.status_code}"
    try:
        err = e.response.json().get("error", {})
        if err.get("message"):
            summary += f": {err['message']}"
        if err.get("code") is not None:
            summary += f" (code {err['code']})"
    except Exception:
        pass
    return _error_result(e.response.status_code, summary)


# AUDIT-2026-08-WD (D3): retry limitado para os `send_*` — nenhum tinha loop,
# backoff ou tratamento especial de 429/5xx, e `Message.send_attempts`/
# `last_attempt_at` existiam sem nada que os justificasse como "base do
# retry" (comentario original em CONV-08b/m003).
#
# So a classe REALMENTE transitoria dispara retry: 429 (a Meta pede para
# esperar — honra `Retry-After`) e 5xx (instabilidade do lado da Meta), mais
# timeout de conexao/leitura. Qualquer outro 4xx (token invalido, numero
# bloqueado, payload rejeitado, template nao aprovado...) e PERMANENTE:
# repetir devolve a MESMA resposta, so mais devagar, e no caso de credencial
# ruim chega a martelar a API com um token ja sabido invalido.
#
# 2 retries (3 tentativas no total) com backoff curto fixo (1s, depois 2s):
# isto roda DENTRO da requisicao HTTP que o atendente esta esperando (nao um
# job em background), entao o teto de latencia extra tem que ficar em
# segundos, nao minutos. O `Retry-After` da Meta e respeitado mas TAMBEM tem
# teto, pelo mesmo motivo — nao ficamos presos ao tempo que a Meta pedir.
# Os timeouts por chamada (`timeout=` de cada `send_*`) NAO mudam: o teto
# esta no NUMERO de tentativas e no backoff, nao encurtando cada uma.
_MAX_RETRIES = 2
_RETRY_BACKOFF_SECONDS = (1.0, 2.0)
_MAX_RETRY_AFTER_SECONDS = 3.0


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Honra o `Retry-After` da Meta (com teto); senao usa o backoff fixo do indice `attempt`."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), _MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            pass
    return _RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)]


async def _post_with_retry(url: str, *, json: dict, headers: dict, timeout: float) -> httpx.Response:
    """
    POST com retry (ver constantes acima). Levanta exatamente o que uma
    chamada UNICA levantaria — `httpx.HTTPStatusError` de `raise_for_status()`
    ou a excecao de rede — para que os `except` de cada `send_*` continuem
    validos sem nenhuma mudanca.
    """
    attempt = 0
    while True:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=json, headers=headers)
            if _is_retryable_status(response.status_code) and attempt < _MAX_RETRIES:
                delay = _retry_delay(response, attempt)
                logger.warning(
                    f"Meta HTTP {response.status_code}; nova tentativa em "
                    f"{delay:.1f}s ({attempt + 1}/{_MAX_RETRIES})"
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue
            response.raise_for_status()
            return response
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError):
            if attempt >= _MAX_RETRIES:
                raise
            delay = _RETRY_BACKOFF_SECONDS[min(attempt, len(_RETRY_BACKOFF_SECONDS) - 1)]
            logger.warning(
                f"Timeout/conexao ao chamar a Meta; nova tentativa em "
                f"{delay:.1f}s ({attempt + 1}/{_MAX_RETRIES})"
            )
            await asyncio.sleep(delay)
            attempt += 1


async def send_text_message(to: str, text: str, db: Optional[Session] = None) -> Optional[dict]:
    """
    Send a text message via WhatsApp Cloud API.

    Args:
        to: Phone number in international format (e.g. '5511999998888')
        text: Message text content
        db: Database session for credential lookup

    Returns:
        API response dict or None if failed/unconfigured
    """
    token, phone_id, base = _get_credentials(db)
    if not token or not phone_id:
        return _unconfigured_result("Mensagem de texto", to=to, text=text)

    url = f"{base}/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }

    try:
        response = await _post_with_retry(url, json=payload, headers=headers, timeout=15.0)
        data = response.json()
        logger.info(f"Mensagem enviada para {to}: wamid={data.get('messages', [{}])[0].get('id', '?')}")
        return data
    except httpx.HTTPStatusError as e:
        logger.error(f"Erro HTTP ao enviar mensagem: {e.response.status_code} - {e.response.text}")
        return _http_error_summary(e)
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem: {e}")
        return _error_result(None, f"Erro de rede/cliente: {type(e).__name__}")


async def get_media_url(media_id: str, db: Optional[Session] = None) -> Optional[dict]:
    """
    CONV-02: resolve um media_id da Meta para a URL temporaria de download.

    Retorna (contrato padrao do adapter):
      - dict {"url", "mime_type", "sha256", "file_size", "id"} no sucesso
      - dict {"simulated": True} sem credenciais (dev)
      - dict {"error": True, "status_code", "summary"} em falha real
    A URL retornada expira em ~5 minutos — consumir imediatamente.
    """
    token, phone_id, base = _get_credentials(db)
    if not token or not phone_id:
        logger.warning("Meta Cloud API não configurada. get_media_url simulado.")
        return {"simulated": True, "media_id": media_id}

    url = f"{base}/{media_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"Erro HTTP ao resolver media_id: {e.response.status_code}")
        return _http_error_summary(e)
    except Exception as e:
        logger.error(f"Erro ao resolver media_id: {e}")
        return _error_result(None, f"Erro de rede/cliente: {type(e).__name__}")


async def download_media_content(media_url: str, db: Optional[Session] = None) -> Optional[dict]:
    """
    CONV-02: baixa o binario de uma URL de midia da Meta (autenticada por token).

    Retorna:
      - dict {"content": bytes} no sucesso
      - dict {"simulated": True} sem credenciais (dev)
      - dict {"error": True, "status_code", "summary"} em falha real
    """
    token, phone_id, _ = _get_credentials(db)
    if not token or not phone_id:
        return {"simulated": True}

    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.get(media_url, headers=headers)
            response.raise_for_status()
            return {"content": response.content}
    except httpx.HTTPStatusError as e:
        logger.error(f"Erro HTTP ao baixar midia: {e.response.status_code}")
        return _http_error_summary(e)
    except Exception as e:
        logger.error(f"Erro ao baixar midia: {e}")
        return _error_result(None, f"Erro de rede/cliente: {type(e).__name__}")


async def upload_media(
    content: bytes, mime_type: str, db: Optional[Session] = None
) -> Optional[dict]:
    """
    CONV-02: sobe um binario para a Meta e retorna o media_id (fundacao do
    envio outbound de midia — o endpoint/UI chegam no CONV-03/04).

    Retorna:
      - dict {"id": "<media_id>"} no sucesso
      - dict {"simulated": True, "id": None} sem credenciais (dev)
      - dict {"error": True, "status_code", "summary"} em falha real
    """
    token, phone_id, base = _get_credentials(db)
    if not token or not phone_id:
        return _unconfigured_result("Upload de midia", id=None)

    url = f"{base}/{phone_id}/media"
    headers = {"Authorization": f"Bearer {token}"}
    files = {"file": ("upload", content, mime_type)}
    data = {"messaging_product": "whatsapp", "type": mime_type}
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(url, headers=headers, files=files, data=data)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"Erro HTTP no upload de midia: {e.response.status_code} - {e.response.text}")
        return _http_error_summary(e)
    except Exception as e:
        logger.error(f"Erro no upload de midia: {e}")
        return _error_result(None, f"Erro de rede/cliente: {type(e).__name__}")


async def send_media_message(
    to: str, media_type: str, media_url: str = "", caption: str = "",
    db: Optional[Session] = None, *, media_id: Optional[str] = None
) -> Optional[dict]:
    """
    Send a media message (image, audio, document, video) via WhatsApp Cloud API.

    Args:
        to: Phone number
        media_type: One of 'image', 'audio', 'document', 'video'
        media_url: URL of the media file
        caption: Optional caption for the media
        db: Database session for credential lookup

    Returns:
        API response dict or None if failed
    """
    token, phone_id, base = _get_credentials(db)
    if not token or not phone_id:
        return _unconfigured_result("Midia", to=to, media_type=media_type)

    url = f"{base}/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    # CONV-02: envio por media_id (upload previo) tem prioridade sobre link
    media_object = {"id": media_id} if media_id else {"link": media_url}
    if caption and media_type in ("image", "document", "video"):
        media_object["caption"] = caption

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": media_type,
        media_type: media_object,
    }

    try:
        response = await _post_with_retry(url, json=payload, headers=headers, timeout=30.0)
        data = response.json()
        logger.info(f"Mídia ({media_type}) enviada para {to}")
        return data
    except httpx.HTTPStatusError as e:
        logger.error(f"Erro HTTP ao enviar mídia: {e.response.status_code} - {e.response.text}")
        return _http_error_summary(e)
    except Exception as e:
        logger.error(f"Erro ao enviar mídia: {e}")
        return _error_result(None, f"Erro de rede/cliente: {type(e).__name__}")


async def mark_as_read(message_id: str, db: Optional[Session] = None) -> bool:
    """Mark a message as read on WhatsApp."""
    token, phone_id, base = _get_credentials(db)
    if not token or not phone_id:
        return True

    url = f"{base}/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            return response.status_code == 200
    except Exception:
        return False


async def send_template_message(
    to: str, template_name: str, language: str, components: list,
    db: Optional[Session] = None
) -> Optional[dict]:
    """
    Send a template message via WhatsApp Cloud API.
    Required for messaging outside the 24-hour window.

    Args:
        to: Phone number in international format
        template_name: Name of the approved template
        language: Template language code (e.g. 'pt_BR')
        components: List of component parameters (header/body vars)
        db: Database session for credential lookup

    Returns:
        API response dict or None if failed/unconfigured
    """
    token, phone_id, base = _get_credentials(db)
    if not token or not phone_id:
        return _unconfigured_result("Template", to=to, template=template_name)

    url = f"{base}/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language},
            "components": components,
        },
    }

    try:
        response = await _post_with_retry(url, json=payload, headers=headers, timeout=15.0)
        data = response.json()
        logger.info(f"Template '{template_name}' enviado para {to}: {data}")
        return data
    except httpx.HTTPStatusError as e:
        logger.error(f"Erro HTTP ao enviar template: {e.response.status_code} - {e.response.text}")
        return _http_error_summary(e)
    except Exception as e:
        logger.error(f"Erro ao enviar template: {e}")
        return _error_result(None, f"Erro de rede/cliente: {type(e).__name__}")


async def send_reaction(
    message_id: str, emoji: str, to: str, db: Optional[Session] = None
) -> Optional[dict]:
    """
    Send a reaction to a message.

    Args:
        message_id: The WhatsApp message ID to react to
        emoji: The emoji to react with
        to: Phone number of the recipient
        db: Database session for credential lookup
    """
    token, phone_id, base = _get_credentials(db)
    if not token or not phone_id:
        # AUDIT-2026-08-W1D (F4): era `return None` cru — o unico send_* que
        # quebrava o contrato documentado em outbound.py.
        return _unconfigured_result("Reacao", to=to, message_id=message_id)

    url = f"{base}/{phone_id}/messages"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "reaction",
        "reaction": {
            "message_id": message_id,
            "emoji": emoji,
        },
    }

    try:
        response = await _post_with_retry(url, json=payload, headers=headers, timeout=10.0)
        return response.json()
    except Exception as e:
        logger.error(f"Erro ao enviar reação: {e}")
        return None
