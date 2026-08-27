from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.tag import Tag, lead_tags
from app.models.lead import Lead
from app.models.user import User
from app.schemas.tag import (
    TagCreate,
    TagUpdate,
    TagResponse,
    TagListResponse,
    LeadTagsUpdate,
)
from app.auth import get_current_user, require_admin

router = APIRouter(prefix="/api/tags", tags=["Tags"])

# AUDIT-2026-08-W2G (F12): handlers deste router são `def` puros, não
# `async def` — fazem I/O SÍNCRONO do SQLAlchemy, que como `async` rodava no
# event loop e travava as demais requisições do worker. Sendo `def`, o FastAPI
# executa na threadpool (mesmo padrão de leads.py/pipeline.py).


# ─── Tags CRUD ───────────────────────────────────────────

@router.get("", response_model=TagListResponse, summary="Listar todas as tags")
def list_tags(
    search: Optional[str] = Query(None, description="Busca por nome"),
    skip: int = Query(0, ge=0, description="Registros para pular"),
    limit: int = Query(100, ge=1, le=500, description="Máximo de registros"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Lista todas as tags disponíveis com paginação.
    
    **N8N**: Use para obter a lista de tags antes de associar a um lead.
    """
    query = db.query(Tag)
    if search:
        query = query.filter(Tag.nome.ilike(f"%{search}%"))
    
    total = query.count()
    tags = query.order_by(Tag.nome).offset(skip).limit(limit).all()
    return TagListResponse(
        total=total,
        tags=[TagResponse.model_validate(t) for t in tags],
    )


@router.get("/{tag_id}", response_model=TagResponse, summary="Detalhes de uma tag")
def get_tag(
    tag_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tag = db.query(Tag).filter(Tag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag não encontrada")
    return TagResponse.model_validate(tag)


@router.post("", response_model=TagResponse, status_code=201, summary="Criar tag")
def create_tag(
    data: TagCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Cria uma nova tag com nome e cor.
    
    **N8N**: Crie tags dinamicamente antes de associá-las a leads.
    """
    existing = db.query(Tag).filter(Tag.nome == data.nome).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Já existe uma tag com este nome"
        )

    tag = Tag(nome=data.nome, cor=data.cor)
    db.add(tag)
    # AUDIT-2026-08-W2G (F7): o check acima é só fast path. `tags.nome` tem
    # índice único no banco: dois POSTs concorrentes (retry do n8n, dois
    # operadores) passavam os dois pelo check e o segundo virava 500 opaco com a
    # transação abortada e sem caminho de rollback. O contrato aqui é 409.
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Já existe uma tag com este nome"
        )
    db.refresh(tag)
    return TagResponse.model_validate(tag)


@router.put("/{tag_id}", response_model=TagResponse, summary="Atualizar tag")
def update_tag(
    tag_id: int,
    data: TagUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tag = db.query(Tag).filter(Tag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag não encontrada")

    update_data = data.model_dump(exclude_unset=True)

    if "nome" in update_data and update_data["nome"] != tag.nome:
        existing = db.query(Tag).filter(Tag.nome == update_data["nome"]).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Já existe uma tag com este nome"
            )

    for field, value in update_data.items():
        setattr(tag, field, value)

    # AUDIT-2026-08-W2G (F7): mesma corrida do create — renomear para um nome que
    # outra transação acabou de gravar bate no mesmo índice único.
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Já existe uma tag com este nome"
        )
    db.refresh(tag)
    return TagResponse.model_validate(tag)


@router.delete("/{tag_id}", summary="Excluir tag")
def delete_tag(
    tag_id: int,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Exclui a tag e remove todas as associações com leads."""
    tag = db.query(Tag).filter(Tag.id == tag_id).first()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag não encontrada")

    db.delete(tag)
    db.commit()
    return {"message": f"Tag '{tag.nome}' excluída"}


# ─── Lead-Tag Association ────────────────────────────────

@router.put("/lead/{lead_id}", summary="Definir tags de um lead")
def set_lead_tags(
    lead_id: int,
    data: LeadTagsUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Define as tags de um lead. Dois modos (a validação do payload recusa a
    mistura dos dois na mesma chamada — ver `LeadTagsUpdate`):

    **Full-replace** — substitui TODAS as tags do lead pela lista enviada.
    **N8N**: `Tool Definir Tags Lead` usa este modo e continua funcionando.
    ```json
    {"tag_ids": [1, 3, 5]}
    ```

    **Incremental** — altera só os IDs informados, sem tocar no resto.
    AUDIT-2026-08-WC (C1): é o modo que o editor de lead do CRM usa hoje.
    Full-replace a partir de um snapshot tirado quando o editor abriu apagava
    em silêncio qualquer tag aplicada por outra origem (outro operador,
    Conversas, n8n) enquanto o editor estava aberto.
    ```json
    {"adicionar": [2], "remover": [5]}
    ```
    """
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    if data.tag_ids is not None:
        tags = db.query(Tag).filter(Tag.id.in_(data.tag_ids)).all()
        if len(tags) != len(data.tag_ids):
            found_ids = {t.id for t in tags}
            missing = [tid for tid in data.tag_ids if tid not in found_ids]
            raise HTTPException(status_code=404, detail=f"Tags não encontradas: {missing}")
        lead.tags = tags
    else:
        ids_envolvidos = list({*(data.adicionar or []), *(data.remover or [])})
        if ids_envolvidos:
            encontradas = db.query(Tag).filter(Tag.id.in_(ids_envolvidos)).all()
            found_ids = {t.id for t in encontradas}
            missing = [tid for tid in ids_envolvidos if tid not in found_ids]
            if missing:
                raise HTTPException(status_code=404, detail=f"Tags não encontradas: {missing}")

            por_id = {t.id: t for t in encontradas}
            atuais = {t.id for t in lead.tags}
            for tid in (data.adicionar or []):
                if tid not in atuais:
                    lead.tags.append(por_id[tid])
                    atuais.add(tid)
            remover_ids = set(data.remover or [])
            if remover_ids:
                lead.tags = [t for t in lead.tags if t.id not in remover_ids]

    db.commit()

    return {
        "lead_id": lead_id,
        "tags": [TagResponse.model_validate(t) for t in lead.tags],
    }


@router.get("/lead/{lead_id}", summary="Tags de um lead")
def get_lead_tags(
    lead_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retorna as tags associadas a um lead."""
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(status_code=404, detail="Lead não encontrado")

    return {
        "lead_id": lead_id,
        "tags": [TagResponse.model_validate(t) for t in lead.tags],
    }
