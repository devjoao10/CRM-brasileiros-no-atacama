from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List

from app.database import get_db
from app.auth import get_current_user
from app.models.user import User
from app.schemas.operational import (
    CommentCreate,
    CommentResponse,
    MentionResponse,
)
from app.services.operational_comment_service import OperationalCommentService

router = APIRouter(
    prefix="/api/operational",
    tags=["Operational Comments & Mentions"]
)


@router.get("/cards/{card_id}/comments", response_model=List[CommentResponse])
def list_comments_by_card(
    card_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalCommentService(db)
    try:
        return service.list_comments_by_card(card_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao listar comentários do card"
        )


@router.post("/cards/{card_id}/comments", response_model=CommentResponse, status_code=status.HTTP_201_CREATED)
def create_comment(
    card_id: int,
    comment_data: CommentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalCommentService(db)
    try:
        data_dict = comment_data.model_dump(exclude_unset=True)
        # Força o card_id vindo do path parameter
        data_dict["card_id"] = card_id
        return service.create_comment(card_id, data_dict, current_user)
    except ValueError as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro inesperado ao criar comentário"
        )


@router.get("/comments/{comment_id}/mentions", response_model=List[MentionResponse])
def list_mentions_by_comment(
    comment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    service = OperationalCommentService(db)
    try:
        return service.list_mentions_by_comment(comment_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Erro ao listar menções do comentário"
        )
