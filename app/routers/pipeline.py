import logging
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import func, or_, tuple_, String
from sqlalchemy.orm import Session, joinedload, selectinload

from app.database import get_db, IS_SQLITE
from app.models.pipeline import Funnel, FunnelEntry, LeadHistory
from app.models.lead import Lead
from app.models.tag import lead_tags
from app.models.user import User
from app.schemas.pipeline import (
    FunnelCreate, FunnelUpdate, FunnelResponse, FunnelListResponse,
    FunnelEntryCreate, FunnelEntryMove, FunnelEntryTransfer, FunnelEntryResponse,
    LeadCardResponse, KanbanStageResponse, KanbanBoardResponse,
    HistoryResponse, HistoryListResponse,
    KanbanStageMeta, KanbanBoardMeta, StageCardsResponse, LeadLocationResponse,
)
from app.schemas.tag import TagResponse
from app.auth import get_current_user, require_admin

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


def _responsavel_nome(lead: Lead, db: Session, cache: dict) -> Optional[str]:
    """Nome do responsavel, com cache por request (evita query por card)."""
    if lead.responsavel_id is None:
        return "Agente IA"
    if lead.responsavel_id not in cache:
        user = db.query(User).filter(User.id == lead.responsavel_id).first()
        cache[lead.responsavel_id] = user.nome if user else None
    return cache[lead.responsavel_id]


def _card(entry: FunnelEntry, lead: Lead, resp_nome: Optional[str]) -> LeadCardResponse:
    """Monta o card. Unico lugar que define o payload — board antigo e board
    paginado usam este helper, entao os dois nao podem divergir."""
    return LeadCardResponse(
        entry_id=entry.id,
        lead_id=lead.id,
        nome=lead.nome,
        email=lead.email,
        whatsapp=lead.whatsapp,
        destinos=lead.destinos,
        data_chegada=lead.data_chegada,
        data_partida=lead.data_partida,
        num_viajantes=lead.num_viajantes,
        etapa_id=entry.etapa_id,
        posicao=entry.posicao,
        tags=[TagResponse.model_validate(t) for t in lead.tags],
        responsavel_id=lead.responsavel_id,
        responsavel_nome=resp_nome,
        entry_created_at=entry.created_at,
        entry_updated_at=entry.updated_at,
    )


def _get_stage_name(funnel: Funnel, stage_id: str) -> str:
    """Get stage display name from funnel stages list."""
    for s in funnel.etapas:
        if s.get("id") == stage_id:
            return s.get("nome", stage_id)
    return stage_id


# ─── Funnels CRUD ────────────────────────────────

