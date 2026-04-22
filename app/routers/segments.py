from typing import Optional
from datetime import datetime, date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, extract, String
from app.database import IS_SQLITE

from app.database import get_db
from app.models.segment import Segment
from app.models.lead import Lead
from app.models.tag import lead_tags
from app.models.pipeline import FunnelEntry
from app.models.user import User
from app.schemas.segment import (
    SegmentCreate, SegmentUpdate, SegmentResponse,
    SegmentListResponse, SegmentFilters,
)
from app.schemas.lead import LeadResponse, LeadListResponse, LeadFunnelInfo
from app.models.pipeline import Funnel
from app.auth import get_current_user

router = APIRouter(prefix="/api/segments", tags=["Segmentação"])


def _json_list_contains(column, value: str):
    """Filtra coluna JSON list que contenha um valor. Compatível com SQLite e PostgreSQL."""
    if IS_SQLITE:
        return column.cast(String).ilike(f'%"{value}"%')
    else:
        import json
        return column.op("@>")(json.dumps([value]))


# ─── Helpers ─────────────────────────────────────

def _build_lead_response(lead: Lead, db: Session) -> LeadResponse:
    """Build LeadResponse with funnel info populated."""
    entries = (
        db.query(FunnelEntry)
        .filter(FunnelEntry.lead_id == lead.id)
        .options(joinedload(FunnelEntry.funnel))
        .all()
    )
    funis = []
    for entry in entries:
        if entry.funnel:
            etapa_nome = entry.etapa_id
            for s in entry.funnel.etapas:
                if s.get("id") == entry.etapa_id:
                    etapa_nome = s.get("nome", entry.etapa_id)
                    break
            funis.append(LeadFunnelInfo(
                funnel_id=entry.funnel.id,
                funnel_nome=entry.funnel.nome,
                etapa_id=entry.etapa_id,
                etapa_nome=etapa_nome,
                entry_id=entry.id,
            ))
    resp = LeadResponse.model_validate(lead)
    resp.funis = funis
    return resp


def _resolve_segment_query(filtros: dict, db: Session, for_count: bool = False):
    """
    Build a SQLAlchemy query from segment filter criteria.
    
    for_count=True: returns a lightweight query on Lead.id (no joins) for accurate counting.
    for_count=False: returns a full query with joinedload for fetching lead objects.
    """
    if for_count:
        query = db.query(Lead.id)
    else:
        query = db.query(Lead).options(joinedload(Lead.tags))

    search = filtros.get("search")
    destino = filtros.get("destino")
    is_active = filtros.get("is_active")
    data_chegada_de = filtros.get("data_chegada_de")
    data_chegada_ate = filtros.get("data_chegada_ate")
    data_partida_de = filtros.get("data_partida_de")
    data_partida_ate = filtros.get("data_partida_ate")
    ano_chegada = filtros.get("ano_chegada")
    mes_chegada = filtros.get("mes_chegada")
    ano_partida = filtros.get("ano_partida")
    mes_partida = filtros.get("mes_partida")
    tag_ids = filtros.get("tag_ids")
    tag_mode = filtros.get("tag_mode", "any")
    funnel_id = filtros.get("funnel_id")
    etapa_id = filtros.get("etapa_id")
    campo_chave = filtros.get("campo_chave")
    campo_valor = filtros.get("campo_valor")
    criado_de = filtros.get("criado_de")
    criado_ate = filtros.get("criado_ate")

    if search:
        f = f"%{search}%"
        query = query.filter(or_(
            Lead.nome.ilike(f), Lead.email.ilike(f), Lead.whatsapp.ilike(f),
        ))

    if destino:
        query = query.filter(_json_list_contains(Lead.destinos, destino))

    if is_active is not None:
        query = query.filter(Lead.is_active == is_active)

    if data_chegada_de:
        query = query.filter(Lead.data_chegada >= date.fromisoformat(data_chegada_de))
    if data_chegada_ate:
        query = query.filter(Lead.data_chegada <= date.fromisoformat(data_chegada_ate))
    if data_partida_de:
        query = query.filter(Lead.data_partida >= date.fromisoformat(data_partida_de))
    if data_partida_ate:
        query = query.filter(Lead.data_partida <= date.fromisoformat(data_partida_ate))

    if ano_chegada:
        query = query.filter(extract("year", Lead.data_chegada) == ano_chegada)
    if mes_chegada:
        query = query.filter(extract("month", Lead.data_chegada) == mes_chegada)
    if ano_partida:
        query = query.filter(extract("year", Lead.data_partida) == ano_partida)
    if mes_partida:
        query = query.filter(extract("month", Lead.data_partida) == mes_partida)

    if tag_ids:
        if tag_mode == "all":
            for tid in tag_ids:
                sub = db.query(lead_tags.c.lead_id).filter(lead_tags.c.tag_id == tid).subquery()
                query = query.filter(Lead.id.in_(sub))
        else:
            sub = db.query(lead_tags.c.lead_id).filter(lead_tags.c.tag_id.in_(tag_ids)).subquery()
            query = query.filter(Lead.id.in_(sub))

    if funnel_id:
        entry_sub = db.query(FunnelEntry.lead_id).filter(FunnelEntry.funnel_id == funnel_id)
        if etapa_id:
            entry_sub = entry_sub.filter(FunnelEntry.etapa_id == etapa_id)
        query = query.filter(Lead.id.in_(entry_sub.subquery()))

    if criado_de:
        query = query.filter(Lead.created_at >= datetime.combine(
            date.fromisoformat(criado_de), datetime.min.time()))
    if criado_ate:
        query = query.filter(Lead.created_at <= datetime.combine(
            date.fromisoformat(criado_ate), datetime.max.time()))

    # Custom field filtering (Python-side for SQLite compat)
    needs_python_filter = bool(campo_chave)

    return query, needs_python_filter, campo_chave, campo_valor


