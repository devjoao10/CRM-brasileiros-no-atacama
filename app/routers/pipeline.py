import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.pipeline import Funnel, FunnelEntry, LeadHistory
from app.models.lead import Lead
from app.models.user import User
from app.schemas.pipeline import (
    FunnelCreate, FunnelUpdate, FunnelResponse, FunnelListResponse,
    FunnelEntryCreate, FunnelEntryMove, FunnelEntryTransfer, FunnelEntryResponse,
    LeadCardResponse, KanbanStageResponse, KanbanBoardResponse,
    HistoryResponse, HistoryListResponse,
)
from app.schemas.tag import TagResponse
from app.auth import get_current_user

router = APIRouter(prefix="/api/pipeline", tags=["Pipeline"])


# ─── Helper ──────────────────────────────────────

def _log_event(db: Session, lead_id: int, evento: str, descricao: str,
               funnel_id=None, etapa_origem=None, etapa_destino=None,
               funnel_origem_id=None, dados=None):
    """Log a history event for a lead."""
    entry = LeadHistory(
        lead_id=lead_id,
        evento=evento,
        descricao=descricao,
        funnel_id=funnel_id,
        etapa_origem=etapa_origem,
        etapa_destino=etapa_destino,
        funnel_origem_id=funnel_origem_id,
        dados=dados or {},
    )
    db.add(entry)
    return entry


def _get_stage_name(funnel: Funnel, stage_id: str) -> str:
    """Get stage display name from funnel stages list."""
    for s in funnel.etapas:
        if s.get("id") == stage_id:
            return s.get("nome", stage_id)
    return stage_id


# ─── Funnels CRUD ────────────────────────────────

