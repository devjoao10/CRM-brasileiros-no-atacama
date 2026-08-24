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
import re
import time
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.models.template import MessageTemplate, ServiceTemplate, TemplateParamMap
from app.models.api_config import ApiConfig
from app.services import whatsapp
from app.services import variables as variables_service

logger = logging.getLogger(__name__)

# CONV-WINDOW-01: identidade de template na Meta e (name, language) — NUNCA name
# sozinho. A mesma conta pode ter o mesmo nome em varios idiomas, e enviar o
# idioma errado e um envio errado (a Meta aceita e o cliente recebe em ingles).
_PARAM_RE = re.compile(r"\{\{(\d+)\}\}")

# Cache EM MEMORIA do catalogo aprovado. Existe porque abrir o seletor de
# templates numa conversa expirada faria um round-trip a Graph API a cada clique.
# Curto de proposito: um template revogado/pausado na Meta some do seletor em no
# maximo 5 min. NAO e tabela, NAO persiste, morre com o processo — a fonte de
# verdade continua sendo a Meta.
_CATALOG_TTL_SECONDS = 300
_catalog_cache: Optional[dict] = None
_catalog_cached_at: float = 0.0


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


async def _fetch_meta_templates(base_url: str, waba_id: str, headers: dict) -> list:
    """
    GET /{WABA_ID}/message_templates, seguindo `paging.next` ate o fim.
    SOMENTE LEITURA — nunca escreve na Meta nem no banco.

    Extraido de `sync_template_statuses` para ser compartilhado com o catalogo
    read-only: o dropdown do composer NAO pode disparar um sync (que escreveria
    status no banco) so para listar o que enviar.

    Levanta httpx.HTTPStatusError em resposta != 200 (os chamadores traduzem).
    """
    url = f"{base_url}/{waba_id}/message_templates"
    params: dict = {
        "fields": "name,language,status,category,components",
        "limit": 250,
    }
    collected: list = []

    async with httpx.AsyncClient(timeout=30.0) as client:
        next_url = url
        while next_url:
            response = await client.get(next_url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            collected.extend(data.get("data", []))
            next_url = data.get("paging", {}).get("next")
            params = {}  # a URL de `next` ja carrega os parametros

    return collected


def count_body_params(text: str) -> int:
    """
    Aridade de um corpo de template: quantos {{n}} POSICIONAIS distintos ele exige.

    Conta indices distintos (nao ocorrencias): `alerta_lead` repete {{1}} em duas
    frases e continua exigindo um unico valor. Se os indices nao forem exatamente
    1..N contiguos, devolve o MAIOR indice — a Meta exige o array completo ate ele,
    e pedir a mais e seguro; pedir a menos monta payload invalido.
    """
    if not text:
        return 0
    idx = {int(m) for m in _PARAM_RE.findall(text)}
    return max(idx) if idx else 0


def _describe_template(raw: dict) -> dict:
    """
    Traduz um template cru da Graph API para o contrato do frontend, e decide se
    ESTE pacote sabe envia-lo.

    Capacidades implementadas (definidas pelo inventario real da WABA — 34/34
    APPROVED, nenhum header de midia, nenhum botao, nenhum parametro de header):
      - BODY textual, com ou sem {{n}} posicionais
      - HEADER TEXT ESTATICO (sem {{n}})
      - FOOTER (estatico, sem parametros por especificacao da Meta)

    Fora disso o template e listado como INDISPONIVEL com motivo visivel. Nunca
    escondido em silencio, e nunca enviado com payload adivinhado.
    """
    components = raw.get("components") or []
    header = next((c for c in components if c.get("type") == "HEADER"), None)
    body = next((c for c in components if c.get("type") == "BODY"), None)
    footer = next((c for c in components if c.get("type") == "FOOTER"), None)
    buttons = next((c for c in components if c.get("type") == "BUTTONS"), None)

    body_text = (body or {}).get("text") or ""
    header_text = (header or {}).get("text") or None
    header_format = (header or {}).get("format")

    unsupported = None
    if not body:
        unsupported = "sem componente BODY"
    elif header and header_format != "TEXT":
        unsupported = f"header de midia ({header_format}) ainda nao suportado"
    elif header_text and count_body_params(header_text) > 0:
        unsupported = "header com parametros ainda nao suportado"
    elif buttons:
        unsupported = "botoes ainda nao suportados"

    # CONV-TPLMAP-01 — template ANTIGO que escreveu `@PRIMEIRONOMECLIENTE` como
    # TEXTO no BODY: body_params=0, entao a Meta manda a arroba literal ao
    # cliente e nao ha `{{n}}` onde encaixar um mapeamento. So DETECTA (a UI
    # avisa); corrigir exige recriar o template na Meta com {{1}}, {{2}}...,
    # que e decisao humana — nada aqui altera template existente.
    static_tokens = variables_service.find_tokens(body_text) if body_text else []

    return {
        "name": raw.get("name") or "",
        "language": raw.get("language") or "",
        "category": raw.get("category") or "",
        "status": raw.get("status") or "",
        "header_text": header_text,
        "body_text": body_text,
        "footer_text": (footer or {}).get("text") or None,
        "body_params": count_body_params(body_text),
        "supported": unsupported is None,
        "unsupported_reason": unsupported,
        "static_tokens": static_tokens,
    }


def invalidate_catalog_cache() -> None:
    """Usado pelos testes e por qualquer mudanca de credencial."""
    global _catalog_cache, _catalog_cached_at
    _catalog_cache = None
    _catalog_cached_at = 0.0


async def list_approved_templates(db: Session, *, force: bool = False) -> dict:
    """
    Catalogo READ-ONLY dos templates APPROVED da conta, direto da Meta.

    NAO consulta a tabela local `message_templates`: o status local so muda em
    sync manual e pode estar desatualizado — um template revogado continuaria
    "APPROVED" no banco e o envio falharia na cara do operador.

    Retorna {"ok": True, "templates": [...]} ou {"ok": False, "error": <seguro>}.
    O erro NUNCA vaza token, header ou URL com credencial.
    """
    global _catalog_cache, _catalog_cached_at

    if not force and _catalog_cache is not None:
        if time.monotonic() - _catalog_cached_at < _CATALOG_TTL_SECONDS:
            return _catalog_cache

    creds = whatsapp.get_waba_credentials(db)
    if creds is None:
        return {
            "ok": False,
            "error": "Meta API nao configurada. Configure Access Token e WABA ID em Configuracoes > API WhatsApp.",
        }
    token, waba_id, base_url = creds

    try:
        raw = await _fetch_meta_templates(base_url, waba_id, {"Authorization": f"Bearer {token}"})
    except httpx.HTTPStatusError as e:
        summary = f"HTTP {e.response.status_code}"
        try:
            err = e.response.json().get("error", {})
            if err.get("message"):
                summary += f": {err['message']}"
        except Exception:
            pass
        logger.error(f"Falha ao listar templates na Meta: {summary}")
        return {"ok": False, "error": f"Nao foi possivel carregar os templates ({summary})."}
    except httpx.TimeoutException:
        return {"ok": False, "error": "Tempo esgotado ao consultar a Meta. Tente novamente."}
    except Exception as e:
        logger.error(f"Erro inesperado ao listar templates: {type(e).__name__}")
        return {"ok": False, "error": "Nao foi possivel carregar os templates. Tente novamente."}

    templates = [
        _describe_template(t) for t in raw if (t.get("status") or "").upper() == "APPROVED"
    ]
    templates.sort(key=lambda t: (t["name"], t["language"]))

    result = {"ok": True, "templates": templates}
    _catalog_cache = result
    _catalog_cached_at = time.monotonic()
    logger.info(f"Catalogo de templates atualizado: {len(templates)} APPROVED de {len(raw)} totais.")
    return result


def authorized_keys(db: Session) -> set:
    """
    CONV-CURATION-01 — pares (name, language) que o CRM autoriza no atendimento.

    Lido do banco a CADA chamada, DE PROPOSITO: o cache de 5 min e dos dados da
    META (name/status/components, que mudam raramente e so no Business Manager).
    A autorizacao e decisao do admin daqui e precisa valer na hora — uma
    revogacao nao pode continuar em vigor por ate 5 minutos.

    Uma unica query por requisicao (a tabela tem no maximo algumas dezenas de
    linhas), nunca uma por template.
    """
    return {
        (name, language)
        for name, language in db.query(ServiceTemplate.name, ServiceTemplate.language).all()
    }


def set_service_availability(db: Session, name: str, language: str, available: bool) -> bool:
    """
    Liga/desliga um template no atendimento. Idempotente nos dois sentidos.
    Retorna o estado final. NAO valida contra a Meta: quem valida e a leitura
    (o catalogo cruza com APPROVED), entao autorizar algo inexistente e inocuo.
    """
    row = (
        db.query(ServiceTemplate)
        .filter(ServiceTemplate.name == name, ServiceTemplate.language == language)
        .first()
    )
    if available and row is None:
        db.add(ServiceTemplate(name=name, language=language))
    elif not available and row is not None:
        db.delete(row)
    db.commit()
    return available


def param_maps(db: Session) -> dict:
    """
    CONV-TPLMAP-01 — mapeamentos {{n}} -> @TOKEN, indexados por (name, language).

    Devolve `{(name, language): {posicao: token}}`. UMA query para o catalogo
    inteiro, nunca uma por template — a tabela tem, no maximo, algumas dezenas
    de linhas (uma por posicao mapeada), exatamente como `authorized_keys` aqui
    do lado, que ja le a sua inteira a cada requisicao.

    Lido do banco a CADA chamada, pelo mesmo motivo de `authorized_keys`: o
    cache de 5 min e dos dados da META. Mapeamento e configuracao DAQUI e
    precisa valer na hora — trocar {{1}} de variavel nao pode continuar
    enviando a antiga por ate 5 minutos.
    """
    out: dict = {}
    for row in db.query(TemplateParamMap).all():
        out.setdefault((row.name, row.language), {})[row.position] = row.token
    return out


def set_param_map(db: Session, name: str, language: str, mapping: dict) -> None:
    """
    Substitui TODO o mapeamento de (name, language) — posicao ausente e posicao
    sem mapeamento. Replace-all e idempotente e nao deixa orfao para tras: se
    fosse merge, remover {{2}} do BODY exigiria uma chamada de exclusao propria
    que alguem esqueceria de fazer.

    NAO valida (posicao existente, token cadastrado): quem valida e o router,
    que tem o catalogo da Meta em maos. Aqui e so escrita.
    """
    db.query(TemplateParamMap).filter(
        TemplateParamMap.name == name,
        TemplateParamMap.language == language,
    ).delete(synchronize_session=False)
    for position, token in sorted(mapping.items()):
        db.add(TemplateParamMap(
            name=name, language=language, position=int(position), token=token,
        ))
    db.commit()


async def list_service_templates(db: Session) -> dict:
    """
    Catalogo do ATENDIMENTO: APPROVED na Meta **E** autorizado localmente.

    As duas condicoes sao obrigatorias e nenhuma sobrepoe a outra: a Meta manda
    no status (autorizar localmente nunca torna um PENDING enviavel) e o CRM
    manda na finalidade (aprovado nao significa que serve para falar com cliente).

    O filtro e do BACKEND. O frontend nunca recebe template interno para depois
    esconder — se recebesse, um `view-source` ja seria a lista completa.
    """
    catalog = await list_approved_templates(db)
    if not catalog.get("ok"):
        return catalog
    allowed = authorized_keys(db)
    visible = [t for t in catalog["templates"] if (t["name"], t["language"]) in allowed]

    # CONV-TPLMAP-01: o mapeamento entra AQUI e nao em `_describe_template`
    # porque aquele resultado fica no cache de 5 min da Meta. Mapeamento e
    # configuracao local e precisa valer imediatamente. Copia rasa: o dict
    # cacheado nunca e mutado.
    maps = param_maps(db)
    return {
        "ok": True,
        "templates": [
            {**t, "param_map": maps.get((t["name"], t["language"]), {})}
            for t in visible
        ],
    }


async def find_service_template(db: Session, name: str, language: str) -> Optional[dict]:
    """
    Busca por (name, language) — a chave real — no MESMO catalogo que o seletor
    enxerga. Ler daqui, e nao repetir o cruzamento APPROVED x autorizado, e o que
    garante que o envio nunca aceite algo que o seletor nao ofereceria.

    None quando qualquer condicao falha, e por isso vale como validacao de envio:
    nao aprovado ou nao autorizado simplesmente nao e encontrado.
    """
    catalog = await list_service_templates(db)
    if not catalog.get("ok"):
        return None
    return next(
        (t for t in catalog["templates"] if t["name"] == name and t["language"] == language),
        None,
    )


def render_template_body(body_text: str, params: list) -> str:
    """
    Substitui {{n}} pelos valores informados, para o HISTORICO refletir o que o
    cliente REALMENTE recebeu ("Ola Joao", nao "Ola {{1}}").

    Nao e a renderizacao oficial — quem renderiza de verdade e a Meta. E a melhor
    aproximacao possivel sem coluna nova, e por isso e usada apenas em
    `Message.content`, jamais no payload enviado.
    """
    def sub(match):
        i = int(match.group(1)) - 1
        return str(params[i]) if 0 <= i < len(params) else match.group(0)

    return _PARAM_RE.sub(sub, body_text or "")


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

    try:
        # CONV-WINDOW-01: paginacao compartilhada com o catalogo read-only —
        # uma unica implementacao de "listar templates da WABA".
        all_meta_templates = await _fetch_meta_templates(
            _build_base_url(config), config.meta_waba_id, _build_headers(config)
        )

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

    except httpx.HTTPStatusError as e:
        # `_fetch_meta_templates` levanta em != 200; traduz aqui para a MESMA
        # mensagem de antes da extracao (e sem deixar `str(e)` vazar a URL).
        error_msg = f"HTTP {e.response.status_code}"
        try:
            error_msg = e.response.json().get("error", {}).get("message", error_msg)
        except Exception:
            pass
        return {"success": False, "error": f"Meta API: {error_msg}"}
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
