"""
CONV-VAR-01 — CRUD de variaveis dinamicas + catalogo + preview.

Permissoes (padrao ja existente do Conversas, ver api_config.py):
  - leitura (listar, detalhe, catalogo, preview): `get_current_user`
    — o vendedor precisa ver as variaveis para inseri-las na mensagem;
  - criar/editar/excluir: `require_admin` (role == 'admin' -> 403).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.auth import User, get_current_user, require_admin
from app.database import get_db
from app.models.conversation import Conversation
from app.models.message_variable import MessageVariable
from app.schemas.message_variable import (
    CatalogResponse,
    MessageVariableCreate,
    MessageVariableListResponse,
    MessageVariableResponse,
    MessageVariableUpdate,
    PreviewRequest,
    PreviewResponse,
)
from app.services import variables as variables_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/variables", tags=["Variáveis"])


def _author_names(db: Session, rows) -> dict:
    """Nome do autor por id (uma query para a lista inteira)."""
    ids = {row.created_by for row in rows if row.created_by}
    if not ids:
        return {}
    users = db.query(User).filter(User.id.in_(ids)).all()
    return {u.id: u.nome for u in users}


@router.get("", response_model=MessageVariableListResponse)
async def list_variables(
    active_only: bool = Query(True, description="Somente variáveis ativas"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Listar variáveis (fonte do seletor de inserção e da tela de gestão)."""
    query = db.query(MessageVariable)
    if active_only:
        query = query.filter(MessageVariable.is_active == True)  # noqa: E712
    rows = query.order_by(MessageVariable.token).all()
    names = _author_names(db, rows)
    return MessageVariableListResponse(
        variables=[MessageVariableResponse.from_model(r, names.get(r.created_by)) for r in rows],
        total=len(rows),
    )


@router.get("/catalog", response_model=CatalogResponse)
async def get_catalog(
    current_user: User = Depends(get_current_user),
):
    """Catálogo de propriedades dinâmicas reais do sistema, agrupado."""
    return CatalogResponse(groups=variables_service.catalog_groups())


@router.post("/preview", response_model=PreviewResponse)
async def preview_text(
    data: PreviewRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Prévia com o MESMO mecanismo do envio: a interface nunca reimplementa a
    resolução, então prévia e envio não podem divergir.
    """
    conversation = None
    if data.conversation_id:
        conversation = db.query(Conversation).filter(
            Conversation.id == data.conversation_id
        ).first()
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversa não encontrada")

    ctx = variables_service.VariableContext(db, conversation=conversation, user=current_user)
    rendered, problems = variables_service.render(db, data.text, ctx)
    return PreviewResponse(
        rendered=rendered,
        problems=[p._asdict() for p in problems],
        ok=not problems,
    )


@router.post("", response_model=MessageVariableResponse, status_code=201)
async def create_variable(
    data: MessageVariableCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Criar variável (somente administradores)."""
    existing = db.query(MessageVariable).filter(MessageVariable.token == data.token).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"A variável {data.token} já existe")

    variable = MessageVariable(
        name=data.name.strip(),
        token=data.token,
        kind=data.kind,
        fixed_value=data.fixed_value,
        source_key=data.source_key,
        created_by=current_user.id,
    )
    db.add(variable)
    db.commit()
    db.refresh(variable)

    logger.info(f"Variável criada: {variable.token} (kind={variable.kind})")
    return MessageVariableResponse.from_model(variable, current_user.nome)


@router.get("/{variable_id}", response_model=MessageVariableResponse)
async def get_variable(
    variable_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Detalhe de uma variável."""
    variable = db.query(MessageVariable).filter(MessageVariable.id == variable_id).first()
    if not variable:
        raise HTTPException(status_code=404, detail="Variável não encontrada")
    names = _author_names(db, [variable])
    return MessageVariableResponse.from_model(variable, names.get(variable.created_by))


@router.put("/{variable_id}", response_model=MessageVariableResponse)
async def update_variable(
    variable_id: int,
    data: MessageVariableUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Editar variável (somente administradores)."""
    variable = db.query(MessageVariable).filter(MessageVariable.id == variable_id).first()
    if not variable:
        raise HTTPException(status_code=404, detail="Variável não encontrada")

    if data.token is not None and data.token != variable.token:
        duplicate = db.query(MessageVariable).filter(
            MessageVariable.token == data.token,
            MessageVariable.id != variable_id,
        ).first()
        if duplicate:
            raise HTTPException(status_code=409, detail=f"A variável {data.token} já existe")
        variable.token = data.token

    if data.name is not None:
        variable.name = data.name.strip()
    if data.kind is not None:
        variable.kind = data.kind
    if data.fixed_value is not None:
        variable.fixed_value = data.fixed_value
    if data.source_key is not None:
        variable.source_key = data.source_key
    if data.is_active is not None:
        variable.is_active = data.is_active

    # Coerência final: o tipo manda sobre os campos de valor/origem. A troca de
    # tipo exige o campo do NOVO tipo na mesma requisição — senão a variável
    # ficaria salva sem valor e todo envio que a usasse passaria a ser
    # bloqueado, sem o administrador perceber.
    if variable.kind == "dynamic":
        if not variable.source_key:
            raise HTTPException(
                status_code=422,
                detail="Escolha a propriedade do sistema para uma variável do tipo Variável.",
            )
        variable.fixed_value = None
    else:
        if data.kind == "fixed" and not (variable.fixed_value or "").strip():
            raise HTTPException(
                status_code=422,
                detail="Informe o valor da variável ao mudar o tipo para Fixo.",
            )
        variable.source_key = None

    db.commit()
    db.refresh(variable)
    names = _author_names(db, [variable])
    return MessageVariableResponse.from_model(variable, names.get(variable.created_by))


@router.delete("/{variable_id}")
async def delete_variable(
    variable_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Excluir variável (somente administradores)."""
    variable = db.query(MessageVariable).filter(MessageVariable.id == variable_id).first()
    if not variable:
        raise HTTPException(status_code=404, detail="Variável não encontrada")

    token = variable.token
    db.delete(variable)
    db.commit()
    logger.info(f"Variável excluída: {token}")
    return {"message": f"Variável {token} excluída"}
