from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime


# --- Auth Schemas ---

class LoginRequest(BaseModel):
    email: str = Field(..., description="Email do usuário")
    password: str = Field(..., description="Senha do usuário")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: "UserResponse"


class ApiKeyResponse(BaseModel):
    api_key: str
    message: str = "Use este token no header X-API-Key para integrações"


# --- User Schemas ---

class UserBase(BaseModel):
    nome: str = Field(..., min_length=2, max_length=100, description="Nome completo")
    email: str = Field(..., description="Email único")
    role: str = Field(default="user", description="Papel: admin ou user")


class UserCreate(UserBase):
    password: str = Field(..., min_length=6, description="Senha (mínimo 6 caracteres)")


class UserUpdate(BaseModel):
    nome: Optional[str] = Field(None, min_length=2, max_length=100)
    email: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = Field(None, min_length=6)


class UserResponse(BaseModel):
    id: int
    nome: str
    email: str
    role: str
    is_active: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class UserListResponse(BaseModel):
    total: int
    skip: int
    limit: int
    users: list[UserResponse]


# Resolve forward reference
TokenResponse.model_rebuild()
