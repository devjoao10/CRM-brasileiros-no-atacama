"""
Meta Graph API — Template Management Service.
Handles creating, deleting, and syncing WhatsApp message templates
with the official Meta Cloud API (Graph API).

API Reference:
- Create: POST /{WABA_ID}/message_templates
- List:   GET  /{WABA_ID}/message_templates
- Delete: DELETE /{WABA_ID}/message_templates?name={name}
"""

import logging
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.models.template import MessageTemplate
from app.models.api_config import ApiConfig

logger = logging.getLogger(__name__)


def _get_api_config(db: Session) -> Optional[ApiConfig]:
    """Retrieve the singleton API config from the database."""
    return db.query(ApiConfig).filter(ApiConfig.id == 1).first()


def _is_meta_configured(config: Optional[ApiConfig]) -> bool:
    """Check if Meta API credentials are present and connected."""
    if not config:
        return False
    return bool(
        config.meta_access_token
        and config.meta_waba_id
        and config.is_connected
    )


def _build_headers(config: ApiConfig) -> dict:
    """Build authorization headers for Meta API requests."""
    return {
        "Authorization": f"Bearer {config.meta_access_token}",
        "Content-Type": "application/json",
    }


def _build_base_url(config: ApiConfig) -> str:
    """Build the Graph API base URL."""
    version = config.meta_api_version or "v21.0"
    return f"https://graph.facebook.com/{version}"


def _build_template_components(template: MessageTemplate) -> list:
    """
    Build the 'components' array required by the Meta API
    from our local template model.
    """
    import json

    components = []

    # HEADER component
    if template.header_type and template.header_type == "TEXT" and template.header_text:
        header_comp = {
            "type": "HEADER",
            "format": "TEXT",
            "text": template.header_text,
        }
        # Add example if header has variables
        if "{{" in template.header_text:
            sample_values = {}
            if template.sample_values_json:
                try:
                    sample_values = json.loads(template.sample_values_json)
                except (json.JSONDecodeError, TypeError):
                    pass
            header_examples = sample_values.get("header", [])
            if header_examples:
                header_comp["example"] = {"header_text": header_examples}
        components.append(header_comp)

    elif template.header_type and template.header_type in ("IMAGE", "VIDEO", "DOCUMENT"):
        components.append({
            "type": "HEADER",
            "format": template.header_type,
        })

    # BODY component (always required)
    body_comp = {
        "type": "BODY",
        "text": template.body_text,
    }
    # Add example values for body variables (required by Meta for approval)
    if "{{" in template.body_text:
        sample_values = {}
        if template.sample_values_json:
            try:
                sample_values = json.loads(template.sample_values_json)
            except (json.JSONDecodeError, TypeError):
                pass
        body_examples = sample_values.get("body", [])
        if body_examples:
            body_comp["example"] = {"body_text": [body_examples]}
    components.append(body_comp)

    # FOOTER component
    if template.footer_text:
        components.append({
            "type": "FOOTER",
            "text": template.footer_text,
        })

    # BUTTONS component
    if template.buttons_json:
        try:
            buttons = json.loads(template.buttons_json)
            if buttons:
                meta_buttons = []
                for btn in buttons:
                    btn_type = btn.get("type", "").upper()
                    if btn_type == "QUICK_REPLY":
                        meta_buttons.append({
                            "type": "QUICK_REPLY",
                            "text": btn.get("text", ""),
                        })
                    elif btn_type == "URL":
                        meta_btn = {
                            "type": "URL",
                            "text": btn.get("text", ""),
                            "url": btn.get("url", ""),
                        }
                        meta_buttons.append(meta_btn)
                    elif btn_type == "PHONE_NUMBER":
                        meta_buttons.append({
                            "type": "PHONE_NUMBER",
                            "text": btn.get("text", ""),
                            "phone_number": btn.get("phone_number", ""),
                        })

                if meta_buttons:
                    components.append({
                        "type": "BUTTONS",
                        "buttons": meta_buttons,
                    })
        except (json.JSONDecodeError, TypeError):
            pass

    return components


