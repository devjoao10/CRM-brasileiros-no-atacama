import hashlib
import logging
from enum import Enum
from typing import Optional

from fastapi import Depends, HTTPException, status, Header, Request
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session
from sqlalchemy import Column, Integer, String, Boolean, DateTime
from sqlalchemy.sql import func

from app.config import SECRET_KEY, ALGORITHM
from app.database import Base, get_db


# ─── User Model (mirrors CRM's users table) ─────
class User(Base):
    """Espelho de LEITURA da tabela `users`, cujo DONO e o CRM (app/models/user.py).

    AUDIT-2026-08-orq — `users` e a unica tabela compartilhada pelos dois
    servicos, e este espelho entra no `Base.metadata` do Conversas, que roda
    `create_all()` no startup. Num banco onde o Conversas suba PRIMEIRO, quem
    cria a tabela e ESTE arquivo — e ele divergia do dono em quatro pontos:
    `nome` era String(200) contra String(100), faltava `email_verified` (que o
    CRM declara NOT NULL) e `api_key` nao tinha UNIQUE nem indice. O CRM
    quebrava no primeiro INSERT contra a tabela assim criada.

    As colunas abaixo agora batem com a declaracao do dono. Se o CRM mudar,
    ESTE arquivo muda junto — a fonte da verdade e app/models/user.py.

    `role` continua String(20) de proposito: o CRM usa `SAEnum(UserRole)`, que
    grava o NOME do membro ("ADMIN"), e declarar o mesmo enum aqui exigiria
    importar o enum do CRM (que este servico nao importa). A normalizacao de
    leitura vive em `is_admin_role()`, logo abaixo. Para ESCRITA vale a regra
    inversa e ela e obrigatoria: grave "ADMIN"/"USER" em caixa alta, senao a
    ORM do CRM levanta LookupError ao ler a linha.
    """
    __tablename__ = "users"
    __table_args__ = {'extend_existing': True}

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String(100), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(20), default="USER")
    is_active = Column(Boolean, default=True, nullable=False)
    email_verified = Column(Boolean, default=False, nullable=False)
    api_key = Column(String(255), unique=True, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


logger = logging.getLogger(__name__)

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
    """Extrai o usuario de um JWT — SO se ele for um token de sessao.

    AUDIT-2026-08-orq — este era o outro lado de um buraco que a wave do CRM
    fechou pela metade. Os dois servicos validam com a MESMA SECRET_KEY, e
    `app/routers/users.py` (CRM) emite um token de VERIFICACAO DE E-MAIL com essa
    chave, entregue na QUERY STRING de um link — ou seja, vazado para log de
    acesso, historico do navegador e `Referer`.

    O CRM passou a exigir `typ == "access"` e recusar esse token. Aqui nao havia
    checagem de proposito nenhuma: assinatura valida + `sub` presente bastava.
    Consequencia pratica ate esta linha existir: o link de verificacao de e-mail
    do CRM era uma sessao valida do Conversas, dando acesso ao inbox inteiro.

    Recusa por AUSENCIA do claim, nao por valor conhecido: token sem proposito
    declarado nao e sessao.
    """
    payload = decode_token(token)
    if payload is None:
        return None
    if payload.get("typ") != "access":
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

    # 2/3. JWT do header Authorization e JWT do cookie — nesta ordem, mas SEM
    #      curto-circuito entre eles.
    #
    #      AUDIT-2026-08-WG — este era o MESMO defeito que o CRM ja corrigiu
    #      (AUDIT-2026-08-W1A, `app/auth.py:198-218`), e que aqui continuava de
    #      pe: o `auth.js` do inbox anexa o token do localStorage a TODA
    #      request, entao UM valor obsoleto ali levantava 401 na API inteira —
    #      mesmo com o cookie `access_token` perfeitamente valido. O atendente
    #      era derrubado no meio do atendimento, sem ter feito nada, e "logar de
    #      novo" so resolvia porque reescrevia o localStorage. E o relato
    #      "usuarios que antes acessavam deixam de acessar".
    #
    #      Diferente da API Key (credencial explicita, sem ambiguidade, que
    #      continua falhando alto acima), o header Bearer aqui e MELHOR ESFORCO:
    #      so levantamos 401 depois de tentar TODOS os mecanismos. Nenhuma
    #      sessao invalida passa a ser aceita — o que muda e nao rejeitar uma
    #      sessao VALIDA por causa de uma credencial obsoleta ao lado dela.
    if token:
        user = _get_user_from_jwt(token, db)
        if user:
            return user

    cookie_token = request.cookies.get("access_token")
    if cookie_token:
        if cookie_token.startswith("Bearer "):
            cookie_token = cookie_token[7:]
        user = _get_user_from_jwt(cookie_token, db)
        if user:
            return user

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


# ─── Sessao das paginas HTML (AUDIT-2026-08-W1B — F2) ─────────────────
# O gate anterior (`pages.py::_require_cookie`) so olhava a PRESENCA do cookie:
# qualquer valor — expirado, adulterado, de usuario ja desativado — renderizava o
# shell do app. O JS entao chamava a API, tomava 401, redirecionava para /login,
# e o login.html via o token velho no localStorage e devolvia para "/" — o loop
# vivo que o CRM ja tinha diagnosticado como AUTH-LOOP-01 (app/auth.py:210-250).
# A correcao e ter UMA fonte de verdade: paginas e API resolvem o usuario pelo
# MESMO `_get_user_from_jwt` (assinatura, `exp`, existencia e `is_active`).


def require_page_session(request: Request, db: Session) -> Optional[User]:
    """Resolve o usuario da sessao de cookie das paginas HTML.

    Devolve None quando nao ha sessao utilizavel — o chamador responde com
    `page_login_redirect`. Aceita o cookie com ou sem o prefixo "Bearer ",
    exatamente como o passo 3 de `get_current_user`.
    """
    raw = request.cookies.get("access_token")
    if not raw:
        return None
    token = raw[7:] if raw.startswith("Bearer ") else raw
    return _get_user_from_jwt(token, db)


def page_login_redirect(request: Request) -> RedirectResponse:
    """302 para /login APAGANDO a credencial invalida (recuperacao automatica).

    Sem o delete, um cookie expirado/corrompido sobrevive as 8h inteiras no
    navegador e o estado inconsistente nunca se resolve sozinho.
    """
    response = RedirectResponse(url="/login", status_code=302)
    # ponytail: `decode_token` engole ExpiredSignatureError junto das demais
    # JWTError, entao expirado e adulterado logam o mesmo "invalid_session".
    had_cookie = bool(request.cookies.get("access_token"))
    if had_cookie:
        response.delete_cookie("access_token", path="/")
    logger.info(
        "AUTH_REDIRECT path=%s reason=%s",
        request.url.path,
        "invalid_session" if had_cookie else "missing_session",
    )
    return response