@router.get("/funnels", response_model=FunnelListResponse, summary="Listar funis")
def list_funnels(
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
def get_funnel(
    funnel_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    funnel = db.query(Funnel).filter(Funnel.id == funnel_id).first()
    if not funnel:
        raise HTTPException(status_code=404, detail="Funil não encontrado")
    return FunnelResponse.model_validate(funnel)


@router.post("/funnels", response_model=FunnelResponse, status_code=201, summary="Criar funil")
def create_funnel(
    data: FunnelCreate,
    current_user: User = Depends(require_admin),
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
def update_funnel(
    funnel_id: int,
    data: FunnelUpdate,
    current_user: User = Depends(require_admin),
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
def delete_funnel(
    funnel_id: int,
    current_user: User = Depends(require_admin),
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
def get_kanban_board(
    funnel_id: int,
    responsavel_id: Optional[int] = Query(None, description="Filtrar por responsável (0 = Agente IA)"),
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

        query = (
            db.query(FunnelEntry)
            .filter(FunnelEntry.funnel_id == funnel_id)
            # LeadCardResponse le lead.tags: sem o selectinload, cada card
            # dispara uma query propria (3000 cards = 3000 queries).
            .options(joinedload(FunnelEntry.lead).selectinload(Lead.tags))
            .order_by(FunnelEntry.posicao)
        )

        entries = query.all()

        # Build user cache for responsavel names
        user_cache = {}

        # Group entries by stage
        stage_entries = {}
        for entry in entries:
            if entry.etapa_id not in stage_entries:
                stage_entries[entry.etapa_id] = []
            lead = entry.lead
            if lead and lead.is_active:
                # Apply responsavel filter
                if responsavel_id is not None:
                    if responsavel_id == 0 and lead.responsavel_id is not None:
                        continue
                    elif responsavel_id != 0 and lead.responsavel_id != responsavel_id:
                        continue

                stage_entries[entry.etapa_id].append(
                    _card(entry, lead, _responsavel_nome(lead, db, user_cache))
                )

        stages = []
        total = 0
        for stage in funnel.etapas:
            leads_in_stage = stage_entries.get(stage["id"], [])
            total += len(leads_in_stage)
            stages.append(KanbanStageResponse(
                id=stage["id"],
                nome=stage["nome"],
                dias_limite=stage.get("dias_limite", 7),
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


# ─── Board paginado por etapa (PERF-PIPE-01) ─────
# O endpoint /board/{id} acima continua intacto para compatibilidade. O caminho
# novo e: /meta desenha o esqueleto e cada coluna busca seus proprios cards.
#
# ORDENACAO: (updated_at DESC, id DESC).
#   - `posicao` NAO serve de chave: e sempre COUNT(etapa) na escrita, logo
#     REPETE quando entries saem da etapa. Ordenar por ela paginando duplicaria
#     e pularia cards.
#   - `updated_at` muda quando move_lead_stage troca a etapa, entao o lead
#     recem-movido aparece no topo — que e o requisito.
#   - `id DESC` e o desempate deterministico exigido pelo keyset.
# As ESCRITAS de `posicao` ficam exatamente como estavam: nada em add/move/
# transfer foi tocado. Isso e seguro porque o frontend nunca envia `posicao`
# (onDrop manda so `etapa_id`), ou seja, nao existe reordenacao manual hoje.

_PERIODOS = {"hoje": 1, "3d": 3, "7d": 7, "30d": 30}


def _json_list_contains(column, value: str):
    """Mesma expressao de leads.py/segments.py: @> com cast jsonb no PostgreSQL
    (a coluna e `json`), LIKE no SQLite."""
    if IS_SQLITE:
        return column.cast(String).ilike(f'%"{value}"%')
    import json
    from sqlalchemy.dialects.postgresql import JSONB
    return column.cast(JSONB).op("@>")(json.dumps([value]))


def _cursor_encode(entry: FunnelEntry) -> Optional[str]:
    """
    `updated_at` e nullable no DDL (server_default=now(), sem NOT NULL). Pela
    aplicacao nunca fica NULL — nenhum caminho de codigo o define e o default
    do banco sempre preenche. Mas uma linha inserida por SQL direto poderia
    ter NULL e derrubaria o .isoformat() aqui. Sem cursor, a coluna para de
    paginar em vez de estourar 500.
    """
    if entry.updated_at is None:
        return None
    return f"{entry.updated_at.isoformat()}|{entry.id}"


def _cursor_decode(cursor: str):
    """('<iso>|<id>') -> (datetime, int). Cursor invalido = primeira pagina."""
    try:
        ts, eid = cursor.split("|", 1)
        return datetime.fromisoformat(ts), int(eid)
    except (ValueError, AttributeError):
        return None


def _stage_query(db: Session, funnel_id: str, etapa_id: str, f: dict):
    """
    Query base de UMA etapa com TODOS os filtros aplicados em SQL.

    Cobre os mesmos criterios que a barra de filtros do Pipeline aplicava no
    JavaScript sobre o board inteiro. Com paginacao isso deixou de ser possivel:
    filtrar no cliente so veria os cards ja carregados.
    """
    query = (
        db.query(FunnelEntry)
        .join(Lead, Lead.id == FunnelEntry.lead_id)
        .filter(
            FunnelEntry.funnel_id == funnel_id,
            FunnelEntry.etapa_id == etapa_id,
            Lead.is_active == True,  # noqa: E712 — mesma regra do board antigo
        )
    )
    if f.get("q"):
        termo = f"%{f['q'].strip()}%"
        query = query.filter(or_(
            Lead.nome.ilike(termo), Lead.whatsapp.ilike(termo), Lead.email.ilike(termo)
        ))
    if f.get("periodo") in _PERIODOS:
        limite = datetime.now(timezone.utc) - timedelta(days=_PERIODOS[f["periodo"]])
        query = query.filter(FunnelEntry.updated_at >= limite)
    if f.get("responsavel_id") is not None:
        if f["responsavel_id"] == 0:
            query = query.filter(Lead.responsavel_id.is_(None))
        else:
            query = query.filter(Lead.responsavel_id == f["responsavel_id"])
    if f.get("destino"):
        query = query.filter(_json_list_contains(Lead.destinos, f["destino"]))
    if f.get("tag_ids"):
        sub = db.query(lead_tags.c.lead_id).filter(lead_tags.c.tag_id.in_(f["tag_ids"]))
        query = query.filter(Lead.id.in_(sub.subquery()))
    # AUDIT-2026-08-WC5 — "pelo menos X" e "exatamente X" sao perguntas
    # DIFERENTES, e a operacao precisa das duas.
    #
    # O filtro nasceu como minimo (a UI diz "pelo menos X viajantes" e dois
    # testes existentes afirmam esse contrato), mas a regra operacional que
    # motivou este ajuste pede quantidade EXATA: separar casal de familia de
    # viajante solo. Trocar a semantica de `viajantes_min` atenderia um dos
    # dois e quebraria o outro — inclusive quem ja salvou um filtro.
    # Por isso e um parametro NOVO, e os dois nunca chegam juntos (a rota
    # rejeita a combinacao com 422).
    if f.get("viajantes_exato") is not None:
        query = query.filter(Lead.num_viajantes == f["viajantes_exato"])
    elif f.get("viajantes_min") is not None:
        query = query.filter(Lead.num_viajantes >= f["viajantes_min"])
    if f.get("chegada_de"):
        query = query.filter(Lead.data_chegada >= f["chegada_de"])
    if f.get("chegada_ate"):
        query = query.filter(Lead.data_chegada <= f["chegada_ate"])
    return query


@router.get("/board/{funnel_id}/meta", response_model=KanbanBoardMeta,
            summary="Esqueleto do board (etapas + contagens, SEM cards)")
def get_board_meta(
    funnel_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Devolve funil, etapas e total por etapa — nenhum card.

    A contagem sai de UM GROUP BY, nao de um COUNT por etapa.
    """
    funnel = db.query(Funnel).filter(Funnel.id == funnel_id).first()
    if not funnel:
        raise HTTPException(status_code=404, detail="Funil não encontrado")

    contagens = dict(
        db.query(FunnelEntry.etapa_id, func.count(FunnelEntry.id))
        .join(Lead, Lead.id == FunnelEntry.lead_id)
        .filter(FunnelEntry.funnel_id == funnel_id, Lead.is_active == True)  # noqa: E712
        .group_by(FunnelEntry.etapa_id)
        .all()
    )

    stages = [
        KanbanStageMeta(
            id=s["id"],
            nome=s["nome"],
            dias_limite=s.get("dias_limite", 7),
            total=contagens.get(s["id"], 0),
        )
        for s in funnel.etapas
    ]
    return KanbanBoardMeta(
        funnel=FunnelResponse.model_validate(funnel),
        stages=stages,
        total_leads=sum(s.total for s in stages),
    )


@router.get("/board/{funnel_id}/stage/{etapa_id}", response_model=StageCardsResponse,
            summary="Cards de UMA etapa, paginados por cursor")
def get_stage_cards(
    funnel_id: int,
    etapa_id: str,
    limit: int = Query(30, ge=1, le=100),
    cursor: Optional[str] = Query(None, description="next_cursor da página anterior"),
    q: Optional[str] = Query(None, description="Busca em nome ou whatsapp"),
    periodo: Optional[str] = Query(None, description="hoje | 3d | 7d | 30d"),
    responsavel_id: Optional[int] = Query(None, description="0 = Agente IA"),
    destino: Optional[str] = Query(None),
    tag_ids: Optional[List[int]] = Query(None),
    viajantes_min: Optional[int] = Query(
        None, ge=1, description="Leads com PELO MENOS este numero de viajantes"),
    viajantes_exato: Optional[int] = Query(
        None, ge=1, description="Leads com EXATAMENTE este numero de viajantes"),
    chegada_de: Optional[date] = Query(None),
    chegada_ate: Optional[date] = Query(None),
    include_lead_id: Optional[int] = Query(
        None, description="Se este lead existir na etapa e ficar fora da página, vem em `target`"
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Uma página de cards da etapa. Nunca carrega a etapa inteira."""
    filtros = {
        "q": q, "periodo": periodo, "responsavel_id": responsavel_id,
        "destino": destino, "tag_ids": tag_ids, "viajantes_min": viajantes_min,
        "viajantes_exato": viajantes_exato,
        "chegada_de": chegada_de, "chegada_ate": chegada_ate,
    }
    if viajantes_min is not None and viajantes_exato is not None:
        raise HTTPException(
            status_code=422,
            detail=("Envie `viajantes_min` OU `viajantes_exato`, nunca os dois — "
                    "sao perguntas diferentes e a combinacao seria ambigua."),
        )
    base = _stage_query(db, funnel_id, etapa_id, filtros)
    total = base.count()

    pagina = base.options(
        joinedload(FunnelEntry.lead).selectinload(Lead.tags)
    ).order_by(FunnelEntry.updated_at.desc(), FunnelEntry.id.desc())

    if cursor:
        decodificado = _cursor_decode(cursor)
        if decodificado:
            ts, eid = decodificado
            pagina = pagina.filter(
                tuple_(FunnelEntry.updated_at, FunnelEntry.id) < (ts, eid)
            )

    # limit+1 para saber se ha proxima pagina sem um COUNT extra
    entries = pagina.limit(limit + 1).all()
    has_more = len(entries) > limit
    entries = entries[:limit]

    cache: dict = {}
    items = [_card(e, e.lead, _responsavel_nome(e.lead, db, cache)) for e in entries]

    # Deep-link: se o alvo nao caiu nesta pagina, busca SO ele (1 query),
    # sem carregar os cards que vem antes dele.
    target = None
    if include_lead_id and not any(i.lead_id == include_lead_id for i in items):
        alvo = (
            _stage_query(db, funnel_id, etapa_id, {})
            .filter(Lead.id == include_lead_id)
            .options(joinedload(FunnelEntry.lead).selectinload(Lead.tags))
            .first()
        )
        if alvo:
            target = _card(alvo, alvo.lead, _responsavel_nome(alvo.lead, db, cache))

    return StageCardsResponse(
        etapa_id=etapa_id,
        items=items,
        total=total,
        has_more=has_more,
        next_cursor=_cursor_encode(entries[-1]) if (has_more and entries) else None,
        target=target,
    )


@router.get("/locate/{lead_id}", response_model=LeadLocationResponse,
            summary="Em que funil/etapa um lead está (sem carregar o board)")
def locate_lead(
    lead_id: int,
    prefer_funnel_id: Optional[int] = Query(
        None, description="Funil aberto na tela — tem prioridade, como antes"
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Resolve funil + etapa + entry de um lead.

    Um lead PODE estar em varios funis (nao ha unique em (lead_id, funnel_id);
    o guard de 409 so impede repetir no MESMO funil).

    Regra DECIDIDA (nao e mais a inercia do frontend antigo, que so procurava
    dentro do funil aberto e nao fazia nada se o lead estivesse em outro):

      1. entry no `prefer_funnel_id`      -> esse funil vence
      2. sem entry la, mas ha em outro    -> localiza e abre o outro funil
      3. nenhum FunnelEntry               -> 404

    O passo 2 e o motivo de existir do deep-link Conversas -> Funil: achar o
    lead. O desempate do fallback e (created_at ASC, id ASC) — `created_at`
    sozinho nao basta, duas entries criadas no mesmo instante devolveriam
    resultado nao-deterministico entre chamadas.
    """
    base = db.query(FunnelEntry).filter(FunnelEntry.lead_id == lead_id)

    entry = None
    if prefer_funnel_id is not None:
        entry = (
            base.filter(FunnelEntry.funnel_id == prefer_funnel_id)
            .order_by(FunnelEntry.created_at, FunnelEntry.id)
            .first()
        )
    if entry is None:
        entry = base.order_by(FunnelEntry.created_at, FunnelEntry.id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Lead não está em nenhum funil")

    funnel = db.query(Funnel).filter(Funnel.id == entry.funnel_id).first()
    if not funnel:
        raise HTTPException(status_code=404, detail="Funil não encontrado")

    return LeadLocationResponse(
        lead_id=lead_id,
        funnel_id=funnel.id,
        funnel_nome=funnel.nome,
        etapa_id=entry.etapa_id,
        etapa_nome=_get_stage_name(funnel, entry.etapa_id),
        entry_id=entry.id,
    )


# ─── Lead Entries ────────────────────────────────

@router.post("/funnels/{funnel_id}/leads", response_model=FunnelEntryResponse,
             status_code=201, summary="Adicionar lead ao funil")
def add_lead_to_funnel(
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
def move_lead_stage(
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
def transfer_lead(
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
def remove_lead_from_funnel(
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
def get_lead_history(
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
def add_history_note(
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
