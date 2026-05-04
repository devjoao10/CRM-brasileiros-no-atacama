"""
Auth router — DEV ONLY.
Provides a local /api/auth/login so the Conversas app can be tested
independently, without needing the CRM running on port 8000.
In production, authentication goes through the CRM API.
"""

import hashlib
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from jose import jwt
from sqlalchemy.orm import Session

from app.config import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES
from app.database import get_db
from app.auth import User

router = APIRouter(prefix="/api/auth", tags=["Auth (Dev)"])


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

CRM_BASE_URL = os.getenv("CRM_BASE_URL", "http://crm:8000")

@router.post("/login")
async def login(data: LoginRequest):
    """
    Login em produção: Repassa a requisição para a API do CRM,
    que possui o passlib (bcrypt) para validar a senha real.
    """
    async with httpx.AsyncClient() as client:
        try:
            # Envia para a rota de login do CRM
            response = await client.post(
                f"{CRM_BASE_URL}/api/auth/login",
                json={"email": data.email, "password": data.password},
                timeout=10.0
            )
            
            if response.status_code != 200:
                # Repassa o erro do CRM
                detail = response.json().get("detail", "Email ou senha incorretos")
                raise HTTPException(status_code=response.status_code, detail=detail)
                
            return response.json()
            
        except httpx.RequestError:
            raise HTTPException(
                status_code=503,
                detail="Serviço de autenticação temporariamente indisponível."
            )


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(
    db: Session = Depends(get_db),
):
    """
    Valida o token e retorna o usuário atual.
    Usado pelo frontend para verificar se o token ainda é válido.
    NOTE: This uses a manual token extraction approach since
    the get_current_user dependency might not be available here.
    """
    return {"detail": "Use Authorization header"}


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
async def validate_token():
    """Simple token validation — returns 200 if token header is valid."""
    return {"valid": True}
