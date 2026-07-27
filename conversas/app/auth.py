import hashlib
from enum import Enum
from typing import Optional

from fastapi import Depends, HTTPException, status, Header, Request
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.sql import func

from app.config import SECRET_KEY, ALGORITHM
from app.database import Base, get_db


# ─── User Model (mirrors CRM's users table) ─────
class User(Base):
    """User model — reads from the SAME users table as the CRM."""
    __tablename__ = "users"
    __table_args__ = {'extend_existing': True}

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String(200), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(20), default="user")
    is_active = Column(Boolean, default=True)
    api_key = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def decode_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT token."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


def hash_api_key(api_key: str) -> str:
    """Hash an API key with SHA-256."""
    return hashlib.sha256(api_key.encode()).hexdigest()


def _get_user_from_jwt(token: str, db: Session) -> Optional[User]:
    """Extract user from JWT token."""
    payload = decode_token(token)
    if payload is None:
        return None
    email: str = payload.get("sub")
    if email is None:
        return None
    user = db.query(User).filter(User.email == email).first()
    if user and user.is_active:
        return user
    return None


def _get_user_from_api_key(api_key: str, db: Session) -> Optional[User]:
    """Extract user from API Key (for N8N)."""
    hashed = hash_api_key(api_key)
    user = db.query(User).filter(User.api_key == hashed).first()
    if user and user.is_active:
        return user
    return None


async def get_current_user(
    request: Request,
    token: Optional[str] = Depends(oauth2_scheme),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> User:
    """
    Unified authentication — same logic as CRM.
    Accepts JWT (header/cookie) or API Key.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Credenciais inválidas",
        headers={"WWW-Authenticate": "Bearer"},
    )

    # 1. Try API Key first (N8N integration)
    if x_api_key:
        user = _get_user_from_api_key(x_api_key, db)
        if user:
            return user
        raise credentials_exception

    # 2. Try JWT from Authorization header
    if token:
        user = _get_user_from_jwt(token, db)
        if user:
            return user
        raise credentials_exception

    # 3. Try JWT from cookie (frontend)
    cookie_token = request.cookies.get("access_token")
    if cookie_token:
        if cookie_token.startswith("Bearer "):
            cookie_token = cookie_token[7:]
        user = _get_user_from_jwt(cookie_token, db)
        if user:
            return user
        raise credentials_exception

    raise credentials_exception


# CONV-VAR-01-HOTFIX-ADMIN-01
# Papel administrativo oficial. Comparacao SEMPRE via `is_admin_role()`.
ADMIN_ROLE = "admin"


def is_admin_role(role) -> bool:
    """
    Normalizacao CENTRAL do papel administrativo.

    Causa raiz do 403 indevido: o CRM declara `users.role` como
    `SAEnum(UserRole)` e o SQLAlchemy grava na coluna o NOME do membro
    ("ADMIN"), nao o `value` ("admin"). O Conversas espelha a MESMA tabela
    declarando `role = Column(String(20))`, entao le a string CRUA "ADMIN" —
    e a comparacao literal `role != "admin"` negava acesso a administradores
    reais. No CRM o mesmo codigo funciona porque o SAEnum reconverte para
    `UserRole.ADMIN`, que e subclasse de `str` com valor "admin".

    Aceita, portanto: "ADMIN", "admin", ou enum cujo `.value` seja qualquer
    uma das duas formas (o `.value` e extraido ANTES da comparacao, senao um
    enum comum viraria "UserRole.ADMIN" no `str()`).

    NAO amplia para nenhum outro papel: a comparacao continua sendo de
    igualdade exata com "admin" apos normalizar caixa e espacos. MANAGER,
    SELLER, USER, vazio e None seguem fora.

    O desempacotamento usa `isinstance(role, Enum)` em vez de
    `getattr(role, "value", role)`: um objeto NAO-enum que exponha um atributo
    `.value` valendo "admin" seria promovido a administrador pela forma com
    getattr. Hoje isso e inalcancavel (a coluna e String, entao chega `str` ou
    `None`), mas em um guard de autorizacao nao se deixa caminho de escalonamento
    aberto por acaso.
    """
    raw = role.value if isinstance(role, Enum) else role
    return str(raw).strip().lower() == ADMIN_ROLE


async def require_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """Require an authenticated admin user. Returns 403 if not admin."""
    if not is_admin_role(current_user.role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acesso restrito a administradores",
        )
    return current_user
