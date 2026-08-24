"""
Message Templates CRUD API + Meta Cloud API sync.
All template operations are real — when Meta API is configured,
templates are submitted for approval, synced, and deleted via Graph API.
"""

import json
import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_user, require_admin, User
from app.models.message_variable import MessageVariable
from app.models.template import MessageTemplate
from app.schemas.template import (
    TemplateCreate,
    TemplateUpdate,
    TemplateResponse,
    TemplateListResponse,
    TemplateSendRequest,
    TemplateParamMapUpdate,
    ServiceAvailabilityUpdate,
)
from app.services import whatsapp
from app.services import meta_templates
from app.services import variables as variables_service
from app.services.outbound import classify_wa_response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/templates", tags=["Templates"])


@router.post("", response_model=TemplateResponse, status_code=201)
async def create_template(
    data: TemplateCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Criar template local e submeter ao Meta para aprovação (se API configurada).
    Se a API não estiver configurada, salva apenas localmente com status PENDING.
    """
    # Verificar duplicata
    existing = db.query(MessageTemplate).filter(MessageTemplate.name == data.name).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Template '{data.name}' já existe")

    template = MessageTemplate(
        name=data.name,
        category=data.category,
        language=data.language,
        header_type=data.header_type,
        header_text=data.header_text,
        body_text=data.body_text,
        footer_text=data.footer_text,
        buttons_json=json.dumps([b.model_dump() for b in data.buttons]) if data.buttons else None,
        sample_values_json=json.dumps(data.sample_values) if data.sample_values else None,
        status="PENDING",
    )
    db.add(template)
    db.commit()
    db.refresh(template)

    # Submit to Meta if configured
    meta_result = await meta_templates.create_template_on_meta(template, db)
    if meta_result.get("success"):
        db.refresh(template)  # Refresh to get updated meta_template_id and status
        logger.info(f"Template '{template.name}' criado e submetido ao Meta (ID: {meta_result.get('meta_template_id')})")
    elif "não configurada" not in meta_result.get("error", ""):
        # If Meta is configured but submission failed, log the error but keep local
        logger.warning(f"Template '{template.name}' criado localmente, mas falhou no Meta: {meta_result.get('error')}")

    return TemplateResponse.from_orm_model(template)


@router.get("", response_model=TemplateListResponse)
async def list_templates(
    status: Optional[str] = Query(None, description="Filtrar por status: PENDING, APPROVED, REJECTED"),
    category: Optional[str] = Query(None, description="Filtrar por categoria: MARKETING, UTILITY, AUTHENTICATION"),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Listar todos os templates."""
    query = db.query(MessageTemplate)

    if status:
        query = query.filter(MessageTemplate.status == status)
    if category:
        query = query.filter(MessageTemplate.category == category)
    if search:
        term = f"%{search}%"
        query = query.filter(
            MessageTemplate.name.ilike(term) | MessageTemplate.body_text.ilike(term)
        )

    total = query.count()
    templates = query.order_by(MessageTemplate.created_at.desc()).all()

    return TemplateListResponse(
        templates=[TemplateResponse.from_orm_model(t) for t in templates],
        total=total,
    )


@router.get("/meta/approved")
async def list_meta_approved_templates(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    CONV-WINDOW-01 / CONV-CURATION-01 — catalogo do ATENDIMENTO.

    Fonte do seletor do composer: APPROVED na Meta **E** autorizado pelo admin.
    NAO le a tabela local de templates (status local so muda em sync manual e
    pode estar velho) e NAO escreve nada: nem na Meta, nem no banco.

    O filtro de curadoria e feito AQUI. Templates internos (alertas de lead,
    notificacoes de CRM, hello_world, testes) nunca chegam ao navegador do
    atendente — esconder no frontend nao seria esconder.

    Exige usuario autenticado.

    Rota declarada ANTES de `/{template_id}` de proposito — "meta" nao e um int e
    cairia no conversor de path da outra rota.

    503 quando a Meta esta indisponivel: o frontend precisa distinguir "nao ha
    templates" de "nao consegui carregar" — e no segundo caso NUNCA liberar texto.
    """
    result = await meta_templates.list_service_templates(db)
    if not result.get("ok"):
        raise HTTPException(status_code=503, detail=result.get("error", "Nao foi possivel carregar os templates."))
    return {"templates": result["templates"], "total": len(result["templates"])}


@router.get("/service-availability")
async def list_service_availability(
    refresh: bool = Query(False, description="Ignora o cache de 5 min e reconsulta a Meta"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """
    CONV-CURATION-01 — tela de curadoria (ADMIN).

    Todos os APPROVED da Meta com a marca `available` de cada um. Precisa listar
    os NAO autorizados tambem — sem isso nao haveria como autorizar o primeiro.
    E o unico lugar do sistema onde um template interno aparece, e so para admin.
    """
    result = await meta_templates.list_approved_templates(db, force=refresh)
    if not result.get("ok"):
        raise HTTPException(status_code=503, detail=result.get("error", "Nao foi possivel carregar os templates."))

    allowed = meta_templates.authorized_keys(db)
    # CONV-TPLMAP-01: esta e a tela onde o admin ve TODOS os templates, entao e
    # tambem onde o mapeamento persistido de um template feito no Business
    # Manager pode ser conferido e ajustado — sem mexer no BODY aprovado.
    maps = meta_templates.param_maps(db)
    items = [
        {
            **t,
            "available": (t["name"], t["language"]) in allowed,
            "param_map": {
                str(p): tok
                for p, tok in sorted(maps.get((t["name"], t["language"]), {}).items())
            },
        }
        for t in result["templates"]
    ]
    return {"templates": items, "total": len(items), "available": sum(1 for i in items if i["available"])}


@router.put("/service-availability")
async def update_service_availability(
    data: ServiceAvailabilityUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """
    CONV-CURATION-01 — liga/desliga um template no atendimento (ADMIN).

    Chave (name, language), idempotente nos dois sentidos. Efeito IMEDIATO: a
    autorizacao e lida do banco a cada requisicao, sem cache — revogar precisa
    valer agora, nao daqui a 5 minutos.
    """
    meta_templates.set_service_availability(db, data.name, data.language, data.available)
    logger.info(
        f"Template '{data.name}' ({data.language}) "
        f"{'liberado para' if data.available else 'removido do'} atendimento por {current_user.email}"
    )
    return {"name": data.name, "language": data.language, "available": data.available}


# ─── CONV-TPLMAP-01: mapeamento {{n}} -> @VARIAVEL ────────────────────

async def _body_params_of(db: Session, name: str, language: str) -> Optional[int]:
    """
    Aridade do BODY de (name, language), das DUAS fontes que o backend possui.

    O template local vem PRIMEIRO porque no modal "Novo Template" ele ainda esta
    PENDING e nem existe no catalogo aprovado da Meta — validar so contra a Meta
    tornaria impossivel mapear na criacao, que e exatamente quando o operador
    esta olhando para os `{{n}}` que acabou de escrever.

    Templates feitos no Business Manager nao tem linha local: para esses a
    fonte e o catalogo APROVADO (nao o do atendimento — autorizacao e outra
    decisao, e mapear antes de autorizar tem que ser possivel).

    None = nao conhecemos esse template em fonte nenhuma.
    """
    local = db.query(MessageTemplate).filter(
        MessageTemplate.name == name, MessageTemplate.language == language
    ).first()
    if local is not None:
        return meta_templates.count_body_params(local.body_text)

    catalog = await meta_templates.list_approved_templates(db)
    if catalog.get("ok"):
        for t in catalog["templates"]:
            if t["name"] == name and t["language"] == language:
                return t["body_params"]
    return None


@router.get("/param-map")
async def get_param_map(
    name: str = Query(..., min_length=1),
    language: str = Query(..., min_length=2),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Mapeamento persistido de um template. Leitura para qualquer autenticado —
    o composer precisa saber quais posicoes NAO pedir ao atendente.

    Declarada ANTES de `/{template_id}`: "param-map" nao e int e cairia no
    conversor de path da outra rota (mesmo motivo de `/meta/approved`).
    """
    mapping = meta_templates.param_maps(db).get((name, language), {})
    return {"name": name, "language": language, "mappings": {str(k): v for k, v in sorted(mapping.items())}}


@router.put("/param-map")
async def update_param_map(
    data: TemplateParamMapUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """
    Define o mapeamento COMPLETO de (name, language). Somente administradores —
    e configuracao de envio, mesmo nivel da curadoria.

    Valida, nesta ordem:
      1. template conhecido (local ou catalogo Meta)  -> senao 404
      2. posicao inteira dentro de 1..N do BODY REAL  -> senao 422
      3. token de variavel EXISTENTE e ATIVA          -> senao 422

    Sobre a regra de sequencia: `count_body_params` devolve o MAIOR indice e a
    Meta recebe o array completo ate ele, entao buracos ({{1}} e {{3}} sem
    {{2}}) sao tolerados pelo envio. Validar contiguidade aqui inventaria uma
    regra mais estrita que a do proprio envio — so a FAIXA e validada.

    NAO toca a Meta: mapeamento e local, o BODY aprovado continua exatamente
    como esta. Por isso editar mapeamento nao exige recriar o template.
    """
    expected = await _body_params_of(db, data.name, data.language)
    if expected is None:
        raise HTTPException(
            status_code=404,
            detail=f"Template '{data.name}' ({data.language}) nao encontrado.",
        )

    cleaned: dict = {}
    for raw_position, raw_token in (data.mappings or {}).items():
        try:
            position = int(raw_position)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail=f"Posicao invalida: {raw_position!r}.")
        if not 1 <= position <= expected:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Posicao {{{{{position}}}}} nao existe neste template "
                    f"(o corpo usa {expected} parametro(s))."
                    if expected else
                    f"Este template nao possui parametros {{{{n}}}} para mapear."
                ),
            )
        token = variables_service.normalize_token(raw_token)
        variable = db.query(MessageVariable).filter(MessageVariable.token == token).first()
        if variable is None:
            raise HTTPException(status_code=422, detail=f"{token} nao e uma variavel cadastrada.")
        if not variable.is_active:
            raise HTTPException(status_code=422, detail=f"A variavel {token} esta desativada.")
        cleaned[position] = token

    meta_templates.set_param_map(db, data.name, data.language, cleaned)
    logger.info(
        f"Mapeamento de '{data.name}' ({data.language}) definido por {current_user.email}: "
        f"{ {p: t for p, t in sorted(cleaned.items())} }"
    )
    return {
        "name": data.name,
        "language": data.language,
        "mappings": {str(k): v for k, v in sorted(cleaned.items())},
    }