async def create_template_on_meta(
    template: MessageTemplate, db: Session
) -> dict:
    """
    Submit a template to Meta for approval via Graph API.

    POST https://graph.facebook.com/{version}/{WABA_ID}/message_templates

    Returns:
        {"success": True, "meta_template_id": "..."} or
        {"success": False, "error": "..."}
    """
    config = _get_api_config(db)
    if not _is_meta_configured(config):
        return {"success": False, "error": "Meta API não configurada. Configure as credenciais em Configurações > API WhatsApp."}

    url = f"{_build_base_url(config)}/{config.meta_waba_id}/message_templates"
    headers = _build_headers(config)

    payload = {
        "name": template.name,
        "category": template.category,
        "language": template.language,
        "components": _build_template_components(template),
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, json=payload, headers=headers)

            if response.status_code == 200:
                data = response.json()
                meta_id = data.get("id")
                meta_status = data.get("status", "PENDING")

                # Update local template with Meta response
                template.meta_template_id = meta_id
                template.status = meta_status
                template.rejection_reason = None
                db.commit()

                logger.info(f"Template '{template.name}' submetido ao Meta: ID={meta_id}, status={meta_status}")
                return {"success": True, "meta_template_id": meta_id, "status": meta_status}
            else:
                error_data = response.json()
                error_msg = error_data.get("error", {}).get("message", response.text)
                error_code = error_data.get("error", {}).get("code", response.status_code)
                logger.error(f"Erro ao criar template no Meta: {error_code} - {error_msg}")
                return {"success": False, "error": f"Meta API erro ({error_code}): {error_msg}"}

    except httpx.TimeoutException:
        return {"success": False, "error": "Timeout ao conectar com a Meta API."}
    except Exception as e:
        logger.error(f"Erro inesperado ao criar template no Meta: {e}", exc_info=True)
        return {"success": False, "error": f"Erro inesperado: {str(e)}"}


