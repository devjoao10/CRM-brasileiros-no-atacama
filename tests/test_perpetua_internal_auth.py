"""
PERPETUA-INTERNAL-AUTH-01 — regressão de autenticação da Perpétua.

Prova, em processo (TestClient + SQLite descartável em scratch/), que:
  1. Auth por X-API-Key (n8n/integrações) continua funcionando.
  2. Auth por JWT/cookie (frontend) continua funcionando.
  3. As ferramentas internas da IA autenticam SEM current_user.api_key
     (via HMAC interno) — todo usuário logado pode usar a Perpétua.
  4. INTERNAL_AI_AUTH_SECRET ausente falha com segurança (401 no auth,
     erro seguro em call_internal_api — nunca pede "API Key").
  5. Assinatura interna inválida é rejeitada (401).
  6. Timestamp expirado é rejeitado (401).
  7. Atribuição do usuário é preservada (a chamada age COMO o usuário).
  8. Regressão SlowAPI: /api/ai/chat tem parâmetro `request: Request`.
  9. run_select_query emite erro seguro quando o banco read-only falha
     (sem vazar senha).

NÃO toca produção. Não requer Gemini real (GEMINI_API_KEY fica ausente de
propósito). Sem rede externa.

Rodar:  python tests/test_perpetua_internal_auth.py
   ou:  python -m pytest tests/test_perpetua_internal_auth.py
"""
import json
import os
import pathlib
import sys
import time

# raiz do repo no sys.path (permite `python tests/test_perpetua_internal_auth.py`)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Sentinelas para provar não-vazamento.
SECRET_SENTINEL = "test-internal-ai-secret-DO-NOT-USE-abc123XYZ"
READONLY_PW_SENTINEL = "sup3r-s3cret-r0-passw0rd-should-never-leak"

# Ambiente DEVE ser definido ANTES de importar app.config / app.main.
os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": "sqlite:///./scratch/perpetua_auth_test.db",
    "SEED_INITIAL_ADMIN": "true",
    "ADMIN_INITIAL_EMAIL": "admin@local.test",
    "ADMIN_INITIAL_PASSWORD": "LocalSmoke123!",
    "INTERNAL_AI_AUTH_SECRET": SECRET_SENTINEL,
    "INTERNAL_AI_AUTH_MAX_SKEW_SECONDS": "300",
    # GEMINI vazio de propósito. Definir "" (em vez de remover) impede que o
    # load_dotenv() do config repopule a chave a partir de um .env real —
    # garantindo que NENHUM teste chame o Gemini de verdade.
    "GEMINI_API_KEY": "",
})

ADMIN_EMAIL = "admin@local.test"
ADMIN_PASSWORD = "LocalSmoke123!"


_DB_INITIALIZED = False


def _client(raise_server_exceptions: bool = True):
    # Deleta o DB descartável apenas UMA vez, antes de o engine abrir o arquivo
    # (no Windows a conexão do pool mantém o arquivo travado entre clients).
    # Depois reaproveita o DB semeado — o seed é idempotente.
    global _DB_INITIALIZED
    pathlib.Path("scratch").mkdir(exist_ok=True)
    db = pathlib.Path("scratch/perpetua_auth_test.db")
    if not _DB_INITIALIZED and db.exists():
        try:
            db.unlink()
        except PermissionError:
            pass
    _DB_INITIALIZED = True
    from fastapi.testclient import TestClient  # requer httpx
    from app.main import app
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _admin():
    """Retorna (id, email) do admin semeado. Requer app já importado/seeded."""
    from app.database import SessionLocal
    from app.models.user import User
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == ADMIN_EMAIL).first()
        assert u is not None, "admin não foi semeado"
        return u.id, u.email
    finally:
        db.close()


def _set_admin_api_key(plain: str):
    from app.database import SessionLocal
    from app.models.user import User
    from app.auth import hash_api_key
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == ADMIN_EMAIL).first()
        u.api_key = hash_api_key(plain) if plain else None
        db.commit()
    finally:
        db.close()


