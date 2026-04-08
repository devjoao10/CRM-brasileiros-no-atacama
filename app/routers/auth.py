from fastapi import APIRouter, Depends, HTTPException, status, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.schemas.user import LoginRequest, TokenResponse, ApiKeyResponse, UserResponse
from app.auth import (
    verify_password,
    create_access_token,
    generate_api_key,
    get_current_user,
    hash_password,
)

router = APIRouter(prefix="/api/auth", tags=["Autenticação"])


@router.post("/login", response_model=TokenResponse, summary="Login com email e senha")
async def login(data: LoginRequest, response: Response, db: Session = Depends(get_db)):
    """
    Autentica o usuário e retorna um JWT token.
    
    **Uso no N8N**: Use este endpoint para obter um token JWT,
    ou prefira gerar uma API Key com `POST /api/auth/token`.
    """
    user = db.query(User).filter(User.email == data.email).first()
    if not user or not verify_password(data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email ou senha incorretos"
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Conta desativada"
        )
    if not user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Por favor, verifique seu e-mail para ativar a conta."
        )

    access_token = create_access_token(data={"sub": user.email})

    # Set cookie for frontend
    response.set_cookie(
        key="access_token",
        value=f"Bearer {access_token}",
        httponly=True,
        max_age=28800,  # 8 hours
        samesite="lax"
    )

    return TokenResponse(
        access_token=access_token,
        user=UserResponse.model_validate(user)
    )


@router.post("/token", response_model=ApiKeyResponse, summary="Gerar API Key para N8N")
async def generate_api_key_endpoint(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Gera uma API Key para uso em integrações externas (N8N, automações).
    
    Use a key no header `X-API-Key` em todas as requisições.
    A key anterior será substituída.
    """
    new_key = generate_api_key()
    current_user.api_key = new_key
    db.commit()

    return ApiKeyResponse(api_key=new_key)


@router.get("/me", response_model=UserResponse, summary="Dados do usuário logado")
async def get_me(current_user: User = Depends(get_current_user)):
    """Retorna os dados do usuário autenticado (funciona com JWT ou API Key)."""
    return UserResponse.model_validate(current_user)


@router.delete("/apikey", summary="Revogar API Key")
async def revoke_api_key(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Revoga a API Key do usuário atual. Integrações N8N pararão de funcionar."""
    current_user.api_key = None
    db.commit()
    return {"message": "API Key revogada com sucesso"}


@router.post("/logout", summary="Logout")
async def logout(response: Response):
    """Remove o cookie de autenticação (frontend)."""
    response.delete_cookie("access_token")
    return {"message": "Logout realizado"}
