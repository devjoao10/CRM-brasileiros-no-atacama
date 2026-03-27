import io
import csv
from typing import Optional
from datetime import datetime, date

from fastapi import APIRouter, Depends, HTTPException, status, Query, UploadFile, File
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import or_

from app.database import get_db
from app.models.lead import Lead
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
    destino: Optional[str] = Query(None, description="Filtrar por destino"),
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
    
    **N8N**: Use query params para filtrar. Exemplo:
    `GET /api/leads?destino=Atacama&data_chegada_de=2026-04-01`
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
        query = query.filter(Lead.destino == destino)
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
    # Get distinct destinos from DB
    custom = db.query(Lead.destino).filter(Lead.destino.isnot(None)).distinct().all()
    all_destinos = set(DESTINOS_PRINCIPAIS)
    for (d,) in custom:
        if d:
            all_destinos.add(d)
    return {"destinos": sorted(all_destinos)}


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
        destino=data.destino,
        data_chegada=data.data_chegada,
        data_partida=data.data_partida,
        campos_personalizados=data.campos_personalizados or {},
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


@router.delete("/{lead_id}", summary="Desativar lead")
async def delete_lead(
    lead_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Desativa um lead (soft delete)."""
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    lead.is_active = False
    db.commit()
    return {"message": f"Lead '{lead.nome}' desativado"}


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
    "destino": "destino", "destination": "destino", "dest": "destino",
    "data_chegada": "data_chegada", "data chegada": "data_chegada",
    "chegada": "data_chegada", "check-in": "data_chegada",
    "checkin": "data_chegada", "arrival": "data_chegada", "check in": "data_chegada",
    "data de chegada": "data_chegada",
    "data_partida": "data_partida", "data partida": "data_partida",
    "partida": "data_partida", "check-out": "data_partida",
    "checkout": "data_partida", "departure": "data_partida", "check out": "data_partida",
    "data de partida": "data_partida", "saida": "data_partida", "saída": "data_partida",
}

KNOWN_FIELDS = {"nome", "email", "whatsapp", "destino", "data_chegada", "data_partida"}


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
                destino=lead_data.get("destino"),
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