def _internal_headers(user_id, method, path, secret=SECRET_SENTINEL, timestamp=None):
    from app.services.internal_ai_auth import (
        sign_internal_request, HEADER_USER_ID, HEADER_TIMESTAMP, HEADER_SIGNATURE,
    )
    ts = str(timestamp if timestamp is not None else int(time.time()))
    sig = sign_internal_request(secret, str(user_id), ts, method, path)
    return {
        HEADER_USER_ID: str(user_id),
        HEADER_TIMESTAMP: ts,
        HEADER_SIGNATURE: sig,
    }


# ── 1. X-API-Key (n8n) ainda funciona ──────────────────────────────────
def test_api_key_auth_still_works():
    with _client() as client:
        _set_admin_api_key("bna_integration_test_key_0001")
        try:
            r = client.get("/api/auth/me", headers={"X-API-Key": "bna_integration_test_key_0001"})
            assert r.status_code == 200, r.text
            assert r.json()["email"] == ADMIN_EMAIL
        finally:
            _set_admin_api_key("")  # limpa


# ── 2. JWT/cookie (frontend) ainda funciona ─────────────────────────────
def test_jwt_cookie_auth_still_works():
    with _client() as client:
        r = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert r.status_code == 200, r.text
        r = client.get("/api/auth/me")  # cookie persiste no client
        assert r.status_code == 200, r.text
        assert r.json()["email"] == ADMIN_EMAIL


