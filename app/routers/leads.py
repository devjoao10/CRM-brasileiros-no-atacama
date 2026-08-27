import base64
import io
import logging
import csv
import os
from typing import Optional, List
from datetime import datetime, date, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status, Query, UploadFile, File
from app.config import MAX_UPLOAD_SIZE_BYTES
from app.database import IS_SQLITE
from sqlalchemy.orm import Session, joinedload, selectinload
from sqlalchemy import or_, extract, and_, String, func, tuple_

from app.database import get_db
from app.models.lead import Lead
from app.models.tag import Tag, lead_tags
from app.models.pipeline import Funnel, FunnelEntry
from app.models.user import User
from app.schemas.lead import (
    LeadCreate,
    LeadUpdate,
    LeadResponse,
    LeadListResponse,
    LeadFunnelInfo,
    ImportResponse,
    DESTINOS_PRINCIPAIS,
)
from app.auth import get_current_user, require_admin
from app.query_filters import campo_personalizado_match, destino_match
from app.config import LEAD_TAG_ORIGEM_API
from app.services.lead_creation import criar_lead, resolver_funil_por_nome

router = APIRouter(prefix="/api/leads", tags=["Leads"])

logger = logging.getLogger(__name__)


# ─── Paginacao keyset ────────────────────────────────────────────────
# OFFSET fica caro em profundidade e, sem desempate, a ordem entre
# created_at iguais e INDEFINIDA: o banco pode devolver os empatados em
# ordens diferentes entre duas chamadas, e ai a mesma linha aparece em duas
# paginas ou em nenhuma. O cursor fixa o par (created_at, id) — id e unico,
# entao a ordem total e deterministica.
#
# pipeline.py tem um par equivalente para (updated_at, id). Nao unifiquei:
# mexer em pipeline.py esta fora do escopo deste pacote.

def _cursor_encode(lead: "Lead") -> str:
    return base64.urlsafe_b64encode(
        f"{lead.created_at.isoformat()}|{lead.id}".encode()).decode()


def _cursor_decode(cursor: str):
    """Cursor corrompido/forjado vira 400, nunca 500 nem pagina silenciosa."""
    try:
        bruto = base64.urlsafe_b64decode(cursor.encode()).decode()
        ts, sep, lid = bruto.rpartition("|")
        if not sep:
            raise ValueError("sem separador")
        return datetime.fromisoformat(ts), int(lid)
    except Exception:
        raise HTTPException(status_code=400, detail="Cursor inválido")


def _keyset_filtro(cursor: str):
    ts, lid = _cursor_decode(cursor)
    # (a, b) < (x, y) e exatamente `a < x OR (a = x AND b < y)`, mas escrito
    # como row-value o banco consegue virar SEEK no indice. Medido em 19.000
    # leads: com OR o plano e SCAN do indice inteiro (4,1 ms); com row-value e
    # SEARCH usando ix_leads_created_at (0,2 ms). Mesma forma que pipeline.py.
    return tuple_(Lead.created_at, Lead.id) < (ts, lid)


def _tem_telefone():
    """
    "Com telefone" nao e so NOT NULL: a base tem "" e "   " vindos de import.
    whatsapp e o unico campo de telefone do modelo Lead.
    (trim() remove espacos; tabulacao em telefone nao e caso real aqui.)
    """
    return and_(Lead.whatsapp.isnot(None), func.trim(Lead.whatsapp) != "")


def _only_digits(value: Optional[str]) -> str:
    """
    Normalizacao unica de telefone: so digitos (+, espacos, (), - e . somem).

    AUDIT-2026-08-WC (C6): mesmo helper de
    conversas/app/services/crm.py::_only_digits, definido de novo aqui em vez
    de importado — os dois pacotes se chamam `app` e nao podem coexistir no
    mesmo processo.
    """
    return "".join(ch for ch in (value or "") if ch.isdigit())


def _digitos_no_banco(coluna):
    """
    AUDIT-2026-08-WF2: o mesmo `_only_digits`, so que do LADO DO BANCO.

    Sem isto, qualquer filtro sobre `leads.whatsapp` compara um numero ja
    normalizado contra a coluna CRUA — e a coluna guarda o que o formulario do
    site grava (`+55 11 98765-4322`, com `+`, espaco e hifen). Comparar os dois
    lados normalizados e o unico jeito de o pre-filtro devolver TODOS os leads
    com aquele numero, em qualquer formato.

    PostgreSQL: `regexp_replace(whatsapp, '[^0-9]', '', 'g')` — DE PROPOSITO a
    mesma expressao de conversas/app/services/crm.py::lookup_lead_by_whatsapp,
    para que um unico indice de expressao (migration, fora deste arquivo) sirva
    as duas rotas.

    SQLite (dev e a suite): `regexp_replace` nao existe e nao da para registrar
    funcao aqui, entao a normalizacao e uma cadeia de `replace()` sobre os
    simbolos que o `_only_digits` remove. E menos abrangente que o regex de
    proposito: quem DECIDE identidade continua sendo o `_only_digits` em
    Python, sobre os candidatos; este SQL e so pre-filtro.
    """
    if IS_SQLITE:
        expr = coluna
        for simbolo in ("+", " ", "(", ")", "-", "."):
            expr = func.replace(expr, simbolo, "", type_=String)
        return expr
    return func.regexp_replace(coluna, "[^0-9]", "", "g", type_=String)


def _json_list_contains(column, value: str):
    """Filtra coluna JSON list que contenha um valor. Compatível com SQLite e PostgreSQL.

    AUDIT-2026-08-WF2 — a expressao mora em app/query_filters.py: eram TRES
    copias do mesmo `cast(coluna, JSONB) @> ...`, e o cast na coluna `json`
    crua derrubava a listagem inteira com 500 (mesmo defeito do F-043).
    """
    return destino_match(column, value)


def _build_lead_response(lead: Lead) -> LeadResponse:
    """Build LeadResponse with funnel info populated using pre-loaded relationships."""
    entries = lead.funnel_entries if hasattr(lead, 'funnel_entries') else []

    funis = []
    for entry in entries:
        if entry.funnel:
            # Find stage name
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

    responsavel_nome = lead.responsavel.nome if lead.responsavel else ("Agente IA" if lead.responsavel_id is None else None)

    resp = LeadResponse.model_validate(lead)
    resp.funis = funis
    resp.responsavel_nome = responsavel_nome
    return resp


