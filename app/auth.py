import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status, Header, Request
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.config import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES
from app.database import get_db
from app.models.user import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)


def hash_password(password: str) -> str:
    """Hash a password using bcrypt."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash."""
    return pwd_context.verify(plain_password, hashed_password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create a JWT access token."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[dict]:
    """Decode and validate a JWT token."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


def generate_api_key() -> str:
    """Generate a secure API key for N8N integrations."""
    return f"bna_{secrets.token_urlsafe(48)}"


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
    user = db.query(User).filter(User.api_key == api_key).first()
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
    Unified authentication dependency.
    Accepts either:
    - JWT token via Authorization header or cookie
    - API Key via X-API-Key header (for N8N)
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
        # Remove "Bearer " prefix if present
        if cookie_token.startswith("Bearer "):
            cookie_token = cookie_token[7:]
        user = _get_user_from_jwt(cookie_token, db)
        if user:
            return user
        raise credentials_exception

    raise credentials_exception


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Dependency that requires admin role."""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Acesso restrito a administradores"
        )
    return current_user
