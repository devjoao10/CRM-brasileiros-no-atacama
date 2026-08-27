"""
Auth router — DEV ONLY.
Provides a local /api/auth/login so the Conversas app can be tested
independently, without needing the CRM running on port 8000.
In production, authentication goes through the CRM API.
"""

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
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
    """Confere contra hash SHA-256. SO EXISTE PARA DEV — ver `login`.

    `compare_digest` no lugar de `==`: comparacao de string em Python sai no
    primeiro byte diferente, e o tempo da resposta vaza quanto do hash bate.
    """
    return hmac.compare_digest(_hash_password(plain), hashed or "")


def _create_token(email: str) -> str:
    """Cria o JWT de sessao do Conversas.

    AUDIT-2026-08-orq: carimba `typ: "access"` igual ao CRM
    (app/auth.py::create_access_token). Os dois servicos assinam com a MESMA
    SECRET_KEY, entao o claim so vale se as DUAS pontas o exigirem — ver
    `_get_user_from_jwt` em conversas/app/auth.py.
    """
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(
        {"sub": email, "typ": "access", "exp": expire},
        SECRET_KEY, algorithm=ALGORITHM,
    )


import httpx
import os

from app.seed import CONVERSAS_SEED_DEV_DATA

CRM_BASE_URL = os.getenv("CRM_BASE_URL", "http://crm:8000")


def _forwarded_for_headers(request: Request) -> dict:
    """Cabecalhos do salto Conversas -> CRM que carregam o IP real de quem pediu.

    AUDIT-2026-08-WF2 (1): o login abaixo e um PROXY servidor-a-servidor. O CRM
    limita `/api/auth/login` em 5/minute com `key_func=get_remote_address`
    (app/limiter.py), e nesse salto o `remote_address` e SEMPRE o container do
    Conversas — ou seja, todos os atendentes dividiam UM balde de 5/min. Cinco
    tentativas com credencial lixo por minuto, sem conta e sem autenticacao, e o
    429 do CRM era repassado a todo mundo: ninguem mais entrava no inbox.

    REPASSAR a cadeia, nao SOBRESCREVER. O CRM sobe `uvicorn --proxy-headers
    --forwarded-allow-ips=*` (Dockerfile, AUDIT-2026-08-W1E/F10) e nesse modo o
    `ProxyHeadersMiddleware` chaveia no item MAIS A ESQUERDA de X-Forwarded-For.
    Entao:

      - repassar o cabecalho que veio do Traefik mantem o cliente original nessa
        ponta esquerda, que e exatamente a chave desejada;
      - sobrescrever com `request.client.host` daria o mesmo resultado SO
        enquanto o uvicorn do Conversas tambem estivesse com `--proxy-headers`.
        No dia em que nao estiver (execucao local, override do compose, outro
        entrypoint) `request.client.host` vira o IP do Traefik, e a sobrescrita
        descartaria justamente o cabecalho onde estava o cliente real — o balde
        compartilhado voltaria com a correcao parecendo aplicada.

    Tambem NAO acrescentamos um salto nosso a cadeia: o `--proxy-headers` do
    Conversas ja trocou `scope["client"]` pelo primeiro item dela, entao anexar
    `request.client.host` duplicaria esse valor sem nomear salto algum. Sem
    cadeia (chamada direta, dev, teste) sintetizamos a partir do peer.

    Isto NAO amplia a superficie de spoofing: confiar no X-Forwarded-For que
    chega e a postura ja escolhida em F10 para os dois servicos, e o mesmo
    cabecalho vale batendo direto no `/api/auth/login` do CRM pelo Traefik.
    """
    cadeia = request.headers.get("x-forwarded-for", "").strip()
    if not cadeia and request.client:
        cadeia = request.client.host
    # Cabecalho com valor vazio e pior que cabecalho ausente: nao mandamos.
    return {"X-Forwarded-For": cadeia} if cadeia else {}


@router.post("/login")
async def login(
    data: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """
    CONV-BF-AUTH-01:
    - DEV LOCAL (CONVERSAS_SEED_DEV_DATA=true): autentica na tabela `users`
      LOCAL do Conversas — sem chamar o CRM (era o bug: o proxy sempre rodava
      e devolvia 503 sem o CRM de pe, tornando o app intestavel isolado).
    - PRODUCAO (flag false): comportamento ORIGINAL preservado — proxy ao CRM.
    Nunca logar senha/token; 401 uniforme (nao revela se o email existe).
    """
    # AUDIT-2026-08-orq: o portao era SO `CONVERSAS_SEED_DEV_DATA` — uma flag
    # de SEED. Quem a ligasse em producao para popular dados de demonstracao
    # trocaria, junto e sem perceber, TODA a autenticacao deste servico: de
    # proxy ao CRM (bcrypt, via passlib) para SHA-256 SEM SAL conferido
    # diretamente contra `users.hashed_password`, a coluna compartilhada. E o
    # seed gravaria um hash sem sal na tabela de producao. Uma flag de DADOS nao
    # pode decidir o esquema de senha: o ambiente decide, e a flag so escolhe se
    # ha dados de dev. Fora de development, o unico caminho e o CRM.
    if CONVERSAS_SEED_DEV_DATA and ENVIRONMENT == "development":
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
                # AUDIT-2026-08-WF2 (1) — sem isto o CRM limita 5/min POR
                # CONTAINER, e nao por cliente. Ver `_forwarded_for_headers`.
                headers=_forwarded_for_headers(request),
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
