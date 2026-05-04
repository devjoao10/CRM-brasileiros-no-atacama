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
from app.auth import get_current_user, User
from app.models.template import MessageTemplate
from app.schemas.template import (
    TemplateCreate,
    TemplateUpdate,
    TemplateResponse,
    TemplateListResponse,
    TemplateSendRequest,
)
from app.services import whatsapp
from app.services import meta_templates

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

    # Build components from variables
    components = []
    if data.variables:
        if "header" in data.variables:
            components.append({
                "type": "header",
                "parameters": [{"type": "text", "text": v} for v in data.variables["header"]]
            })
        if "body" in data.variables:
            components.append({
                "type": "body",
                "parameters": [{"type": "text", "text": v} for v in data.variables["body"]]
            })

    result = await whatsapp.send_template_message(
        to=data.to,
        template_name=template.name,
        language=template.language,
        components=components,
        db=db,
    )

    if result:
        return {"message": f"Template '{template.name}' enviado para {data.to}", "response": result}
    else:
        raise HTTPException(status_code=500, detail="Falha ao enviar template via WhatsApp API")