@router.get("/{template_id}", response_model=TemplateResponse)
async def get_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Detalhe de um template."""
    template = db.query(MessageTemplate).filter(MessageTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template não encontrado")
    return TemplateResponse.from_orm_model(template)


@router.put("/{template_id}", response_model=TemplateResponse)
async def update_template(
    template_id: int,
    data: TemplateUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Editar template localmente.
    Templates já submetidos ao Meta precisam ser deletados e re-submetidos
    (Meta não permite edição de templates aprovados).
    """
    template = db.query(MessageTemplate).filter(MessageTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template não encontrado")

    if data.name is not None:
        existing = db.query(MessageTemplate).filter(
            MessageTemplate.name == data.name, MessageTemplate.id != template_id
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail=f"Template '{data.name}' já existe")
        template.name = data.name
    if data.category is not None:
        template.category = data.category
    if data.language is not None:
        template.language = data.language
    if data.header_type is not None:
        template.header_type = data.header_type
    if data.header_text is not None:
        template.header_text = data.header_text
    if data.body_text is not None:
        template.body_text = data.body_text
    if data.footer_text is not None:
        template.footer_text = data.footer_text
    if data.buttons is not None:
        template.buttons_json = json.dumps([b.model_dump() for b in data.buttons])
    if data.sample_values is not None:
        template.sample_values_json = json.dumps(data.sample_values)

    db.commit()
    db.refresh(template)
    return TemplateResponse.from_orm_model(template)


@router.delete("/{template_id}")
async def delete_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Deletar template local e do Meta (se sincronizado)."""
    template = db.query(MessageTemplate).filter(MessageTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template não encontrado")

    # Delete from Meta if it has a meta_template_id
    meta_deleted = False
    if template.meta_template_id:
        result = await meta_templates.delete_template_on_meta(template.name, db)
        meta_deleted = result.get("success", False)
        if not meta_deleted:
            logger.warning(f"Template '{template.name}' não pôde ser deletado do Meta: {result.get('error')}")

    name = template.name
    db.delete(template)
    db.commit()
    logger.info(f"Template deletado: {name} (Meta: {'sim' if meta_deleted else 'não'})")
    return {
        "message": f"Template '{name}' deletado",
        "meta_deleted": meta_deleted,
    }


@router.post("/sync")
async def sync_templates(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Sincronizar status dos templates com a Meta API.
    Busca todos os templates na conta do Meta e atualiza os status locais.
    """
    result = await meta_templates.sync_template_statuses(db)

    if result.get("success"):
        return {
            "message": f"Sincronização concluída: {result.get('synced', 0)} templates atualizados.",
            "synced": result.get("synced", 0),
            "total_meta": result.get("total_meta", 0),
            "details": result.get("details", []),
        }
    else:
        raise HTTPException(
            status_code=400,
            detail=result.get("error", "Erro na sincronização.")
        )


@router.post("/{template_id}/submit")
async def submit_template_to_meta(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Submeter (ou re-submeter) um template ao Meta para aprovação.
    Útil para templates que foram criados offline ou precisam ser re-submetidos.
    """
    template = db.query(MessageTemplate).filter(MessageTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template não encontrado")

    # If already on Meta, delete first (Meta doesn't allow editing)
    if template.meta_template_id:
        del_result = await meta_templates.delete_template_on_meta(template.name, db)
        if del_result.get("success"):
            template.meta_template_id = None
            template.status = "PENDING"
            db.commit()
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Não foi possível remover a versão anterior do Meta: {del_result.get('error')}"
            )

    result = await meta_templates.create_template_on_meta(template, db)

    if result.get("success"):
        db.refresh(template)
        return {
            "message": f"Template '{template.name}' submetido ao Meta para aprovação.",
            "meta_template_id": result.get("meta_template_id"),
            "status": result.get("status"),
        }
    else:
        raise HTTPException(
            status_code=400,
            detail=result.get("error", "Falha ao submeter template ao Meta.")
        )


@router.post("/{template_id}/send")
async def send_template(
    template_id: int,
    data: TemplateSendRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Enviar um template aprovado para um número de WhatsApp."""
    template = db.query(MessageTemplate).filter(MessageTemplate.id == template_id).first()
    if not template:
        raise HTTPException(status_code=404, detail="Template não encontrado")

    if template.status != "APPROVED":
        raise HTTPException(
            status_code=400,
            detail=f"Template '{template.name}' não está aprovado (status: {template.status}). "
                   "Apenas templates aprovados pelo Meta podem ser enviados."
        )

    # CONV-VAR-02: esta rota recebe um NUMERO (`data.to`), nao uma conversa —
    # nao existe cliente/atendente/protocolo para resolver. Ainda assim ela fala
    # com a Meta, entao o mesmo `render_strict` roda aqui com contexto VAZIO:
    # texto literal passa, variavel FIXA resolve (nao depende de conversa) e
    # qualquer variavel que precise da conversa BLOQUEIA em 422. O invariante
    # "a Meta nunca recebe @TOKEN" vale no sistema inteiro, nao so no composer.
    ctx = variables_service.VariableContext(db)

    def _resolve(values):
        try:
            return [variables_service.render_strict(db, str(v), ctx) for v in values]
        except variables_service.VariableResolutionError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"{exc} Envie pelo atendimento (/api/conversations/"
                       f"{{id}}/messages) para que a variavel seja preenchida.",
            )

    # Build components from variables
    components = []
    if data.variables:
        if "header" in data.variables:
            components.append({
                "type": "header",
                "parameters": [{"type": "text", "text": v} for v in _resolve(data.variables["header"])]
            })
        if "body" in data.variables:
            components.append({
                "type": "body",
                "parameters": [{"type": "text", "text": v} for v in _resolve(data.variables["body"])]
            })

    result = await whatsapp.send_template_message(
        to=data.to,
        template_name=template.name,
        language=template.language,
        components=components,
        db=db,
    )

    # CONV-WINDOW-01 (bug legado): `if result:` dava 200 em falha — o dict de erro
    # {"error": True, ...} devolvido por `send_template_message` e TRUTHY. Um envio
    # recusado pela Meta respondia "Template enviado". Classificacao real agora,
    # pelo mesmo contrato que todo o resto do outbound usa.
    r = classify_wa_response(result)
    if not r["ok"]:
        raise HTTPException(
            status_code=502,
            detail=f"Falha ao enviar template via WhatsApp API: {r['error_summary']}",
        )
    return {"message": f"Template '{template.name}' enviado para {data.to}", "response": result}
