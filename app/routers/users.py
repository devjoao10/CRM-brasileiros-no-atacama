from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Query, BackgroundTasks
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import IntegrityError
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

# AUDIT-2026-08-W2G (F12): todos os handlers deste router são `def` puros, não
# `async def`. Eles fazem I/O SÍNCRONO do SQLAlchemy; como `async` isso rodava
# no event loop e parava todas as outras requisições do worker. Sendo `def`, o
# FastAPI executa na threadpool — mesmo padrão de leads.py e pipeline.py.


@router.get("/for-select", summary="Lista usuários para selects (qualquer usuário autenticado)")
def users_for_select(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retorna lista simplificada de usuários ativos (id + nome) para uso em dropdowns."""
    users = db.query(User.id, User.nome).filter(User.is_active == True).order_by(User.nome).all()
    result = [{"id": 0, "nome": "Agente IA"}] + [{"id": u.id, "nome": u.nome} for u in users]
    return {"users": result}


@router.get("", response_model=UserListResponse, summary="Listar usuários")
def list_users(
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


# AUDIT-2026-08-W2G (F6): esta rota PRECISA ser declarada antes de
# GET /{user_id} (`user_id: int`). O Starlette casa na ordem de registro: com
# ela declarada depois, "verify-click" caía na rota parametrizada, a coerção
# para int falhava e o link do e-mail devolvia 422 em vez do HTML — o autor já
# tinha evitado exatamente isso ao declarar /for-select lá em cima.
@router.get("/verify-click", summary="Verifica clique no e-mail", response_class=HTMLResponse)
def verify_email_click(token: str, db: Session = Depends(get_db)):
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


@router.get("/{user_id}", response_model=UserResponse, summary="Detalhes de um usuário")
def get_user(
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
def create_user(
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
        email_verified=True  # Verificação por email desativada — ativar quando SMTP estiver configurado
    )
    db.add(user)
    # AUDIT-2026-08-W2G (F7): o check acima é só fast path. `users.email` tem
    # índice único no banco, então dois POSTs concorrentes (retry do n8n, dois
    # operadores) passavam os dois pelo check e o segundo estourava
    # IntegrityError -> 500 opaco, com a transação abortada e sem rollback.
    # O contrato documentado desta rota é 409.
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Já existe um usuário com este email"
        )
    db.refresh(user)

    # 3. Disparo Assíncrono do E-mail de Confirmação (Desativado temporariamente)
    token = create_access_token(data={"sub": user.email, "type": "verify_email"})
    # background_tasks.add_task(send_verification_email, user.email, token, is_lead=False)

    return UserResponse.model_validate(user)


@router.put("/{user_id}", response_model=UserResponse, summary="Atualizar usuário")
def update_user(
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

    # AUDIT-2026-08-W2G (F5): o `delete_user` proíbe explicitamente desativar a
    # própria conta, mas a MESMA ação era alcançável por este verbo, que aplicava
    # `is_active` e `role` do corpo sem nenhuma checagem. E nada impedia demitir
    # o último admin ativo — depois disso ninguém mais entra em /api/users,
    # /api/teams nem no relatório, e a recuperação exige mexer no banco.
    # Último admin primeiro: é a causa mais grave (ninguém mais administra o
    # sistema) e dá a mensagem certa quando o alvo é o próprio requisitante.
    perde_admin = ("role" in update_data and update_data["role"] != UserRole.ADMIN)         or update_data.get("is_active") is False
    if user.role == UserRole.ADMIN and user.is_active and perde_admin:
        outros_admins = db.query(User).filter(
            User.role == UserRole.ADMIN,
            User.is_active == True,  # noqa: E712 — comparação de coluna, não de Python
            User.id != user.id,
        ).count()
        if outros_admins == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Não é possível rebaixar ou desativar o último administrador ativo"
            )

    if user.id == current_user.id:
        if update_data.get("is_active") is False:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Você não pode desativar sua própria conta"
            )
        if "role" in update_data and update_data["role"] != user.role:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Você não pode alterar o seu próprio papel"
            )

    for field, value in update_data.items():
        setattr(user, field, value)

    db.commit()
    db.refresh(user)

    return UserResponse.model_validate(user)


@router.delete("/{user_id}", summary="Desativar usuário")
def delete_user(
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
def verify_user_email(
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
