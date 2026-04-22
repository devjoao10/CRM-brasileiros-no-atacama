from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Query, BackgroundTasks
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from email_validator import validate_email, EmailNotValidError

from app.database import get_db
from app.models.user import User, UserRole
from app.schemas.user import (
    UserCreate,
    UserUpdate,
    UserResponse,
    UserListResponse,
)
from app.auth import get_current_user, require_admin, hash_password, create_access_token, decode_token
# from app.services.mail_service import send_verification_email

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
    Cria um novo usuário no sistema com Validação Silenciosa de DNS e disparo de Double Opt-In.
    """
    # 1. Validação Silenciosa de Email (DNS/MX records)
    try:
        email_info = validate_email(data.email, check_deliverability=True)
        data.email = email_info.normalized
    except EmailNotValidError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"E-mail inválido ou inexistente: {str(e)}"
        )

    # 2. Check duplicate email
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
        email_verified=False  # Bloqueia login até clique no email
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # 3. Disparo Assíncrono do E-mail de Confirmação (Desativado temporariamente)
    token = create_access_token(data={"sub": user.email, "type": "verify_email"})
    # background_tasks.add_task(send_verification_email, user.email, token, is_lead=False)

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


@router.post("/{user_id}/verify-email", summary="Confirmar email do usuário via API")
async def verify_user_email(
    user_id: int,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Confirma o email de um usuário. Requer autenticação (JWT ou API Key do N8N).
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado")
    
    user.email_verified = True
    db.commit()
    db.refresh(user)
    
    return {"message": "Email verificado com sucesso", "email": user.email, "email_verified": True}


@router.get("/verify-click", summary="Verifica clique no e-mail", response_class=HTMLResponse)
async def verify_email_click(token: str, db: Session = Depends(get_db)):
    """Recebe o token do link enviado por email e libera o acesso do usuário."""
    payload = decode_token(token)
    if not payload or payload.get("type") != "verify_email":
        return HTMLResponse("<h1>Link de verificação inválido ou expirado.</h1>", status_code=400)
        
    email = payload.get("sub")
    user = db.query(User).filter(User.email == email).first()
    if not user:
        return HTMLResponse("<h1>Usuário não encontrado.</h1>", status_code=404)
        
    user.email_verified = True
    db.commit()
    
    return HTMLResponse('''
        <html>
        <body style="display: flex; justify-content: center; align-items: center; height: 100vh; background-color: #f3f4f6; font-family: sans-serif;">
            <div style="background: white; padding: 40px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); text-align: center;">
                <h1 style="color: #10b981;">✅ E-mail Verificado!</h1>
                <p style="font-size: 18px; color: #4b5563;">Sua conta no CRM Brasileiros no Atacama foi ativada com sucesso.</p>
                <a href="/" style="display: inline-block; margin-top: 20px; padding: 10px 20px; background-color: #3b82f6; color: white; text-decoration: none; border-radius: 5px;">Acessar o Painel</a>
            </div>
        </body>
        </html>
    ''')
