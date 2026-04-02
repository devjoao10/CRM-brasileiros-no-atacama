from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User, UserRole
from app.schemas.user import (
    UserCreate,
    UserUpdate,
    UserResponse,
    UserListResponse,
)
from app.auth import get_current_user, require_admin, hash_password

router = APIRouter(prefix="/api/users", tags=["Usuários"])


@router.get("", response_model=UserListResponse, summary="Listar usuários")
async def list_users(
    skip: int = Query(0, ge=0, description="Registros para pular"),
    limit: int = Query(100, ge=1, le=500, description="Máximo de registros"),
    search: Optional[str] = Query(None, description="Busca por nome ou email"),
    role: Optional[str] = Query(None, description="Filtrar por role: admin ou user"),
    is_active: Optional[bool] = Query(None, description="Filtrar por status ativo"),
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Lista todos os usuários com paginação e filtros.
    
    **N8N**: Use `skip` e `limit` para paginação.
    Filtre por `search`, `role` e `is_active`.
    """
    query = db.query(User)

    if search:
        search_filter = f"%{search}%"
        query = query.filter(
            (User.nome.ilike(search_filter)) | (User.email.ilike(search_filter))
        )
    if role:
        query = query.filter(User.role == role)
    if is_active is not None:
        query = query.filter(User.is_active == is_active)

    total = query.count()
    users = query.offset(skip).limit(limit).all()

    return UserListResponse(
        total=total,
        skip=skip,
        limit=limit,
        users=[UserResponse.model_validate(u) for u in users],
    )


@router.get("/{user_id}", response_model=UserResponse, summary="Detalhes de um usuário")
async def get_user(
    user_id: int,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Retorna os dados de um usuário específico pelo ID."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    return UserResponse.model_validate(user)


@router.post("", response_model=UserResponse, status_code=201, summary="Criar usuário")
async def create_user(
    data: UserCreate,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Cria um novo usuário no sistema.
    
    **N8N**: Útil para criar usuários automaticamente a partir de formulários externos.
    """
    # Check duplicate email
    existing = db.query(User).filter(User.email == data.email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Já existe um usuário com este email"
        )

    user = User(
        nome=data.nome,
        email=data.email,
        hashed_password=hash_password(data.password),
        role=data.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return UserResponse.model_validate(user)


@router.put("/{user_id}", response_model=UserResponse, summary="Atualizar usuário")
async def update_user(
    user_id: int,
    data: UserUpdate,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Atualiza os dados de um usuário.
    Envie apenas os campos que deseja alterar.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    update_data = data.model_dump(exclude_unset=True)

    if "password" in update_data:
        update_data["hashed_password"] = hash_password(update_data.pop("password"))

    if "email" in update_data and update_data["email"] != user.email:
        existing = db.query(User).filter(User.email == update_data["email"]).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Já existe um usuário com este email"
            )

    for field, value in update_data.items():
        setattr(user, field, value)

    db.commit()
    db.refresh(user)

    return UserResponse.model_validate(user)


@router.delete("/{user_id}", summary="Desativar usuário")
async def delete_user(
    user_id: int,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Desativa um usuário (soft delete).
    O usuário não será removido do banco mas não poderá mais acessar o sistema.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")

    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Você não pode desativar sua própria conta"
        )

    user.is_active = False
    user.api_key = None  # Revoke API Key
    db.commit()

    return {"message": f"Usuário {user.email} desativado"}


@router.post("/{user_id}/verify-email", summary="Confirmar email do usuário")
async def verify_user_email(
    user_id: int,
    db: Session = Depends(get_db),
    # Optional: could be protected by API Key / Admin
    # current_user: User = Depends(require_admin)
):
    """
    Marca o email do usuário como verificado.
    Aberto para webhook via requisição externa (n8n).
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    
    user.email_verified = True
    db.commit()
    db.refresh(user)
    
    return {"message": "Email verificado com sucesso", "email": user.email, "email_verified": True}