async def delete_template_on_meta(
    template_name: str, db: Session
) -> dict:
    """
    Delete a template from Meta by name.

    DELETE https://graph.facebook.com/{version}/{WABA_ID}/message_templates?name={name}

    Returns:
        {"success": True} or {"success": False, "error": "..."}
    """
    config = _get_api_config(db)
    if not _is_meta_configured(config):
        return {"success": False, "error": "Meta API não configurada."}

    url = f"{_build_base_url(config)}/{config.meta_waba_id}/message_templates"
    headers = _build_headers(config)
    params = {"name": template_name}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.delete(url, headers=headers, params=params)

            if response.status_code == 200:
                data = response.json()
                if data.get("success"):
                    logger.info(f"Template '{template_name}' deletado do Meta.")
                    return {"success": True}
                else:
                    return {"success": False, "error": "Meta retornou success=false."}
            else:
                error_data = response.json()
                error_msg = error_data.get("error", {}).get("message", response.text)
                return {"success": False, "error": f"Meta API: {error_msg}"}

    except Exception as e:
        logger.error(f"Erro ao deletar template no Meta: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def sync_template_statuses(db: Session) -> dict:
    """
    Fetch all templates from Meta and update local statuses.

    GET https://graph.facebook.com/{version}/{WABA_ID}/message_templates

    Returns:
        {"success": True, "synced": count, "details": [...]} or
        {"success": False, "error": "..."}
    """
    config = _get_api_config(db)
    if not _is_meta_configured(config):
        return {"success": False, "error": "Meta API não configurada. Configure as credenciais em Configurações > API WhatsApp."}

    url = f"{_build_base_url(config)}/{config.meta_waba_id}/message_templates"
    headers = _build_headers(config)
    params = {"limit": 250}

    try:
        all_meta_templates = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Paginate through all templates
            next_url = url
            while next_url:
                response = await client.get(next_url, headers=headers, params=params)
                if response.status_code != 200:
                    error_data = response.json()
                    error_msg = error_data.get("error", {}).get("message", response.text)
                    return {"success": False, "error": f"Meta API: {error_msg}"}

                data = response.json()
                all_meta_templates.extend(data.get("data", []))

                # Check for pagination
                paging = data.get("paging", {})
                next_url = paging.get("next")
                params = {}  # Next URL already has params

        # Build lookup by name
        meta_lookup = {}
        for mt in all_meta_templates:
            name = mt.get("name", "")
            if name:
                meta_lookup[name] = {
                    "id": mt.get("id"),
                    "status": mt.get("status", "PENDING"),
                    "rejected_reason": mt.get("rejected_reason"),
                    "quality_score": mt.get("quality_score", {}).get("score"),
                }

        # Update local templates
        local_templates = db.query(MessageTemplate).all()
        synced = 0
        details = []

        for lt in local_templates:
            meta_info = meta_lookup.get(lt.name)
            if meta_info:
                old_status = lt.status
                lt.meta_template_id = meta_info["id"]
                lt.status = meta_info["status"]
                if meta_info.get("rejected_reason"):
                    lt.rejection_reason = meta_info["rejected_reason"]

                if old_status != lt.status:
                    details.append({
                        "name": lt.name,
                        "old_status": old_status,
                        "new_status": lt.status,
                    })
                synced += 1
            else:
                # Template exists locally but not on Meta
                if lt.meta_template_id:
                    # Was on Meta but got deleted
                    lt.meta_template_id = None
                    lt.status = "PENDING"
                    details.append({
                        "name": lt.name,
                        "old_status": lt.status,
                        "new_status": "PENDING (removido do Meta)",
                    })

        db.commit()
        logger.info(f"Sincronização concluída: {synced} templates atualizados.")
        return {"success": True, "synced": synced, "total_meta": len(all_meta_templates), "details": details}

    except httpx.TimeoutException:
        return {"success": False, "error": "Timeout ao conectar com a Meta API."}
    except Exception as e:
        logger.error(f"Erro na sincronização: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def test_connection(db: Session) -> dict:
    """
    Test the Meta API connection by fetching the WABA info.

    GET https://graph.facebook.com/{version}/{WABA_ID}

    Returns:
        {"success": True, "waba_name": "...", "phone_number": "..."} or
        {"success": False, "error": "..."}
    """
    config = _get_api_config(db)
    if not config:
        return {"success": False, "error": "Nenhuma configuração encontrada."}

    if not config.meta_access_token or not config.meta_waba_id:
        return {"success": False, "error": "Access Token e WABA ID são obrigatórios."}

    base_url = _build_base_url(config)
    headers = _build_headers(config)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Test WABA access
            resp = await client.get(
                f"{base_url}/{config.meta_waba_id}",
                headers=headers,
                params={"fields": "name,id,currency,timezone_id"}
            )

            if resp.status_code != 200:
                error_data = resp.json()
                error_msg = error_data.get("error", {}).get("message", resp.text)
                return {"success": False, "error": f"Erro ao acessar WABA: {error_msg}"}

            waba_data = resp.json()

            # Test phone number access (if configured)
            phone_info = None
            if config.meta_phone_number_id:
                phone_resp = await client.get(
                    f"{base_url}/{config.meta_phone_number_id}",
                    headers=headers,
                    params={"fields": "display_phone_number,verified_name,quality_rating"}
                )
                if phone_resp.status_code == 200:
                    phone_info = phone_resp.json()

            return {
                "success": True,
                "waba_name": waba_data.get("name", ""),
                "waba_id": waba_data.get("id", ""),
                "currency": waba_data.get("currency", ""),
                "phone_display": phone_info.get("display_phone_number", "") if phone_info else "",
                "phone_name": phone_info.get("verified_name", "") if phone_info else "",
                "phone_quality": phone_info.get("quality_rating", "") if phone_info else "",
            }

    except httpx.TimeoutException:
        return {"success": False, "error": "Timeout ao conectar com a Meta API."}
    except Exception as e:
        return {"success": False, "error": f"Erro de conexão: {str(e)}"}
