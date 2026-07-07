"""
WP-SEC-03 — prova de que o rate limit do login está efetivamente aplicado.

Sobe o app em processo (TestClient + SQLite descartável), faz 1 login válido
(200) e excede o limite de 5/minute para provar o 429. NÃO toca produção.

Rodar:  python tests/test_rate_limit.py
   ou:  python -m pytest tests/test_rate_limit.py
"""
import os
import pathlib
import sys

# garante a raiz do repo no sys.path (permite `python tests/test_rate_limit.py`)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def _client():
    os.environ.update({
        "ENVIRONMENT": "development",
        "DATABASE_URL": "sqlite:///./scratch/ratelimit_test.db",
        "SEED_INITIAL_ADMIN": "true",
        "ADMIN_INITIAL_EMAIL": "admin@local.test",
        "ADMIN_INITIAL_PASSWORD": "LocalSmoke123!",
    })
    pathlib.Path("scratch").mkdir(exist_ok=True)
    db = pathlib.Path("scratch/ratelimit_test.db")
    if db.exists():
        db.unlink()
    from fastapi.testclient import TestClient  # requer httpx
    from app.main import app
    return TestClient(app)


def test_login_throttled_after_5_per_minute():
    with _client() as client:
        codes = []
        r = client.post("/api/auth/login",
                        json={"email": "admin@local.test", "password": "LocalSmoke123!"})
        codes.append(r.status_code)  # login válido
        for _ in range(5):
            r = client.post("/api/auth/login", json={"email": "x@x", "password": "wrong"})
            codes.append(r.status_code)
        assert codes[0] == 200, f"login válido deveria retornar 200: {codes}"
        assert 429 in codes, f"rate limit não aplicado (sem 429): {codes}"
        assert codes.index(429) == 5, f"429 na posição inesperada: {codes}"


if __name__ == "__main__":
    try:
        test_login_throttled_after_5_per_minute()
    except ImportError as e:
        print("SKIP: dependência ausente para TestClient (httpx?):", e)
        raise SystemExit(2)
    print("OK: login válido=200 e 429 após exceder 5/minute")
