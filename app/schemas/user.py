from pydantic import BaseModel, EmailStr, Field
from typing import Optional, List
from datetime import datetime
from app.models.user import UserRole
from app.schemas.team import TeamResponse


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
    """AUDIT-2026-08-W2G — dois campos que travavam contas inteiras.

    `email`: era `str` puro e o formato só era conferido no `create_user`; o
    `EmailStr` já importado neste arquivo nunca era usado. Como o e-mail vira o
    `sub` do JWT (`app/routers/auth.py`), um admin salvando "nope" derrubava
    todos os caminhos de login daquela conta. Agora o 422 vem do Pydantic, e
    vale para create E update.

    `role`: era `str` livre contra uma coluna `Enum(UserRole)`. No SQLite o
    valor desconhecido COMMITA (SQLAlchemy 2.0 usa create_constraint=False) e
    toda leitura ORM daquela linha passa a levantar LookupError — conta
    irrecuperável pela UI; no PostgreSQL vira DataError/500. Tipando como
    `UserRole`, "superadmin" morre no 422 antes de tocar o banco.
    """
    nome: str = Field(..., min_length=2, max_length=100, description="Nome completo")
    email: EmailStr = Field(..., description="Email único")
    role: UserRole = Field(default=UserRole.USER, description="Papel: admin ou user")


class UserCreate(UserBase):
    password: str = Field(..., min_length=6, description="Senha (mínimo 6 caracteres)")


class UserUpdate(BaseModel):
    # AUDIT-2026-08-W2G: mesmas garantias do UserBase no caminho de update —
    # era exatamente por aqui que o formato de e-mail e o role arbitrário
    # passavam sem nenhuma validação.
    nome: Optional[str] = Field(None, min_length=2, max_length=100)
    email: Optional[EmailStr] = None
    role: Optional[UserRole] = None
    is_active: Optional[bool] = None
    password: Optional[str] = Field(None, min_length=6)


class UserResponse(BaseModel):
    id: int
    nome: str
    email: str
    role: str
    is_active: bool
    email_verified: bool = False
    teams: List[TeamResponse] = []
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
