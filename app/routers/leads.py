import io
import csv
from typing import Optional, List
from datetime import datetime, date

from fastapi import APIRouter, Depends, HTTPException, status, Query, UploadFile, File
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_, extract, and_, String

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
from app.auth import get_current_user

router = APIRouter(prefix="/api/leads", tags=["Leads"])


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

    resp = LeadResponse.model_validate(lead)
    resp.funis = funis
    return resp


# ─── CRUD ────────────────────────────────────────────────────────────

@router.get("", response_model=LeadListResponse, summary="Listar leads")
async def list_leads(
    skip: int = Query(0, ge=0, description="Registros para pular"),
    limit: int = Query(100, ge=1, le=500, description="Máximo de registros"),
    search: Optional[str] = Query(None, description="Busca por nome, email ou whatsapp"),
    destino: Optional[str] = Query(None, description="Filtrar por destino (leads que incluem este destino)"),
    status_venda: Optional[str] = Query(None, description="Filtrar por status da venda (em_negociacao, venda, perda)"),
    is_active: Optional[bool] = Query(None, description="Filtrar por status ativo"),
    data_chegada_de: Optional[date] = Query(None, description="Data de chegada a partir de (YYYY-MM-DD)"),
    data_chegada_ate: Optional[date] = Query(None, description="Data de chegada até (YYYY-MM-DD)"),
    data_partida_de: Optional[date] = Query(None, description="Data de partida a partir de (YYYY-MM-DD)"),
    data_partida_ate: Optional[date] = Query(None, description="Data de partida até (YYYY-MM-DD)"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Lista todos os leads com paginação e filtros avançados.
    
    O campo destinos é uma lista. O filtro `destino=Atacama` retorna leads que
    possuem "Atacama" em sua lista de destinos.
    """
    query = db.query(Lead)

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
        # JSON list contains — SQLite stores JSON as text, so LIKE works
        query = query.filter(Lead.destinos.cast(String).ilike(f'%"{destino}"%'))
    if status_venda:
        query = query.filter(Lead.status_venda == status_venda)
    if is_active is not None:
        query = query.filter(Lead.is_active == is_active)
    if data_chegada_de:
        query = query.filter(Lead.data_chegada >= data_chegada_de)
    if data_chegada_ate:
        query = query.filter(Lead.data_chegada <= data_chegada_ate)
    if data_partida_de:
        query = query.filter(Lead.data_partida >= data_partida_de)
    if data_partida_ate:
        query = query.filter(Lead.data_partida <= data_partida_ate)

    total = query.count()
    leads = query.order_by(Lead.created_at.desc()).offset(skip).limit(limit).all()

    return LeadListResponse(
        total=total,
        skip=skip,
        limit=limit,
        leads=[_build_lead_response(l, db) for l in leads],
    )


@router.get("/destinos", summary="Listar destinos disponíveis")
async def list_destinos(
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
async def segment_leads(
    # Paginação
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    # Busca textual
    search: Optional[str] = Query(None, description="Busca por nome, email ou whatsapp"),
    # Destino & Status
    destino: Optional[str] = Query(None, description="Filtrar leads que incluem este destino"),
    status_venda: Optional[str] = Query(None),
    is_active: Optional[bool] = Query(None),
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
    query = db.query(Lead).options(joinedload(Lead.tags))

    # Busca textual
    if search:
        f = f"%{search}%"
        query = query.filter(or_(
            Lead.nome.ilike(f),
            Lead.email.ilike(f),
            Lead.whatsapp.ilike(f),
        ))

    # Destino (JSON list contains)
    if destino:
        query = query.filter(Lead.destinos.cast(String).ilike(f'%"{destino}"%'))

    # Status
    if status_venda:
        query = query.filter(Lead.status_venda == status_venda)
    if is_active is not None:
        query = query.filter(Lead.is_active == is_active)

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

    # Buscar todos para filtro de campo personalizado (SQLite não suporta JSON path queries avançadas)
    if campo_chave:
        all_leads = query.order_by(Lead.created_at.desc()).all()
        chave = campo_chave.strip().lower()
        valor = (campo_valor or "").strip().lower()
        filtered = []
        for lead in all_leads:
            cp = lead.campos_personalizados or {}
            # Busca case-insensitive na chave
            match_key = next((k for k in cp if k.strip().lower() == chave), None)
            if match_key is not None:
                if not valor or valor in str(cp[match_key]).lower():
                    filtered.append(lead)
        total = len(filtered)
        leads = filtered[skip: skip + limit]
    else:
        total = query.count()
        leads = query.order_by(Lead.created_at.desc()).offset(skip).limit(limit).all()

    return LeadListResponse(
        total=total,
        skip=skip,
        limit=limit,
        leads=[_build_lead_response(l, db) for l in leads],
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
async def get_lead(
    lead_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retorna os dados completos de um lead pelo ID."""
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")
    return _build_lead_response(lead, db)


@router.post("", response_model=LeadResponse, status_code=201, summary="Criar lead")
async def create_lead(
    data: LeadCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Cria um novo lead.
    
    **N8N**: Ideal para criar leads a partir de formulários, WhatsApp, etc.
    
    Os `campos_personalizados` aceitam qualquer JSON:
    ```json
    {"origem": "Instagram", "idioma": "pt-BR", "budget": 5000}
    ```
    """
    lead = Lead(
        nome=data.nome,
        email=data.email,
        whatsapp=data.whatsapp,
        destinos=data.destinos or [],
        data_chegada=data.data_chegada,
        data_partida=data.data_partida,
        campos_personalizados=data.campos_personalizados or {},
        status_venda=data.status_venda,
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return _build_lead_response(lead, db)


@router.put("/{lead_id}", response_model=LeadResponse, summary="Atualizar lead")
async def update_lead(
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
    for field, value in update_data.items():
        setattr(lead, field, value)

    db.commit()
    db.refresh(lead)
    return _build_lead_response(lead, db)


@router.delete("/{lead_id}", summary="Excluir lead permanentemente")
async def delete_lead(
    lead_id: int,
    current_user: User = Depends(get_current_user),
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
async def import_leads(
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
    content = await file.read()

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

            lead = Lead(
                nome=lead_data.get("nome", ""),
                email=lead_data.get("email"),
                whatsapp=lead_data.get("whatsapp"),
                destinos=lead_data.get("destinos", []),
                data_chegada=lead_data.get("data_chegada"),
                data_partida=lead_data.get("data_partida"),
                campos_personalizados=lead_data.get("campos_personalizados", {}),
            )
            db.add(lead)
            imported += 1

        except Exception as e:
            errors.append(f"Linha {i}: {str(e)}")

    if imported > 0:
        db.commit()

    return ImportResponse(
        total_linhas=len(rows),
        importados=imported,
        erros=len(errors),
        detalhes_erros=errors[:50],  # Limit error details
    )
