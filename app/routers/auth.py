from fastapi import APIRouter, Depends, HTTPException, status, Response, Request
from sqlalchemy.orm import Session

from app.config import ENVIRONMENT, ACCESS_TOKEN_EXPIRE_MINUTES
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

from app.limiter import limiter  # instância única (WP-SEC-03)

router = APIRouter(prefix="/api/auth", tags=["Autenticação"])


@router.post("/login", response_model=TokenResponse, summary="Login com email e senha")
@limiter.limit("5/minute")
async def login(request: Request, data: LoginRequest, response: Response, db: Session = Depends(get_db)):
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
    # if not user.email_verified:
    #     raise HTTPException(
    #         status_code=status.HTTP_403_FORBIDDEN,
    #         detail="Por favor, verifique seu e-mail para ativar a conta."
    #     )

    access_token = create_access_token(data={"sub": user.email, "role": user.role})

    # Set cookie for frontend
    # AUDIT-2026-08-W1A: a flag Secure agora FALHA FECHADO. Antes, o teste era
    # `os.getenv("ENVIRONMENT") == "production"` lido direto do ambiente: um
    # valor tipográfico como "prod" ou "Production" fazia o cookie de sessão
    # sair SEM Secure em produção, trafegando em claro. Só "development" (o
    # valor canônico de dev, vindo de app.config) libera o cookie sem Secure.
    # O max_age passa a derivar de ACCESS_TOKEN_EXPIRE_MINUTES: com 28800
    # cravado, mudar o tempo de vida do JWT deixava cookie e token divergentes.
    secure = ENVIRONMENT != "development"
    response.set_cookie(
        key="access_token",
        value=f"Bearer {access_token}",
        httponly=True,
        secure=secure,  # HTTPS only fora de dev
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        samesite="lax",
        path="/",  # Garante que o cookie é enviado em todas as rotas
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
    new_key, hashed_key = generate_api_key()
    current_user.api_key = hashed_key  # Armazena o hash, não a key
    db.commit()

    return ApiKeyResponse(api_key=new_key)  # Mostra a key só uma vez


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
async def logout(response: Response, current_user: User = Depends(get_current_user)):
    """Remove o cookie de autenticação (frontend).

    AUDIT-2026-08-W1A: exige sessão válida. Sem a dependência, a rota era um
    endpoint anônimo — qualquer origem conseguia disparar o Set-Cookie de
    remoção e o logout não era atribuível a ninguém no log.
    NOTA: isto NÃO revoga o JWT — quem já tem o token continua podendo usá-lo
    até o `exp`. Revogação de verdade precisa de coluna nova no banco
    (ex.: `tokens_valid_after`) e está FORA DO ESCOPO desta wave.
    """
    response.delete_cookie("access_token", path="/")
    return {"message": "Logout realizado"}
