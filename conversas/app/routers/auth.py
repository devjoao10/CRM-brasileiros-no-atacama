"""
Auth router — DEV ONLY.
Provides a local /api/auth/login so the Conversas app can be tested
independently, without needing the CRM running on port 8000.
In production, authentication goes through the CRM API.
"""

import hashlib
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from jose import jwt
from sqlalchemy.orm import Session

from app.config import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES, ENVIRONMENT
from app.database import get_db
from app.auth import User, get_current_user

router = APIRouter(prefix="/api/auth", tags=["Auth (Dev)"])

# AUDIT-2026-08-W1B — F3: o cookie de sessao era escrito pelo PROPRIO JS do
# login.html (`document.cookie = ...`), o que o torna estruturalmente legivel por
# qualquer script da pagina (nao pode ser HttpOnly) e o mandava sem `Secure` —
# viajava em claro em qualquer downgrade para http. Agora quem emite o cookie e o
# servidor, igual ao CRM (app/routers/auth.py). `secure` acompanha o ambiente
# porque em dev local nao ha TLS e o cookie seria descartado pelo navegador.
_COOKIE_SECURE = ENVIRONMENT != "development"


def _set_session_cookie(response: Response, token: str) -> None:
    """Grava o cookie de sessao HttpOnly. Mesmo formato lido por `get_current_user`."""
    response.set_cookie(
        key="access_token",
        value=f"Bearer {token}",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        httponly=True,
        secure=_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


class LoginRequest(BaseModel):
    email: str
    password: str


class UserResponse(BaseModel):
    id: int
    nome: str
    email: str
    role: str
    is_active: bool

    class Config:
        from_attributes = True


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserResponse


def _hash_password(password: str) -> str:
    """Simple SHA-256 hash for dev purposes."""
    return hashlib.sha256(password.encode()).hexdigest()


def _verify_password(plain: str, hashed: str) -> bool:
    """Verify against SHA-256 hash."""
    return _hash_password(plain) == hashed


def _create_token(email: str) -> str:
    """Create a JWT token."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": email, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


import httpx
import os

from app.seed import CONVERSAS_SEED_DEV_DATA

CRM_BASE_URL = os.getenv("CRM_BASE_URL", "http://crm:8000")


@router.post("/login")
async def login(data: LoginRequest, response: Response, db: Session = Depends(get_db)):
    """
    CONV-BF-AUTH-01:
    - DEV LOCAL (CONVERSAS_SEED_DEV_DATA=true): autentica na tabela `users`
      LOCAL do Conversas — sem chamar o CRM (era o bug: o proxy sempre rodava
      e devolvia 503 sem o CRM de pe, tornando o app intestavel isolado).
    - PRODUCAO (flag false): comportamento ORIGINAL preservado — proxy ao CRM.
    Nunca logar senha/token; 401 uniforme (nao revela se o email existe).
    """
    if CONVERSAS_SEED_DEV_DATA:
        # ── Autenticacao LOCAL (dev) — nao toca CRM_BASE_URL ──
        user = db.query(User).filter(User.email == data.email).first()
        if not user or not user.is_active or not _verify_password(data.password, user.hashed_password):
            # detail uniforme: inexistente, inativo e senha errada respondem igual
            raise HTTPException(status_code=401, detail="Email ou senha incorretos")
        token = _create_token(user.email)
        _set_session_cookie(response, token)  # AUDIT-2026-08-W1B — F3
        return TokenResponse(
            access_token=token,
            user=UserResponse.model_validate(user),
        )

    # ── Producao: repassa a requisicao para a API do CRM (inalterado) ──
    # `crm_response` (antes `response`) foi renomeada: `response` agora e o
    # objeto de resposta do FastAPI usado para gravar o cookie de sessao.
    async with httpx.AsyncClient() as client:
        try:
            crm_response = await client.post(
                f"{CRM_BASE_URL}/api/auth/login",
                json={"email": data.email, "password": data.password},
                timeout=10.0
            )

            if crm_response.status_code != 200:
                # Repassa o erro do CRM
                detail = crm_response.json().get("detail", "Email ou senha incorretos")
                raise HTTPException(status_code=crm_response.status_code, detail=detail)

            payload = crm_response.json()
            # O token vem do CRM, mas quem sela o cookie e este servico — o
            # navegador so fala com o dominio do Conversas (AUDIT-2026-08-W1B — F3).
            crm_token = payload.get("access_token")
            if crm_token:
                _set_session_cookie(response, crm_token)
            return payload

        except httpx.RequestError:
            raise HTTPException(
                status_code=503,
                detail="Serviço de autenticação temporariamente indisponível."
            )


@router.post("/logout")
async def logout(response: Response):
    """Encerra a sessao apagando o cookie.

    AUDIT-2026-08-W1B — F4: nao existia logout no servidor. O `clearAuth()` do
    frontend so limpava o localStorage, entao o cookie de sessao sobrevivia as 8h
    inteiras e continuava valendo como credencial — "sair" nao deslogava ninguem.
    Espelha app/routers/auth.py:102-106.
    """
    response.delete_cookie("access_token", path="/")
    return {"message": "Logout realizado"}


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(
    current_user: User = Depends(get_current_user),
):
    """Valida o token e retorna o usuário atual.

    AUDIT-2026-08-W1B — F6: antes retornava `{"detail": "Use Authorization header"}`
    com `response_model=UserResponse`, ou seja, 500 em 100% das chamadas (falha de
    validacao da resposta). Agora resolve a identidade de verdade, como o CRM.
    """
    return UserResponse.model_validate(current_user)


from fastapi import Header

@router.get("/verify")
async def verify_token(
    authorization: str = Header(None),
    db: Session = Depends(get_db),
):
    """Verify if a JWT token is valid. Returns user data if valid, 401 if not."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Token ausente")

    token = authorization.replace("Bearer ", "")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub")
        if not email:
            raise HTTPException(status_code=401, detail="Token inválido")

        user = db.query(User).filter(User.email == email).first()
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="Usuário não encontrado")

        return UserResponse.model_validate(user)
    except Exception:
        raise HTTPException(status_code=401, detail="Token expirado ou inválido")


@router.get("/me/validate")
async def validate_token(current_user: User = Depends(get_current_user)):
    """Valida a credencial da requisicao e devolve a identidade resolvida.

    AUDIT-2026-08-W1B — F5: a versao anterior nao recebia argumento algum, nao lia
    header nem cookie e devolvia `{"valid": true}` para QUALQUER chamador — apesar
    da docstring prometer validacao. Hoje nada consome a rota, mas ela era uma
    armadilha pronta para a proxima integracao que confiasse no nome. Passa a usar
    o mesmo `get_current_user` das demais rotas (401 sem credencial valida).
    """
    return {
        "valid": True,
        "id": current_user.id,
        "email": current_user.email,
        "role": current_user.role,
    }
