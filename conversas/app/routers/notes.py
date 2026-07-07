"""
CONV-07 — Notas internas de conversa.

INVARIANTE CRITICO: este router NAO importa nem chama app.services.whatsapp —
notas jamais saem para o cliente. Ha teste que conta chamadas ao provider.

Delete restrito ao AUTOR (403 para os demais) — sem papel de admin no
Conversas hoje, decisao registrada no vault.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_user, User
from app.models.conversation import Conversation
from app.models.note import ConversationNote
from app.schemas.conversation import NoteCreate, NoteResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/conversations", tags=["notes"])


def _get_conv(conversation_id: int, db: Session) -> Conversation:
    conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversa nao encontrada")
    return conv


@router.get("/{conversation_id}/notes", response_model=list[NoteResponse])
async def list_notes(
    conversation_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_conv(conversation_id, db)
    return (
        db.query(ConversationNote)
        .filter(ConversationNote.conversation_id == conversation_id)
        .order_by(ConversationNote.created_at)
        .all()
    )


@router.post("/{conversation_id}/notes", response_model=NoteResponse, status_code=201)
async def create_note(
    conversation_id: int,
    data: NoteCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_conv(conversation_id, db)
    note = ConversationNote(
        conversation_id=conversation_id,
        user_id=current_user.id,  # autor SEMPRE do token, nunca do request
        user_nome=getattr(current_user, "nome", None) or getattr(current_user, "email", None),
        content=data.content.strip(),
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    logger.info(f"Nota interna criada na conversa {conversation_id} pelo usuario {current_user.id}")
    return note


@router.delete("/{conversation_id}/notes/{note_id}", status_code=204)
async def delete_note(
    conversation_id: int,
    note_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    note = db.query(ConversationNote).filter(
        ConversationNote.id == note_id,
        ConversationNote.conversation_id == conversation_id,
    ).first()
    if not note:
        raise HTTPException(status_code=404, detail="Nota nao encontrada")
    if note.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Apenas o autor pode remover a nota")
    db.delete(note)
    db.commit()