# ─── CRUD ────────────────────────────────────────────────────────────

@router.get("", response_model=LeadListResponse, summary="Listar leads")
def list_leads(
    skip: int = Query(0, ge=0, description="Registros para pular"),
    limit: int = Query(100, ge=1, le=500, description="Máximo de registros"),
    search: Optional[str] = Query(None, description="Busca por nome, email ou whatsapp"),
    destino: Optional[str] = Query(None, description="Filtrar por destino (leads que incluem este destino)"),
    status_venda: Optional[str] = Query(None, description="Filtrar por status da venda (em_negociacao, venda, perda)"),
    is_active: Optional[bool] = Query(None, description="Filtrar por status ativo"),
    responsavel_id: Optional[int] = Query(None, description="Filtrar por responsável (0 ou null = Agente IA)"),
    data_chegada_de: Optional[date] = Query(None, description="Data de chegada a partir de (YYYY-MM-DD)"),
    data_chegada_ate: Optional[date] = Query(None, description="Data de chegada até (YYYY-MM-DD)"),
    data_partida_de: Optional[date] = Query(None, description="Data de partida a partir de (YYYY-MM-DD)"),
    data_partida_ate: Optional[date] = Query(None, description="Data de partida até (YYYY-MM-DD)"),
    exclude_funnel_id: Optional[int] = Query(
        None, description="Omite os leads que JÁ estão neste funil"
    ),
    com_telefone: Optional[bool] = Query(
        None, description="true = só com WhatsApp preenchido; false = só sem; omitido = todos"
    ),
    cursor: Optional[str] = Query(
        None, description="Cursor da página seguinte (vem em next_cursor). Ignora skip."
    ),
    include_total: bool = Query(
        True, description="false pula o COUNT e devolve total=null. Use nas páginas "
                          "seguintes: o total já é conhecido e não muda."
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Lista todos os leads com paginação e filtros avançados.

    O campo destinos é uma lista. O filtro `destino=Atacama` retorna leads que
    possuem "Atacama" em sua lista de destinos.

    **Paginação**: `skip` continua funcionando para quem já usa. Prefira
    `cursor`: envie o `next_cursor` da resposta anterior e a página seguinte vem
    sem OFFSET e sem risco de repetir/pular linha em empates de `created_at`.
    """
    query = db.query(Lead).options(
        joinedload(Lead.responsavel),
        joinedload(Lead.funnel_entries).joinedload(FunnelEntry.funnel),
        # LeadResponse.tags le lead.tags: sem isto, 1 query por lead (N+1).
        # selectinload e nao joinedload: tags e N:N e joinedload multiplicaria
        # as linhas, atrapalhando o limit da paginacao.
        selectinload(Lead.tags),
    )

    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            or_(
                Lead.nome.ilike(search_filter),
                Lead.email.ilike(search_filter),
                Lead.whatsapp.ilike(search_filter),
            )
        )
    if destino:
        query = query.filter(_json_list_contains(Lead.destinos, destino))
    if status_venda:
        query = query.filter(Lead.status_venda == status_venda)
    if is_active is not None:
        query = query.filter(Lead.is_active == is_active)
    if responsavel_id is not None:
        if responsavel_id == 0:
            # 0 = Agente IA (responsavel_id is NULL)
            query = query.filter(Lead.responsavel_id.is_(None))
        else:
            query = query.filter(Lead.responsavel_id == responsavel_id)
    if data_chegada_de:
        query = query.filter(Lead.data_chegada >= data_chegada_de)
    if data_chegada_ate:
        query = query.filter(Lead.data_chegada <= data_chegada_ate)
    if data_partida_de:
        query = query.filter(Lead.data_partida >= data_partida_de)
    if data_partida_ate:
        query = query.filter(Lead.data_partida <= data_partida_ate)
    if exclude_funnel_id is not None:
        # NOT EXISTS correlacionado: uma unica query, sem N+1 e sem trazer o
        # funil inteiro para a memoria. Usado pelo dropdown "adicionar lead ao
        # funil" do Pipeline, que antes filtrava com o board carregado.
        ja_no_funil = (
            db.query(FunnelEntry.id)
            .filter(
                FunnelEntry.lead_id == Lead.id,
                FunnelEntry.funnel_id == exclude_funnel_id,
            )
            .exists()
        )
        query = query.filter(~ja_no_funil)

    if com_telefone is not None:
        tem = _tem_telefone()
        query = query.filter(tem if com_telefone else ~tem)

    # total do conjunto FILTRADO, antes do cursor: "X leads encontrados" nao
    # pode encolher a cada "Carregar mais".
    #
    # Medido com 19.000 leads, o COUNT domina a requisicao quando ha filtro:
    # 46% no destino, 58% na busca textual, 82% no campo personalizado —
    # enquanto o SELECT da pagina custa ~1 ms. Como o total nao muda entre as
    # paginas do MESMO conjunto, quem ja o tem manda include_total=false.
    # Default True: n8n, pipeline.html e ai_tools nao enviam nada e seguem
    # recebendo o inteiro.
    total = query.count() if include_total else None

    if cursor:
        query = query.filter(_keyset_filtro(cursor))
        skip = 0          # cursor e skip sao excludentes; o cursor manda

    # limit+1 diz se ha proxima pagina sem um segundo COUNT.
    # O desempate por id e obrigatorio: sem ele a ordem entre created_at
    # iguais e indefinida e a paginacao pode repetir ou pular linha.
    ordenada = query.order_by(Lead.created_at.desc(), Lead.id.desc())
    if not cursor and skip:
        ordenada = ordenada.offset(skip)
    linhas = ordenada.limit(limit + 1).all()

    has_more = len(linhas) > limit
    leads = linhas[:limit]

    return LeadListResponse(
        total=total,
        skip=skip,
        limit=limit,
        leads=[_build_lead_response(l) for l in leads],
        next_cursor=_cursor_encode(leads[-1]) if has_more and leads else None,
        has_more=has_more,
    )


@router.get("/destinos", summary="Listar destinos disponíveis")
def list_destinos(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retorna os destinos principais + todos os destinos já cadastrados."""
    all_destinos = set(DESTINOS_PRINCIPAIS)
    leads = db.query(Lead.destinos).filter(Lead.destinos.isnot(None)).all()
    for (dest_list,) in leads:
        if isinstance(dest_list, list):
            for d in dest_list:
                if d:
                    all_destinos.add(d)
        elif isinstance(dest_list, str) and dest_list:
            all_destinos.add(dest_list)
    return {"destinos": sorted(all_destinos)}


@router.get("/segment", response_model=LeadListResponse, summary="Segmentação avançada de leads")
def segment_leads(
    # Paginação
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    # Busca textual
    search: Optional[str] = Query(None, description="Busca por nome, email ou whatsapp"),
    # Destino & Status
    destino: Optional[str] = Query(None, description="Filtrar leads que incluem este destino"),
    status_venda: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
    responsavel_id: Optional[int] = Query(None, description="Filtrar por responsável (0 = Agente IA)"),
    # Viagem — datas exatas
    data_chegada_de: Optional[date] = Query(None),
    data_chegada_ate: Optional[date] = Query(None),
    data_partida_de: Optional[date] = Query(None),
    data_partida_ate: Optional[date] = Query(None),
    # Viagem — ano/mês de chegada
    ano_chegada: Optional[int] = Query(None, description="Ano de chegada (ex: 2026)"),
    mes_chegada: Optional[int] = Query(None, ge=1, le=12, description="Mês de chegada (1-12)"),
    ano_partida: Optional[int] = Query(None, description="Ano de partida (ex: 2026)"),
    mes_partida: Optional[int] = Query(None, ge=1, le=12, description="Mês de partida (1-12)"),
    # Tags
    tag_ids: Optional[List[int]] = Query(None, description="IDs das tags para filtrar"),
    tag_mode: str = Query("any", description="'any' = OR; 'all' = AND"),
    # Funil & Etapa
    funnel_id: Optional[int] = Query(None),
    etapa_id: Optional[str] = Query(None),
    # Campo personalizado
    campo_chave: Optional[str] = Query(None, description="Chave do campo personalizado"),
    campo_valor: Optional[str] = Query(None, description="Valor do campo personalizado (contém, case-insensitive)"),
    # Data de cadastro
    criado_de: Optional[date] = Query(None, description="Cadastrado a partir de"),
    criado_ate: Optional[date] = Query(None, description="Cadastrado até"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Segmentação avançada de leads com filtros combinados.

    - **tag_mode=any**: retorna leads com pelo menos uma das tags selecionadas
    - **tag_mode=all**: retorna leads que possuem TODAS as tags selecionadas
    - **campo_chave + campo_valor**: filtra por campos personalizados (Ex: chave=origem, valor=Instagram)
    """
    # Todos os eager loads valem aqui: nao existe mais ramo que materialize a
    # base inteira, entao eles se aplicam sempre ao slice paginado.
    query = db.query(Lead).options(
        selectinload(Lead.tags),
        joinedload(Lead.responsavel),
        selectinload(Lead.funnel_entries).joinedload(FunnelEntry.funnel),
    )

    # Busca textual
    if search:
        f = f"%{search}%"
        query = query.filter(or_(
            Lead.nome.ilike(f),
            Lead.email.ilike(f),
            Lead.whatsapp.ilike(f),
        ))

    if destino:
        query = query.filter(_json_list_contains(Lead.destinos, destino))

    # Status
    if status_venda:
        query = query.filter(Lead.status_venda == status_venda)
    if is_active is not None:
        query = query.filter(Lead.is_active == is_active)

    # Responsável
    if responsavel_id is not None:
        if responsavel_id == 0:
            query = query.filter(Lead.responsavel_id.is_(None))
        else:
            query = query.filter(Lead.responsavel_id == responsavel_id)

    # Datas exatas de chegada
    if data_chegada_de:
        query = query.filter(Lead.data_chegada >= data_chegada_de)
    if data_chegada_ate:
        query = query.filter(Lead.data_chegada <= data_chegada_ate)

    # Datas exatas de partida
    if data_partida_de:
        query = query.filter(Lead.data_partida >= data_partida_de)
    if data_partida_ate:
        query = query.filter(Lead.data_partida <= data_partida_ate)

    # Ano/mês de chegada
    if ano_chegada:
        query = query.filter(extract("year", Lead.data_chegada) == ano_chegada)
    if mes_chegada:
        query = query.filter(extract("month", Lead.data_chegada) == mes_chegada)

    # Ano/mês de partida
    if ano_partida:
        query = query.filter(extract("year", Lead.data_partida) == ano_partida)
    if mes_partida:
        query = query.filter(extract("month", Lead.data_partida) == mes_partida)

    # Tags — OR (any) ou AND (all)
    if tag_ids:
        if tag_mode == "all":
            # Lead deve ter TODAS as tags — uma subquery por tag
            for tid in tag_ids:
                sub = db.query(lead_tags.c.lead_id).filter(lead_tags.c.tag_id == tid).subquery()
                query = query.filter(Lead.id.in_(sub))
        else:
            # Lead deve ter PELO MENOS UMA tag
            sub = db.query(lead_tags.c.lead_id).filter(lead_tags.c.tag_id.in_(tag_ids)).subquery()
            query = query.filter(Lead.id.in_(sub))

    # Funil & Etapa
    if funnel_id:
        entry_sub = db.query(FunnelEntry.lead_id).filter(FunnelEntry.funnel_id == funnel_id)
        if etapa_id:
            entry_sub = entry_sub.filter(FunnelEntry.etapa_id == etapa_id)
        query = query.filter(Lead.id.in_(entry_sub.subquery()))

    # Data de cadastro
    if criado_de:
        query = query.filter(Lead.created_at >= datetime.combine(criado_de, datetime.min.time()))
    if criado_ate:
        query = query.filter(Lead.created_at <= datetime.combine(criado_ate, datetime.max.time()))

    # Campo personalizado: EXISTS no banco (mesmo predicado de Segmentacoes).
    # Antes este ramo carregava TODO lead que casasse os demais filtros e
    # varria o dict em Python — 19 mil objetos ORM para devolver 50.
    if campo_chave:
        query = query.filter(campo_personalizado_match(
            Lead.campos_personalizados, campo_chave, campo_valor))

    total = query.count()
    leads = (
        query.order_by(Lead.created_at.desc(), Lead.id.desc())
        .offset(skip).limit(limit).all()
    )

    # Este endpoint continua em skip/limit de proposito: quem o consome e n8n
    # e a ferramenta de IA, que paginam por skip. O cursor entrou so em
    # /api/leads, que e o que a tela usa. A ordenacao ganhou o desempate por
    # id nos dois, entao a ordem e deterministica aqui tambem.
    return LeadListResponse(
        total=total,
        skip=skip,
        limit=limit,
        leads=[_build_lead_response(l) for l in leads],
        has_more=skip + len(leads) < total,
    )


@router.get("/segment/campos-personalizados-chaves", summary="Listar chaves de campos personalizados")
async def list_custom_field_keys(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retorna todas as chaves de campos personalizados existentes nos leads."""
    leads = db.query(Lead.campos_personalizados).filter(
        Lead.campos_personalizados.isnot(None)
    ).all()
    keys = set()
    for (cp,) in leads:
        if isinstance(cp, dict):
            keys.update(cp.keys())
    return {"chaves": sorted(keys)}


@router.get("/{lead_id}", response_model=LeadResponse, summary="Detalhes de um lead")
def get_lead(
    lead_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retorna os dados completos de um lead pelo ID."""
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    return _build_lead_response(lead)


@router.post("", response_model=LeadResponse, status_code=201, summary="Criar lead")
def create_lead(
    data: LeadCreate,
    funnel_id: Optional[int] = Query(
        None, description="Funil onde o lead entra, por id. Vence tudo. "
                          "Default: DEFAULT_FUNNEL_ID ou o funil ativo chamado "
                          "DEFAULT_FUNNEL_NOME ('Vendas: Principal')."
    ),
    funnel_nome: Optional[str] = Query(
        None, description="Funil onde o lead entra, por NOME exato (alternativa "
                          "estavel ao id, que nao e versionado). Ignorado se "
                          "funnel_id vier junto."
    ),
    etapa_id: Optional[str] = Query(
        None, description="Etapa inicial dentro do funil. Default: a etapa "
                          "DEFAULT_ETAPA_NOME ('Sem Contato') do funil resolvido."
    ),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Cria um novo lead e o coloca no Kanban (AUDIT-2026-08-WB, F-341).

    **N8N**: Ideal para criar leads a partir de formulários, WhatsApp, etc.

    Os `campos_personalizados` aceitam qualquer JSON:
    ```json
    {"origem": "Instagram", "idioma": "pt-BR", "budget": 5000}
    ```

    Alem da linha em `leads`, esta rota cria (na mesma transação) a entrada no
    funil default, o evento 'created' no histórico e, se `LEAD_TAG_ORIGEM_API`
    estiver configurada, aplica essa tag — ver `app/services/lead_creation.py`.
    Este endpoint recebe leads tanto do formulário do site quanto do agente
    n8n e não tem como distinguir a origem real; por isso a tag de origem é
    uma única config compartilhada, não uma por chamador.

    `funnel_id`/`funnel_nome`/`etapa_id` são opcionais. Sem eles, o lead entra
    no funil comercial padrão — resolvido por NOME (`DEFAULT_FUNNEL_NOME`,
    "Vendas: Principal"), não por ordem de id — na etapa `DEFAULT_ETAPA_NOME`
    ("Sem Contato"). É o contrato que o próprio system message do Gerenciador
    declara.

    AUDIT-2026-08-WF2 — `funnel_nome` existe para o **formulário do site**. O
    workflow dele chama esta rota e, logo depois, `POST /api/pipeline/funnels/
    {id}/leads` com o funil próprio de Formulário. Sem dizer aqui para onde o
    lead vai, ele ganha DUAS entradas: a padrão (Principal) e a do formulário.
    Passando `funnel_nome=Vendas: Formulário` (ou `funnel_id`), a entrada já
    nasce no lugar certo e a chamada seguinte devolve 409 — que o workflow já
    trata como sucesso. Ver M11 em `docs/audit/N8N_MANUAL_CHANGES.md`.

    Prefira `funnel_nome` a `funnel_id`: `funnels.nome` é UNIQUE e estável; o
    id não está versionado em lugar nenhum e muda entre ambientes.
    """
    # AUDIT-2026-08-WF2 (revisao) — os dois parametros recusam IGUAL.
    #
    # Antes, `funnel_nome` inexistente devolvia 404 e nao criava o lead, mas
    # `funnel_id` inexistente devolvia 201 e criava o lead SEM funil nenhum:
    # fora do Kanban, `GET /api/pipeline/locate/{id}` em 404, "Ver no Funil"
    # morto. Mesma intencao do chamador, dois contratos opostos — e o pior dos
    # dois era justamente o do parametro que a docstring acima desaconselha.
    if funnel_id is not None:
        alvo = db.query(Funnel).filter(
            Funnel.id == funnel_id, Funnel.is_active == True,  # noqa: E712
        ).first()
        if alvo is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Nenhum funil ativo com id {funnel_id}. O lead NÃO foi "
                    "criado — criar fora de funil seria pior que recusar."
                ),
            )
    elif funnel_nome:
        alvo = resolver_funil_por_nome(db, funnel_nome)
        if alvo is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Nenhum funil ativo chamado '{funnel_nome}'. O lead NÃO foi "
                    "criado — criar no funil errado seria pior que recusar."
                ),
            )
        funnel_id = alvo.id

    lead = criar_lead(
        db,
        dados=data.model_dump(),
        funnel_id=funnel_id,
        etapa_id=etapa_id,
        tag_nome=LEAD_TAG_ORIGEM_API or None,
        origem="api",
    )
    return _build_lead_response(lead)


@router.put("/{lead_id}", response_model=LeadResponse, summary="Atualizar lead")
def update_lead(
    lead_id: int,
    data: LeadUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Atualiza os dados de um lead. Envie apenas os campos que deseja alterar.
    
    Para **campos_personalizados**, envie o dict completo (sobrescreve o anterior).
    Para mesclar, leia o lead primeiro e envie os dados mesclados.
    """
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    update_data = data.model_dump(exclude_unset=True)

    # AUDIT-2026-08-WF2 (W2-21) — trocar responsavel NAO passa por aqui.
    #
    # Esta rota escrevia `responsavel_id` como qualquer outro campo, via
    # `setattr`. Duas coisas se perdiam em silencio, e as duas sao invariantes
    # que `PUT /{lead_id}/responsavel` mantem:
    #
    # 1. o evento `responsavel_changed` em LeadHistory — a propria rota
    #    dedicada grava esse evento na MESMA transacao "para nunca haver
    #    responsavel sem rastro";
    # 2. a ponte para o inbox (`conversas_bridge.notificar_handoff`) — sem ela,
    #    o lead muda de dono mas a conversa continua com a Bia ligada e fora da
    #    fila, que e exatamente o defeito principal desta rodada.
    #
    # Nenhum chamador conhecido manda `responsavel_id` neste corpo: a
    # `Tool Atualizar Lead` do n8n manda doze chaves e nenhuma e essa, e a
    # interface usa a rota dedicada (`templates/pipeline.html`). Por isso
    # recusar e mais seguro que aceitar em silencio: quem mandar recebe 422
    # dizendo qual e a rota certa, em vez de uma troca de dono sem historico e
    # sem handoff.
    if "responsavel_id" in update_data:
        raise HTTPException(
            status_code=422,
            detail=(
                "Use PUT /api/leads/{lead_id}/responsavel para trocar o "
                "responsável. Por esta rota a troca ficaria sem evento no "
                "histórico e sem mover a conversa do WhatsApp para a fila."
            ),
        )

    # AUDIT-2026-08-F2 — `None` NUNCA pode virar UPDATE de coluna NOT NULL.
    #
    # `exclude_unset` remove o que NAO foi enviado; nao remove o que foi enviado
    # COMO null. E a `Tool Atualizar Lead` do n8n manda as doze chaves em toda
    # chamada, com string vazia no que nao foi coletado — que os validadores do
    # schema convertem para None. Sem este filtro, `setattr(lead, "nome", None)`
    # bate no `nullable=False` e devolve 500 com a transacao abortada, que e
    # PIOR que o 422 anterior.
    #
    # O filtro e derivado do MODELO, nao de uma lista escrita a mao: se alguem
    # tornar uma coluna NOT NULL amanha, a protecao passa a valer sozinha. E
    # continua sendo possivel LIMPAR campo que aceita null (email, datas,
    # responsavel_id), que e comportamento legitimo da interface.
    _nao_anulaveis = {c.name for c in Lead.__table__.columns if not c.nullable}
    update_data = {
        campo: valor for campo, valor in update_data.items()
        if not (valor is None and campo in _nao_anulaveis)
    }

    for field, value in update_data.items():
        setattr(lead, field, value)

    db.commit()
    db.refresh(lead)
    return _build_lead_response(lead)


@router.delete("/{lead_id}", summary="Excluir lead permanentemente")
def delete_lead(
    lead_id: int,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Exclui um lead permanentemente do banco de dados (Hard delete)."""
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    # Limpar todas as referências explicitamente para evitar problemas com SQLite
    # 1. Tags (many-to-many)
    lead.tags.clear()
    # 2. Funnel entries
    db.query(FunnelEntry).filter(FunnelEntry.lead_id == lead_id).delete()
    # 3. Lead history
    from app.models.pipeline import LeadHistory
    db.query(LeadHistory).filter(LeadHistory.lead_id == lead_id).delete()
    # 4. Tasks
    from app.models.task import Task
    db.query(Task).filter(Task.lead_id == lead_id).delete()

    db.delete(lead)
    db.commit()
    return {"message": f"Lead '{lead.nome}' excluído permanentemente"}


# ─── ANOTAÇÕES ────────────────────────────────────────────────────────

def _lock_lead(db: Session, lead_id: int) -> Optional[Lead]:
    """
    Carrega o lead com a linha TRAVADA até o fim da transação.

    AUDIT-2026-08-WC (C3): append_anotacao fazia read-modify-write sem lock —
    duas anotações concorrentes (a `Tool Adicionar Nota` do n8n roda ao fim de
    TODO processamento do Gerenciador, e colide de verdade com uma nota
    humana no mesmo lead) liam o mesmo `campos_personalizados`, e o commit que
    terminava por último apagava silenciosamente a nota do outro.

    Mesmo padrão de `conversas/app/routers/conversations.py::_lock_conversation`:
    no PostgreSQL o FOR UPDATE serializa a segunda transação até a primeira
    commitar, então ela lê o estado já atualizado. No SQLite `with_for_update()`
    não é suportado e não é necessário — o banco inteiro já é serializado por
    lock de arquivo.
    """
    query = db.query(Lead).filter(Lead.id == lead_id)
    if not IS_SQLITE:
        query = query.with_for_update()
    return query.first()


@router.put("/{lead_id}/anotacoes", summary="Adicionar anotação ao lead")
async def append_anotacao(
    lead_id: int,
    texto: str = Query(..., description="Texto da anotação a ser adicionada"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Adiciona uma anotação ao lead, acumulando com as existentes.
    Cada nova anotação é separada por uma linha com timestamp.

    **N8N**: Use para registrar resumos de conversa e ações do Gerenciador.
    """
    lead = _lock_lead(db, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    # AUDIT-2026-08-WC (C3): era datetime.now() naive/local — todo o resto do
    # sistema usa UTC-aware. O formato exibido (dd/mm/aaaa hh:mm) não muda.
    timestamp = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M")

    campos = lead.campos_personalizados or {}
    existing = campos.get("anotacoes", "")
    new_entry = f"[{timestamp}] {texto}"

    if existing:
        campos["anotacoes"] = f"{new_entry}\n\n{existing}"
    else:
        campos["anotacoes"] = new_entry

    lead.campos_personalizados = campos
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(lead, "campos_personalizados")

    db.commit()
    db.refresh(lead)
    return {"message": "Anotação adicionada", "anotacoes": campos["anotacoes"]}


# ─── IMPORT ──────────────────────────────────────────────────────────

def _parse_date(value) -> Optional[date]:
    """Try to parse a date from various formats."""
    if value is None or value == "" or str(value).strip() == "":
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    
    s = str(value).strip()
    formats = [
        "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y",
        "%m/%d/%Y", "%Y/%m/%d", "%d.%m.%Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# Mapping of common column name variations to our field names
COLUMN_MAPPING = {
    "nome": "nome", "name": "nome", "nome completo": "nome", "full name": "nome",
    "email": "email", "e-mail": "email", "e_mail": "email",
    "whatsapp": "whatsapp", "telefone": "whatsapp", "phone": "whatsapp",
    "celular": "whatsapp", "tel": "whatsapp", "número": "whatsapp",
    "numero": "whatsapp", "fone": "whatsapp",
    "destino": "destinos", "destinos": "destinos", "destination": "destinos", "dest": "destinos",
    "data_chegada": "data_chegada", "data chegada": "data_chegada",
    "chegada": "data_chegada", "check-in": "data_chegada",
    "checkin": "data_chegada", "arrival": "data_chegada", "check in": "data_chegada",
    "data de chegada": "data_chegada",
    "data_partida": "data_partida", "data partida": "data_partida",
    "partida": "data_partida", "check-out": "data_partida",
    "checkout": "data_partida", "departure": "data_partida", "check out": "data_partida",
    "data de partida": "data_partida", "saida": "data_partida", "saída": "data_partida",
}

KNOWN_FIELDS = {"nome", "email", "whatsapp", "destinos", "data_chegada", "data_partida"}


def _normalize_header(header: str) -> str:
    """Normalize a column header for mapping."""
    return header.strip().lower().replace("_", " ").replace("-", " ")


def _process_row(row: dict, header_map: dict) -> dict:
    """Process a single row from import data into a lead dict."""
    lead_data = {"campos_personalizados": {}}

    for original_col, value in row.items():
        if value is None or str(value).strip() == "":
            continue
        
        value = str(value).strip()
        mapped_field = header_map.get(original_col)

        if mapped_field in KNOWN_FIELDS:
            if mapped_field in ("data_chegada", "data_partida"):
                lead_data[mapped_field] = _parse_date(value)
            elif mapped_field == "destinos":
                # Support comma-separated destinos in import
                lead_data[mapped_field] = [d.strip() for d in value.split(",") if d.strip()]
            else:
                lead_data[mapped_field] = value
        else:
            # Store unknown columns as custom fields
            lead_data["campos_personalizados"][original_col] = value

    return lead_data


@router.post("/import", response_model=ImportResponse, summary="Importar leads de Excel/CSV")
def import_leads(
    file: UploadFile = File(..., description="Arquivo .xlsx, .xls ou .csv"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Importa leads de um arquivo Excel (.xlsx, .xls) ou CSV (.csv).
    
    **Colunas reconhecidas automaticamente** (case-insensitive):
    - Nome: `nome`, `name`, `nome completo`
    - Email: `email`, `e-mail`
    - WhatsApp: `whatsapp`, `telefone`, `phone`, `celular`
    - Destino: `destino`, `destination`
    - Chegada: `data_chegada`, `chegada`, `check-in`, `arrival`
    - Partida: `data_partida`, `partida`, `check-out`, `departure`
    
    **Colunas não reconhecidas** são salvas automaticamente em `campos_personalizados`.
    
    **Formatos de data aceitos**: `YYYY-MM-DD`, `DD/MM/YYYY`, `DD-MM-YYYY`, `MM/DD/YYYY`
    
    **N8N**: Envie o arquivo como `multipart/form-data`.
    """
    filename = file.filename.lower() if file.filename else ""
    content = file.file.read()
    
    if len(content) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Arquivo muito grande. Máximo permitido: {MAX_UPLOAD_SIZE_BYTES // (1024*1024)}MB"
        )

    if not filename:
        raise HTTPException(status_code=400, detail="Nome do arquivo não informado")

    rows = []

    if filename.endswith((".xlsx", ".xls")):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            ws = wb.active
            
            all_rows = list(ws.iter_rows(values_only=True))
            if len(all_rows) < 2:
                raise HTTPException(status_code=400, detail="Arquivo vazio ou sem dados")
            
            headers = [str(h).strip() if h else f"coluna_{i}" for i, h in enumerate(all_rows[0])]
            for row_values in all_rows[1:]:
                row_dict = {}
                for i, val in enumerate(row_values):
                    if i < len(headers):
                        row_dict[headers[i]] = val
                rows.append(row_dict)
            wb.close()

        except ImportError:
            raise HTTPException(
                status_code=500,
                detail="Biblioteca openpyxl não está instalada. Instale com: pip install openpyxl"
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Erro ao ler arquivo Excel: {str(e)}")

    elif filename.endswith(".csv"):
        try:
            text = content.decode("utf-8-sig")  # Handle BOM
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
        except UnicodeDecodeError:
            try:
                text = content.decode("latin-1")
                reader = csv.DictReader(io.StringIO(text))
                rows = list(reader)
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Erro ao ler CSV: {str(e)}")
    else:
        raise HTTPException(
            status_code=400,
            detail="Formato não suportado. Use .xlsx, .xls ou .csv"
        )

    if not rows:
        raise HTTPException(status_code=400, detail="Nenhum dado encontrado no arquivo")

    # Build header mapping
    header_map = {}
    sample_row = rows[0]
    for col in sample_row.keys():
        normalized = _normalize_header(col)
        if normalized in COLUMN_MAPPING:
            header_map[col] = COLUMN_MAPPING[normalized]
        else:
            header_map[col] = None  # Will go to custom fields

    imported = 0
    errors = []

    for i, row in enumerate(rows, start=2):  # Line 2 = first data row
        try:
            lead_data = _process_row(row, header_map)

            if not lead_data.get("nome"):
                errors.append(f"Linha {i}: campo 'nome' é obrigatório")
                continue

            # AUDIT-2026-08-WB (F-341): mesmo caminho de POST /api/leads —
            # lead importado tambem entra no funil default, nao so na tabela
            # `leads`. criar_lead commita por linha (ver docstring do
            # service); o `db.commit()` unico do fim do lote deixou de existir.
            criar_lead(
                db,
                dados=lead_data,
                tag_nome=LEAD_TAG_ORIGEM_API or None,
                origem="import",
            )
            imported += 1

        except Exception as e:
            # Sem isto, uma linha ruim deixa a sessão com a transação
            # ABORTADA (PostgreSQL) e TODAS as linhas seguintes do mesmo
            # import falhariam em cascata — criar_lead comita por linha, mas
            # uma exceção no meio do seu próprio commit deixa a sessão nesse
            # estado até um rollback explícito.
            db.rollback()
            errors.append(f"Linha {i}: {str(e)}")

    return ImportResponse(
        total_linhas=len(rows),
        importados=imported,
        erros=len(errors),
        detalhes_erros=errors[:50],  # Limit error details
    )


# ─── INTEGRATION: Conversas ←→ CRM ──────────────────────────────────

@router.get("/by-whatsapp/{whatsapp}", response_model=LeadResponse,
            summary="Buscar lead pelo WhatsApp")
def get_lead_by_whatsapp(
    whatsapp: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Busca um lead pelo número de WhatsApp.
    Usado pela plataforma Conversas para vincular conversas a leads automaticamente.

    Contrato (inalterado): casamento por IDENTIDADE de dígitos, tolerante a
    DDI presente de um lado só (sufixo compatível), e AMBIGUIDADE É RECUSADA
    com 409 — nunca resolvida escolhendo um lead arbitrário.
    """
    # AUDIT-2026-08-WF2 — os dois lados normalizados, no banco.
    #
    # Os tres passos antigos consultavam a coluna CRUA: igualdade com o numero
    # sem "+", igualdade com "+" na frente, e `ilike('%' || 11 ultimos
    # digitos)`. Um lead gravado como `+55 11 98765-4322` nao casava em NENHUM
    # dos tres — a string crua nao termina em `11987654322` (tem espaco e hifen
    # no meio) e tampouco e igual ao numero so-digitos. Resultado: 404.
    #
    # Isto se alimenta: esta e a rota do no `Buscar lead pelo WhatsApp` do
    # formulario do site e da Tool Buscar Lead WhatsApp do Gerenciador, e e o
    # PROPRIO formulario que grava o numero formatado. O 404 fazia o fluxo
    # criar um lead NOVO em vez de atualizar o existente, e o proximo lookup do
    # mesmo cliente voltava a nao achar nenhum dos dois. Medido em PostgreSQL
    # 16 com 19.001 leads: os 6 formatos do corpus davam 404, inclusive a busca
    # pela string EXATA que estava gravada.
    #
    # `_digitos_no_banco` aplica a mesma normalizacao do `_only_digits` na
    # coluna, entao os dois passos abaixo enxergam o lead em qualquer formato.
    normalized = _only_digits(whatsapp)
    if not normalized:
        # Sem digitos nao ha numero para identificar: buscar por `''` casaria
        # com qualquer lead cujo whatsapp so tenha simbolos.
        raise HTTPException(status_code=404, detail="Nenhum lead encontrado com este WhatsApp")

    digitos_col = _digitos_no_banco(Lead.whatsapp)

    # 1. Igualdade EXATA de digitos — cobre os passos 1 e 2 antigos (com e sem
    # "+") e, agora, tambem qualquer formatacao guardada na coluna.
    #
    # O `.first()` antigo virou dicionario porque a normalizacao FAZ APARECER
    # ambiguidade onde a coluna crua escondia: o par duplicado
    # `+55 11 98765-4322` / `5511987654322` — exatamente o que o 404 vinha
    # fabricando — passa a casar nos DOIS leads. A resposta para isso e o 409
    # que ja existia no passo de sufixo, nao um `.first()` arbitrario: aqui
    # tambem um casamento errado atualiza o LEAD DO OUTRO CLIENTE.
    #
    # O `_only_digits` em Python decide de novo, sobre os <= 50 candidatos:
    # `[^0-9]` do PostgreSQL e ASCII e `str.isdigit()` nao e, entao quem afirma
    # identidade e sempre o Python (mesma regra do modulo do Conversas).
    compativeis = {
        c.id: c
        for c in db.query(Lead).filter(digitos_col == normalized).limit(50).all()
        if _only_digits(c.whatsapp) == normalized
    }

    # 2. AUDIT-2026-08-WC (C6/W2-12): o ilike de sufixo era resolvido por
    # .first() SEM order_by — com mais de um lead terminando nos mesmos
    # digitos, quem vencia era indefinido pelo banco e podia mudar entre
    # execucoes ("localizar lead esta intermitente"). Esta rota e o primeiro
    # no do fluxo do Formulario e a Tool Buscar Lead WhatsApp do Gerenciador:
    # um casamento errado atualiza o LEAD DO OUTRO CLIENTE.
    #
    # Mesma regra de conversas/app/services/crm.py::lookup_lead_by_whatsapp
    # (AUDIT-2026-08-W2F/F10): o LIKE serve so de PRE-FILTRO barato. A
    # decisao e por compatibilidade EXATA de digitos, feita em Python — um
    # numero e "compativel" com o outro quando um e sufixo do outro (cobre o
    # caso de DDI presente de um lado e ausente do outro, que e o motivo deste
    # passo existir: "handles country code variations"). Se mais de UM lead
    # DISTINTO for compativel, a ambiguidade e RECUSADA (409), nunca resolvida
    # por ORDER BY/LIMIT 1 arbitrario.
    #
    # So roda quando o passo 1 nao achou nada: casamento exato continua tendo
    # precedencia sobre sufixo, senao buscar `5511987654321` com um lead
    # `11987654321` e outro `5511987654321` no banco viraria 409 onde antes
    # havia resposta.
    if not compativeis and len(normalized) >= 11:
        suffix = normalized[-11:]
        candidatos = (
            db.query(Lead)
            .filter(digitos_col.like(f"%{suffix}"))
            .limit(50)
            .all()
        )
        for c in candidatos:
            digitos = _only_digits(c.whatsapp)
            if digitos and (digitos.endswith(normalized) or normalized.endswith(digitos)):
                compativeis[c.id] = c

    if len(compativeis) > 1:
        raise HTTPException(
            status_code=409,
            detail=(
                "Mais de um lead tem um WhatsApp compatível com este número "
                f"(ids: {sorted(compativeis)}). Desambigue manualmente — "
                "nenhum foi escolhido automaticamente."
            ),
        )

    if not compativeis:
        raise HTTPException(status_code=404, detail="Nenhum lead encontrado com este WhatsApp")

    return _build_lead_response(next(iter(compativeis.values())))


@router.put("/{lead_id}/responsavel", response_model=LeadResponse,
            summary="Alterar responsável do lead")
async def update_lead_responsavel(
    lead_id: int,
    response: Response,
    responsavel_id: Optional[int] = Query(None, description="ID do novo responsável (null = Agente IA)"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Altera o responsável de um lead.
    Envie `responsavel_id=null` para atribuir ao Agente IA.
    """
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    # Validate user exists if provided
    if responsavel_id is not None:
        user = db.query(User).filter(User.id == responsavel_id, User.is_active == True).first()
        if not user:
            raise HTTPException(status_code=404, detail="Usuário não encontrado ou inativo")

    old_responsavel = lead.responsavel_id
    lead.responsavel_id = responsavel_id

    # Log the change — MESMA transacao do UPDATE do lead: se o historico falhar,
    # a troca de responsavel tambem e revertida (nunca responsavel sem rastro).
    from app.models.pipeline import LeadHistory
    old_name = "Agente IA" if old_responsavel is None else str(old_responsavel)
    new_name = "Agente IA" if responsavel_id is None else str(responsavel_id)

    if old_responsavel != responsavel_id:
        event = LeadHistory(
            lead_id=lead_id,
            evento="responsavel_changed",
            descricao=f"Responsável alterado de '{old_name}' para '{new_name}'",
            dados={"old_responsavel_id": old_responsavel, "new_responsavel_id": responsavel_id},
        )
        db.add(event)

    db.commit()
    db.refresh(lead)

    # AUDIT-2026-08-WA — propaga o handoff para o inbox.
    #
    # Esta rota e o UNICO sinal deterministico que o repositorio recebe quando o
    # Gerenciador decide encaminhar para humano (`Tool Alterar Responsavel`).
    # Ate aqui ela mexia so em `leads.responsavel_id`: a conversa do WhatsApp
    # continuava com a Bia ligada e fora da fila, e o cliente ouvia que estava
    # na fila sem estar. `POST /api/conversations/{id}/handoff` existia e nao
    # tinha chamador — este e o chamador.
    #
    # So propaga quando o novo responsavel e uma PESSOA. `responsavel_id=null`
    # significa devolver o lead ao Agente IA; mover a conversa para a fila
    # humana nesse caso seria o oposto da intencao.
    #
    # Best-effort: o resultado vai na resposta, mas uma falha aqui nao desfaz a
    # troca de responsavel nem devolve erro ao n8n.
    # AUDIT-2026-08-WA (revisao) — NAO condicionar a `old_responsavel !=
    # responsavel_id`. Era assim, e desligava a ponte em quase todo handoff.
    #
    # O n8n manda `?responsavel_id=5` FIXO, e nada no CRM devolve
    # `lead.responsavel_id` para NULL quando a conversa encerra. Mas o Conversas
    # reseta a conversa para a Bia toda vez que um cliente encerrado volta a
    # escrever (webhook.py, ramo de reabertura). Entao, no SEGUNDO handoff do
    # mesmo lead, o CRM via `5 == 5`, pulava a ponte, e a conversa ficava presa
    # em ATENDIMENTOS BIA — o defeito original de volta, e em silencio, porque
    # `conversa_notificada` continuava None e nem log saia. Cliente que volta
    # nao e caso raro: e o caso comum.
    #
    # Reenviar quando nada mudou custa UMA chamada best-effort. O handoff do
    # lado do Conversas e idempotente por construcao (`keep_queue_position`),
    # entao reaplicar sobre uma conversa ja atendida nao mexe em nada.
    conversa_notificada = None
    if responsavel_id is not None:
        from app.services import conversas_bridge

        conversa_notificada = await conversas_bridge.notificar_handoff(lead_id)

    resposta = _build_lead_response(lead)

    # AUDIT-2026-08-WF2 (revisao) — o resultado da ponte vai num CABECALHO, nao
    # so no log.
    #
    # `conversas_bridge` promete na docstring que "o chamador expoe isso ao
    # cliente para que uma falha silenciosa nao se disfarce de sucesso", e ate
    # aqui a promessa nao era cumprida: com o Conversas reiniciando, a ponte
    # estourava o timeout de 5s, devolvia None, e o n8n recebia um 200
    # identico ao do caso bem-sucedido. A Bia dizia ao cliente que ele estava
    # na fila, a conversa continuava em ATENDIMENTOS BIA, e o unico rastro era
    # um warning do lado do CRM — exatamente a invisibilidade que fez o handoff
    # quebrado passar despercebido em primeiro lugar.
    #
    # Cabecalho, e nao campo do corpo, por dois motivos: `response_model=
    # LeadResponse` descartaria um campo extra, e mudar o schema do lead por
    # causa de um detalhe de transporte contaminaria todos os outros
    # consumidores da rota.
    #
    # Continua sem virar 5xx de proposito: derrubar o PUT perderia a troca de
    # responsavel, que ja foi commitada e esta correta. Quem le o cabecalho
    # decide o que fazer — `pendente` e acionavel, o 200 sozinho nao era.
    if responsavel_id is None:
        estado_ponte = "nao_aplicavel"   # devolver ao Agente IA nao move fila
    elif conversa_notificada is True:
        estado_ponte = "movida"
    elif conversa_notificada is False:
        estado_ponte = "sem_conversa"    # nao ha conversa aberta para este lead
    else:
        estado_ponte = "pendente"        # ponte desligada, fora do ar ou timeout
    response.headers["X-Conversa-Handoff"] = estado_ponte

    if conversa_notificada is not None:
        logger.info(
            "Lead %s: responsavel alterado; conversa %s movida para a fila humana.",
            lead_id, "" if conversa_notificada else "NAO",
        )
    elif responsavel_id is not None:
        logger.warning(
            "Lead %s: responsavel alterado, mas a ponte para o Conversas NAO "
            "respondeu (desligada, fora do ar ou timeout). A conversa pode ter "
            "ficado com a Bia. Cabecalho X-Conversa-Handoff: pendente.",
            lead_id,
        )
    return resposta