# ── 3 & 7. Auth interna sem api_key + atribuição preservada ─────────────
def test_internal_ai_auth_without_api_key_and_attribution():
    with _client() as client:
        _set_admin_api_key("")  # garante SEM api_key
        uid, email = _admin()
        headers = _internal_headers(uid, "GET", "/api/auth/me")
        r = client.get("/api/auth/me", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        # Atribuição: a chamada age COMO o usuário (mesmo id/email).
        assert body["id"] == uid
        assert body["email"] == email


# ── 4a. Segredo ausente → 401 no auth ───────────────────────────────────
def test_internal_ai_missing_secret_rejected():
    import app.config as appconfig
    with _client() as client:
        uid, _ = _admin()
        headers = _internal_headers(uid, "GET", "/api/auth/me")
        saved = appconfig.INTERNAL_AI_AUTH_SECRET
        appconfig.INTERNAL_AI_AUTH_SECRET = ""
        try:
            r = client.get("/api/auth/me", headers=headers)
            assert r.status_code == 401, r.text
        finally:
            appconfig.INTERNAL_AI_AUTH_SECRET = saved


# ── 5. Assinatura inválida → 401 ────────────────────────────────────────
def test_internal_ai_bad_signature_rejected():
    from app.services.internal_ai_auth import HEADER_USER_ID, HEADER_TIMESTAMP, HEADER_SIGNATURE
    with _client() as client:
        uid, _ = _admin()
        headers = {
            HEADER_USER_ID: str(uid),
            HEADER_TIMESTAMP: str(int(time.time())),
            HEADER_SIGNATURE: "deadbeef" * 8,  # inválida
        }
        r = client.get("/api/auth/me", headers=headers)
        assert r.status_code == 401, r.text


# ── 6. Timestamp expirado → 401 ─────────────────────────────────────────
def test_internal_ai_expired_timestamp_rejected():
    with _client() as client:
        uid, _ = _admin()
        old_ts = int(time.time()) - 10_000  # muito além do skew (300s)
        headers = _internal_headers(uid, "GET", "/api/auth/me", timestamp=old_ts)
        r = client.get("/api/auth/me", headers=headers)
        assert r.status_code == 401, r.text


# ── 4b. call_internal_api: guardas seguros sem servidor ─────────────────
def test_call_internal_api_guards():
    import app.config as appconfig
    from app.services import ai_tools

    # (a) sem contexto de usuário → erro de contexto, não "API Key"
    ai_tools.clear_ai_user_context()
    out = json.loads(ai_tools.call_internal_api("GET", "/api/leads"))
    assert "error" in out
    assert "contexto" in out["error"].lower()
    assert "API Key" not in out["error"]

    # (b) com contexto mas SEM secret → erro de config seguro, não "API Key"
    class _U:
        id = 123
        email = "u@local.test"
        role = "admin"
    ai_tools.set_ai_user_context(_U())
    saved = appconfig.INTERNAL_AI_AUTH_SECRET
    appconfig.INTERNAL_AI_AUTH_SECRET = ""
    try:
        out = json.loads(ai_tools.call_internal_api("GET", "/api/leads"))
    finally:
        appconfig.INTERNAL_AI_AUTH_SECRET = saved
        ai_tools.clear_ai_user_context()
    assert "error" in out
    assert "INTERNAL_AI_AUTH_SECRET" in out["error"]
    assert "API Key" not in out["error"]


# ── 8. Regressão SlowAPI: /api/ai/chat tem `request: Request` ───────────
def test_ai_chat_signature_has_request_param():
    import inspect
    # Importa o app primeiro (define env). Introspecção da função da rota.
    with _client():
        from app.routers import ai as ai_router
        sig = inspect.signature(ai_router.ai_chat)  # segue __wrapped__ do SlowAPI
        params = sig.parameters
        assert "request" in params, f"parâmetro 'request' ausente: {list(params)}"
        ann = params["request"].annotation
        assert getattr(ann, "__name__", "") == "Request", f"request não é Request: {ann}"
        assert "chat_request" in params, f"'chat_request' ausente: {list(params)}"


def test_ai_chat_passes_slowapi_and_reaches_gemini_check():
    with _client(raise_server_exceptions=False) as client:
        # Defesa extra: força a chave do Gemini vazia no módulo da rota para
        # NUNCA chamar o Gemini real (a checagem no início de ai_chat dispara 500).
        from app.routers import ai as ai_router
        saved_key = ai_router.GEMINI_API_KEY
        ai_router.GEMINI_API_KEY = ""
        try:
            r = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
            assert r.status_code == 200, r.text
            r = client.post("/api/ai/chat", json={"message": "olá"})
        finally:
            ai_router.GEMINI_API_KEY = saved_key
        # Passou pelo limiter (request:Request OK) e caiu na checagem de GEMINI,
        # ANTES de qualquer chamada ao Gemini.
        assert r.status_code == 500, r.text
        body = r.text
        assert "GEMINI_API_KEY" in body, body
        # NÃO pode ser o erro do SlowAPI de parâmetro request inválido.
        assert "must be an instance" not in body
        assert "starlette" not in body.lower()


# ── 9. run_select_query: erro seguro quando o banco read-only falha ─────
def test_run_select_query_safe_error_on_readonly_failure():
    from sqlalchemy.exc import OperationalError
    with _client():
        from app.services import ai_tools

        class _BoomEngine:
            def connect(self):
                raise OperationalError(
                    "SELECT 1", {},
                    Exception(f'FATAL: password authentication failed; pw={READONLY_PW_SENTINEL}'),
                )

        saved = ai_tools._read_only_engine
        ai_tools._read_only_engine = _BoomEngine()
        try:
            out = ai_tools.run_select_query("SELECT 1")
        finally:
            ai_tools._read_only_engine = saved
        data = json.loads(out)
        assert "error" in data
        # Mensagem segura e útil.
        assert "leitura" in data["error"].lower() or "conectar" in data["error"].lower()
        # NÃO vaza a senha nem o texto cru do erro do driver.
        assert READONLY_PW_SENTINEL not in out
        assert "authentication failed" not in out


if __name__ == "__main__":
    tests = [
        test_api_key_auth_still_works,
        test_jwt_cookie_auth_still_works,
        test_internal_ai_auth_without_api_key_and_attribution,
        test_internal_ai_missing_secret_rejected,
        test_internal_ai_bad_signature_rejected,
        test_internal_ai_expired_timestamp_rejected,
        test_call_internal_api_guards,
        test_ai_chat_signature_has_request_param,
        test_ai_chat_passes_slowapi_and_reaches_gemini_check,
        test_run_select_query_safe_error_on_readonly_failure,
    ]
    try:
        for fn in tests:
            fn()
            print(f"OK {fn.__name__}")
    except ImportError as e:
        print("SKIP: dependência ausente para TestClient (httpx?):", e)
        raise SystemExit(2)
    print("OK: PERPETUA-INTERNAL-AUTH-01 — todos os testes passaram")