@router.get("/funnels", response_model=FunnelListResponse, summary="Listar funis")
async def list_funnels(
    is_active: Optional[bool] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Lista todos os funis de vendas."""
    try:
        query = db.query(Funnel)
        if is_active is not None:
            query = query.filter(Funnel.is_active == is_active)
        funnels = query.order_by(Funnel.created_at).all()
        return FunnelListResponse(
            total=len(funnels),
            funnels=[FunnelResponse.model_validate(f) for f in funnels],
        )
    except Exception as e:
        logging.exception("Erro ao listar funis")
        raise HTTPException(status_code=500, detail="Erro interno do servidor")


@router.get("/funnels/{funnel_id}", response_model=FunnelResponse, summary="Detalhes de um funil")
async def get_funnel(
    funnel_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    funnel = db.query(Funnel).filter(Funnel.id == funnel_id).first()
    if not funnel:
        raise HTTPException(status_code=404, detail="Funil não encontrado")
    return FunnelResponse.model_validate(funnel)


@router.post("/funnels", response_model=FunnelResponse, status_code=201, summary="Criar funil")
async def create_funnel(
    data: FunnelCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Cria um novo funil de vendas com etapas personalizadas.

    **N8N**: Crie funis dinamicamente para diferentes campanhas.

    Exemplo de etapas:
    ```json
    [
        {"id": "novo", "nome": "Novo Lead"},
        {"id": "contato", "nome": "Em Contato"},
        {"id": "negociacao", "nome": "Negociação"},
        {"id": "fechado", "nome": "Fechado"}
    ]
    ```
    """
    existing = db.query(Funnel).filter(Funnel.nome == data.nome).first()
    if existing:
        raise HTTPException(status_code=409, detail="Já existe um funil com este nome")

    # Validate unique stage IDs
    stage_ids = [s.id for s in data.etapas]
    if len(stage_ids) != len(set(stage_ids)):
        raise HTTPException(status_code=400, detail="IDs de etapas devem ser únicos")

    funnel = Funnel(
        nome=data.nome,
        etapas=[s.model_dump() for s in data.etapas],
    )
    db.add(funnel)
    db.commit()
    db.refresh(funnel)
    return FunnelResponse.model_validate(funnel)


@router.put("/funnels/{funnel_id}", response_model=FunnelResponse, summary="Atualizar funil")
async def update_funnel(
    funnel_id: int,
    data: FunnelUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    funnel = db.query(Funnel).filter(Funnel.id == funnel_id).first()
    if not funnel:
        raise HTTPException(status_code=404, detail="Funil não encontrado")

    update_data = data.model_dump(exclude_unset=True)

    if "nome" in update_data and update_data["nome"] != funnel.nome:
        existing = db.query(Funnel).filter(Funnel.nome == update_data["nome"]).first()
        if existing:
            raise HTTPException(status_code=409, detail="Já existe um funil com este nome")

    if "etapas" in update_data:
        stage_ids = [s["id"] if isinstance(s, dict) else s.id for s in update_data["etapas"]]
        if len(stage_ids) != len(set(stage_ids)):
            raise HTTPException(status_code=400, detail="IDs de etapas devem ser únicos")
        update_data["etapas"] = [s if isinstance(s, dict) else s.model_dump() for s in update_data["etapas"]]

    for field, value in update_data.items():
        setattr(funnel, field, value)

    db.commit()
    db.refresh(funnel)
    return FunnelResponse.model_validate(funnel)


@router.delete("/funnels/{funnel_id}", summary="Excluir funil")
async def delete_funnel(
    funnel_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    funnel = db.query(Funnel).filter(Funnel.id == funnel_id).first()
    if not funnel:
        raise HTTPException(status_code=404, detail="Funil não encontrado")
    db.delete(funnel)
    db.commit()
    return {"message": f"Funil '{funnel.nome}' excluído"}


# ─── Kanban Board ────────────────────────────────

@router.get("/board/{funnel_id}", response_model=KanbanBoardResponse, summary="Kanban board de um funil")
async def get_kanban_board(
    funnel_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Retorna o board Kanban completo de um funil: etapas com seus leads.

    **N8N**: Use para monitorar o estado atual do pipeline e reagir a mudanças.
    """
    try:
        funnel = db.query(Funnel).filter(Funnel.id == funnel_id).first()
        if not funnel:
            raise HTTPException(status_code=404, detail="Funil não encontrado")

        entries = (
            db.query(FunnelEntry)
            .filter(FunnelEntry.funnel_id == funnel_id)
            .options(joinedload(FunnelEntry.lead))
            .order_by(FunnelEntry.posicao)
            .all()
        )

        # Group entries by stage
        stage_entries = {}
        for entry in entries:
            if entry.etapa_id not in stage_entries:
                stage_entries[entry.etapa_id] = []
            lead = entry.lead
            if lead and lead.is_active:
                stage_entries[entry.etapa_id].append(
                    LeadCardResponse(
                        entry_id=entry.id,
                        lead_id=lead.id,
                        nome=lead.nome,
                        email=lead.email,
                        whatsapp=lead.whatsapp,
                        destinos=lead.destinos,
                        data_chegada=lead.data_chegada,
                        data_partida=lead.data_partida,
                        etapa_id=entry.etapa_id,
                        posicao=entry.posicao,
                        tags=[TagResponse.model_validate(t) for t in lead.tags],
                        entry_created_at=entry.created_at,
                    )
                )

        stages = []
        total = 0
        for stage in funnel.etapas:
            leads_in_stage = stage_entries.get(stage["id"], [])
            total += len(leads_in_stage)
            stages.append(KanbanStageResponse(
                id=stage["id"],
                nome=stage["nome"],
                leads=leads_in_stage,
            ))

        return KanbanBoardResponse(
            funnel=FunnelResponse.model_validate(funnel),
            stages=stages,
            total_leads=total,
        )
    except Exception as e:
        logging.exception("Erro ao carregar board kanban")
        raise HTTPException(status_code=500, detail="Erro interno do servidor")


# ─── Lead Entries ────────────────────────────────

@router.post("/funnels/{funnel_id}/leads", response_model=FunnelEntryResponse,
             status_code=201, summary="Adicionar lead ao funil")
async def add_lead_to_funnel(
    funnel_id: int,
    data: FunnelEntryCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Adiciona um lead a uma etapa específica do funil.

    **N8N**: Adicione leads automaticamente ao pipeline quando captados.
    """
    funnel = db.query(Funnel).filter(Funnel.id == funnel_id).first()
    if not funnel:
        raise HTTPException(status_code=404, detail="Funil não encontrado")

    lead = db.query(Lead).filter(Lead.id == data.lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    # Validate stage exists
    stage_ids = [s["id"] for s in funnel.etapas]
    if data.etapa_id not in stage_ids:
        raise HTTPException(status_code=400, detail=f"Etapa '{data.etapa_id}' não existe neste funil")

    # Check if lead already in this funnel
    existing = db.query(FunnelEntry).filter(
        FunnelEntry.lead_id == data.lead_id,
        FunnelEntry.funnel_id == funnel_id,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Lead já está neste funil")

    max_pos = db.query(FunnelEntry).filter(
        FunnelEntry.funnel_id == funnel_id,
        FunnelEntry.etapa_id == data.etapa_id,
    ).count()

    entry = FunnelEntry(
        lead_id=data.lead_id,
        funnel_id=funnel_id,
        etapa_id=data.etapa_id,
        posicao=max_pos,
    )
    db.add(entry)

    stage_name = _get_stage_name(funnel, data.etapa_id)
    _log_event(db, data.lead_id, "entered_funnel",
               f"Entrou no funil '{funnel.nome}' na etapa '{stage_name}'",
               funnel_id=funnel_id, etapa_destino=data.etapa_id)

    db.commit()
    db.refresh(entry)
    return FunnelEntryResponse.model_validate(entry)


@router.put("/entries/{entry_id}/move", summary="Mover lead de etapa")
async def move_lead_stage(
    entry_id: int,
    data: FunnelEntryMove,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Move um lead para outra etapa dentro do mesmo funil.

    **N8N**: Automatize movimentações baseadas em eventos (ex: resposta no WhatsApp → mover para "Em contato").
    """
    entry = db.query(FunnelEntry).filter(FunnelEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry não encontrada")

    funnel = db.query(Funnel).filter(Funnel.id == entry.funnel_id).first()
    stage_ids = [s["id"] for s in funnel.etapas]
    if data.etapa_id not in stage_ids:
        raise HTTPException(status_code=400, detail=f"Etapa '{data.etapa_id}' não existe neste funil")

    old_stage = entry.etapa_id
    old_stage_name = _get_stage_name(funnel, old_stage)
    new_stage_name = _get_stage_name(funnel, data.etapa_id)

    entry.etapa_id = data.etapa_id
    if data.posicao is not None:
        entry.posicao = data.posicao
    else:
        max_pos = db.query(FunnelEntry).filter(
            FunnelEntry.funnel_id == entry.funnel_id,
            FunnelEntry.etapa_id == data.etapa_id,
        ).count()
        entry.posicao = max_pos

    if old_stage != data.etapa_id:
        _log_event(db, entry.lead_id, "stage_moved",
                   f"Movido de '{old_stage_name}' para '{new_stage_name}' no funil '{funnel.nome}'",
                   funnel_id=funnel.id, etapa_origem=old_stage, etapa_destino=data.etapa_id)

    db.commit()
    return {
        "message": f"Lead movido para '{new_stage_name}'",
        "entry_id": entry.id,
        "etapa_id": data.etapa_id,
    }


@router.post("/entries/{entry_id}/transfer", summary="Transferir lead entre funis")
async def transfer_lead(
    entry_id: int,
    data: FunnelEntryTransfer,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Transfere um lead de um funil para outro.

    **N8N**: Automatize transferências entre funis (ex: lead de Atacama → funil Uyuni).
    """
    entry = db.query(FunnelEntry).filter(FunnelEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry não encontrada")

    old_funnel = db.query(Funnel).filter(Funnel.id == entry.funnel_id).first()
    new_funnel = db.query(Funnel).filter(Funnel.id == data.destino_funnel_id).first()
    if not new_funnel:
        raise HTTPException(status_code=404, detail="Funil de destino não encontrado")

    stage_ids = [s["id"] for s in new_funnel.etapas]
    if data.destino_etapa_id not in stage_ids:
        raise HTTPException(status_code=400, detail=f"Etapa '{data.destino_etapa_id}' não existe no funil de destino")

    # Check not already in target funnel
    existing = db.query(FunnelEntry).filter(
        FunnelEntry.lead_id == entry.lead_id,
        FunnelEntry.funnel_id == data.destino_funnel_id,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Lead já está no funil de destino")

    old_funnel_id = entry.funnel_id
    old_stage_name = _get_stage_name(old_funnel, entry.etapa_id)
    new_stage_name = _get_stage_name(new_funnel, data.destino_etapa_id)

    # Log leaving
    _log_event(db, entry.lead_id, "left_funnel",
               f"Saiu do funil '{old_funnel.nome}' (etapa '{old_stage_name}')",
               funnel_id=old_funnel_id, etapa_origem=entry.etapa_id)

    # Update entry
    entry.funnel_id = data.destino_funnel_id
    entry.etapa_id = data.destino_etapa_id
    entry.posicao = db.query(FunnelEntry).filter(
        FunnelEntry.funnel_id == data.destino_funnel_id,
        FunnelEntry.etapa_id == data.destino_etapa_id,
    ).count()

    # Log transfer
    _log_event(db, entry.lead_id, "transferred",
               f"Transferido de '{old_funnel.nome}' para '{new_funnel.nome}' (etapa '{new_stage_name}')",
               funnel_id=data.destino_funnel_id, etapa_destino=data.destino_etapa_id,
               funnel_origem_id=old_funnel_id)

    db.commit()
    return {
        "message": f"Lead transferido para '{new_funnel.nome}' → '{new_stage_name}'",
        "entry_id": entry.id,
    }


@router.delete("/entries/{entry_id}", summary="Remover lead do funil")
async def remove_lead_from_funnel(
    entry_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    entry = db.query(FunnelEntry).filter(FunnelEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry não encontrada")

    funnel = db.query(Funnel).filter(Funnel.id == entry.funnel_id).first()
    stage_name = _get_stage_name(funnel, entry.etapa_id) if funnel else entry.etapa_id

    _log_event(db, entry.lead_id, "left_funnel",
               f"Removido do funil '{funnel.nome if funnel else 'desconhecido'}' (etapa '{stage_name}')",
               funnel_id=entry.funnel_id, etapa_origem=entry.etapa_id)

    db.delete(entry)
    db.commit()
    return {"message": "Lead removido do funil"}


# ─── History ─────────────────────────────────────

@router.get("/history/{lead_id}", response_model=HistoryListResponse, summary="Histórico de um lead")
async def get_lead_history(
    lead_id: int,
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Retorna o histórico completo de um lead: entradas em funis, movimentações, transferências.

    **N8N**: Monitore a jornada do lead para automações condicionais.
    """
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    history = (
        db.query(LeadHistory)
        .filter(LeadHistory.lead_id == lead_id)
        .order_by(LeadHistory.created_at.desc())
        .limit(limit)
        .all()
    )

    return HistoryListResponse(
        total=len(history),
        historico=[HistoryResponse.model_validate(h) for h in history],
    )


@router.post("/history/{lead_id}/note", summary="Adicionar nota ao histórico")
async def add_history_note(
    lead_id: int,
    descricao: str = Query(..., description="Texto da nota"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Adiciona uma nota manual ao histórico do lead.

    **N8N**: Registre ações de automações no histórico.
    """
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    event = _log_event(db, lead_id, "note", descricao)
    db.commit()
    db.refresh(event)
    return HistoryResponse.model_validate(event)