def _count_segment_leads(filtros: dict, db: Session) -> int:
    """Count how many leads match a segment's criteria (accurate, no duplicates)."""
    query, needs_python, chave, valor = _resolve_segment_query(filtros, db, for_count=True)
    if needs_python:
        # Need to fetch full Lead objects for Python-side filtering
        full_query = db.query(Lead).filter(Lead.id.in_(query.subquery()))
        all_leads = full_query.all()
        chave_lower = (chave or "").strip().lower()
        valor_lower = (valor or "").strip().lower()
        count = 0
        for lead in all_leads:
            cp = lead.campos_personalizados or {}
            match_key = next((k for k in cp if k.strip().lower() == chave_lower), None)
            if match_key is not None:
                if not valor_lower or valor_lower in str(cp[match_key]).lower():
                    count += 1
        return count
    return query.count()


# ─── CRUD ────────────────────────────────────────

@router.get("", response_model=SegmentListResponse, summary="Listar listas de segmentação")
async def list_segments(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Lista todas as listas de segmentação com contagem de leads em tempo real.

    **N8N**: Use para descobrir quais segmentos existem e quantos leads cada um tem.
    """
    segments = db.query(Segment).order_by(Segment.created_at.desc()).all()
    result = []
    for seg in segments:
        resp = SegmentResponse.model_validate(seg)
        resp.lead_count = _count_segment_leads(seg.filtros or {}, db)
        result.append(resp)

    return SegmentListResponse(total=len(result), segments=result)


@router.get("/{segment_id}", response_model=SegmentResponse, summary="Detalhes de um segmento")
async def get_segment(
    segment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    seg = db.query(Segment).filter(Segment.id == segment_id).first()
    if not seg:
        raise HTTPException(status_code=404, detail="Segmento não encontrado")
    resp = SegmentResponse.model_validate(seg)
    resp.lead_count = _count_segment_leads(seg.filtros or {}, db)
    return resp


@router.post("", response_model=SegmentResponse, status_code=201, summary="Criar lista de segmentação")
async def create_segment(
    data: SegmentCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Cria uma nova lista de segmentação com critérios de filtro.

    **N8N**: Crie segmentos dinamicamente para campanhas específicas.

    Exemplo de filtros:
    ```json
    {
      "destino": "Atacama",
      "tag_ids": [1, 3],
      "tag_mode": "any",
      "ano_chegada": 2026,
      "is_active": true
    }
    ```
    """
    existing = db.query(Segment).filter(Segment.nome == data.nome).first()
    if existing:
        raise HTTPException(status_code=409, detail="Já existe um segmento com este nome")

    seg = Segment(
        nome=data.nome,
        descricao=data.descricao,
        cor=data.cor,
        filtros=data.filtros.model_dump(exclude_none=True) if data.filtros else {},
    )
    db.add(seg)
    db.commit()
    db.refresh(seg)

    resp = SegmentResponse.model_validate(seg)
    resp.lead_count = _count_segment_leads(seg.filtros or {}, db)
    return resp


@router.put("/{segment_id}", response_model=SegmentResponse, summary="Atualizar segmento")
async def update_segment(
    segment_id: int,
    data: SegmentUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    seg = db.query(Segment).filter(Segment.id == segment_id).first()
    if not seg:
        raise HTTPException(status_code=404, detail="Segmento não encontrado")

    update_data = data.model_dump(exclude_unset=True)

    if "nome" in update_data and update_data["nome"] != seg.nome:
        existing = db.query(Segment).filter(Segment.nome == update_data["nome"]).first()
        if existing:
            raise HTTPException(status_code=409, detail="Já existe um segmento com este nome")

    if "filtros" in update_data and update_data["filtros"] is not None:
        update_data["filtros"] = {k: v for k, v in update_data["filtros"].items() if v is not None}

    for field, value in update_data.items():
        setattr(seg, field, value)

    db.commit()
    db.refresh(seg)

    resp = SegmentResponse.model_validate(seg)
    resp.lead_count = _count_segment_leads(seg.filtros or {}, db)
    return resp


@router.delete("/{segment_id}", summary="Excluir segmento")
async def delete_segment(
    segment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    seg = db.query(Segment).filter(Segment.id == segment_id).first()
    if not seg:
        raise HTTPException(status_code=404, detail="Segmento não encontrado")
    db.delete(seg)
    db.commit()
    return {"message": f"Segmento '{seg.nome}' excluído"}


# ─── Resolve: get leads in a segment ─────────────

@router.get("/{segment_id}/leads", response_model=LeadListResponse,
            summary="Leads de um segmento")
async def get_segment_leads(
    segment_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Retorna os leads que pertencem ao segmento (resolvido em tempo real).

    **N8N**: Use para obter a lista de leads de um segmento antes de enviar mensagens.
    """
    seg = db.query(Segment).filter(Segment.id == segment_id).first()
    if not seg:
        raise HTTPException(status_code=404, detail="Segmento não encontrado")

    filtros = seg.filtros or {}

    # Count accurately (no join duplicates)
    count_q, needs_python, chave, valor = _resolve_segment_query(filtros, db, for_count=True)

    if needs_python:
        full_query = db.query(Lead).filter(Lead.id.in_(count_q.subquery()))
        all_leads = full_query.order_by(Lead.created_at.desc()).all()
        chave_lower = (chave or "").strip().lower()
        valor_lower = (valor or "").strip().lower()
        filtered = []
        for lead in all_leads:
            cp = lead.campos_personalizados or {}
            match_key = next((k for k in cp if k.strip().lower() == chave_lower), None)
            if match_key is not None:
                if not valor_lower or valor_lower in str(cp[match_key]).lower():
                    filtered.append(lead)
        total = len(filtered)
        leads = filtered[skip: skip + limit]
    else:
        total = count_q.count()
        # Fetch with tags loaded, deduplicate
        fetch_q, _, _, _ = _resolve_segment_query(filtros, db, for_count=False)
        all_results = fetch_q.order_by(Lead.created_at.desc()).all()
        # Deduplicate (joinedload can produce duplicates)
        seen = set()
        unique_leads = []
        for lead in all_results:
            if lead.id not in seen:
                seen.add(lead.id)
                unique_leads.append(lead)
        leads = unique_leads[skip: skip + limit]

    return LeadListResponse(
        total=total, skip=skip, limit=limit,
        leads=[_build_lead_response(l, db) for l in leads],
    )


# ─── Preview: resolve filters without saving ─────

@router.post("/preview", response_model=LeadListResponse, summary="Preview de filtros")
async def preview_segment(
    filtros: SegmentFilters,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Executa filtros em tempo real sem salvar — útil para preview antes de criar o segmento.
    """
    filtros_dict = filtros.model_dump(exclude_none=True)

    # Count accurately (no join duplicates)
    count_q, needs_python, chave, valor = _resolve_segment_query(filtros_dict, db, for_count=True)

    if needs_python:
        full_query = db.query(Lead).filter(Lead.id.in_(count_q.subquery()))
        all_leads = full_query.order_by(Lead.created_at.desc()).all()
        chave_lower = (chave or "").strip().lower()
        valor_lower = (valor or "").strip().lower()
        filtered = []
        for lead in all_leads:
            cp = lead.campos_personalizados or {}
            match_key = next((k for k in cp if k.strip().lower() == chave_lower), None)
            if match_key is not None:
                if not valor_lower or valor_lower in str(cp[match_key]).lower():
                    filtered.append(lead)
        total = len(filtered)
        leads = filtered[skip: skip + limit]
    else:
        total = count_q.count()
        # Fetch with tags loaded, deduplicate
        fetch_q, _, _, _ = _resolve_segment_query(filtros_dict, db, for_count=False)
        all_results = fetch_q.order_by(Lead.created_at.desc()).all()
        seen = set()
        unique_leads = []
        for lead in all_results:
            if lead.id not in seen:
                seen.add(lead.id)
                unique_leads.append(lead)
        leads = unique_leads[skip: skip + limit]

    return LeadListResponse(
        total=total, skip=skip, limit=limit,
        leads=[_build_lead_response(l, db) for l in leads],
    )

