"""
CONV-BF-AUTH-01 — Login local de dev do Conversas.

Prova que, com CONVERSAS_SEED_DEV_DATA=true:
  1. POST /api/auth/login autentica na tabela LOCAL (200 + access_token).
  2. Resposta tem token_type e user; NUNCA inclui hashed_password.
  3. Senha errada / email inexistente / usuario inativo -> 401 UNIFORME
     (mesmo detail — nao revela se o email existe).
  4. O token emitido passa no GET /api/auth/verify.
  5. O caminho local NAO chama o CRM (proxy instrumentado com contador).
  6. Proxy de PRODUCAO preservado: com a flag desligada, /login chama o CRM.
  7. Nenhum segredo/hash em respostas de erro.

Credenciais de teste sao FIXTURES locais (sem segredo real).
Roda standalone:  python tests/test_conversas_auth_local.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_auth_local_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

DEV_EMAIL = "dev@bna.local"
DEV_PASS = "senha-de-teste-local"  # fixture de teste, nao e segredo real

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "true"
os.environ["CONVERSAS_DEV_EMAIL"] = DEV_EMAIL
os.environ["CONVERSAS_DEV_PASSWORD"] = DEV_PASS
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(CONVERSAS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
import app.routers.auth as auth_router  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.auth import User  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


# TestClient com lifespan -> roda o seed do dev user
client = TestClient(main.app)
client.__enter__()  # dispara startup/lifespan (seed_dev_user)

# instrumenta o proxy: qualquer chamada ao CRM incrementa o contador
proxy_calls = {"n": 0}
import httpx  # noqa: E402


class _FakeCRMResp:
    status_code = 200

    def json(self):
        return {"access_token": "tok-do-crm", "token_type": "bearer",
                "user": {"id": 1, "nome": "X", "email": "x@x", "role": "user", "is_active": True}}


class _FakeCRMClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        proxy_calls["n"] += 1
        return _FakeCRMResp()


_orig_client = httpx.AsyncClient
httpx.AsyncClient = _FakeCRMClient


# ============ 1/2. LOGIN LOCAL OK ============
print("CONV-BF-AUTH-01 — login local de dev")
r = client.post("/api/auth/login", json={"email": DEV_EMAIL, "password": DEV_PASS})
check(r.status_code == 200, f"login local 200 (got {r.status_code})")
body = r.json()
check(bool(body.get("access_token")), "access_token presente")
check(body.get("token_type") == "bearer", "token_type presente")
check(body.get("user", {}).get("email") == DEV_EMAIL, "user presente com email")
check("hashed_password" not in r.text and "senha-de-teste" not in r.text,
      "hash/senha NUNCA na resposta")
check(proxy_calls["n"] == 0, "caminho local NAO chamou o CRM")

TOKEN = body["access_token"]


# ============ 3. 401 UNIFORME ============
print("\nCONV-BF-AUTH-01 — 401 uniforme")
r_wrong = client.post("/api/auth/login", json={"email": DEV_EMAIL, "password": "errada"})
check(r_wrong.status_code == 401, "senha errada -> 401")
r_unknown = client.post("/api/auth/login", json={"email": "naoexiste@bna.local", "password": "x"})
check(r_unknown.status_code == 401, "email inexistente -> 401")

# usuario inativo
s = SessionLocal()
u = s.query(User).filter(User.email == DEV_EMAIL).first()
u.is_active = False
s.commit()
s.close()
r_inactive = client.post("/api/auth/login", json={"email": DEV_EMAIL, "password": DEV_PASS})
check(r_inactive.status_code == 401, "usuario inativo -> 401")
check(r_wrong.json()["detail"] == r_unknown.json()["detail"] == r_inactive.json()["detail"],
      "detail UNIFORME (nao revela se o email existe)")
for resp in (r_wrong, r_unknown, r_inactive):
    if "hashed" in resp.text or DEV_PASS in resp.text or "test-secret-key" in resp.text:
        check(False, "segredo/hash vazou em resposta de erro")
        break
else:
    check(True, "nenhum segredo/hash em respostas de erro")
# reativa
s = SessionLocal()
u = s.query(User).filter(User.email == DEV_EMAIL).first()
u.is_active = True
s.commit()
s.close()


# ============ 4. TOKEN VALIDO NO /verify ============
print("\nCONV-BF-AUTH-01 — token emitido e valido")
r_verify = client.get("/api/auth/verify", headers={"Authorization": f"Bearer {TOKEN}"})
check(r_verify.status_code == 200 and r_verify.json().get("email") == DEV_EMAIL,
      "token passa no /api/auth/verify")
check(client.get("/api/auth/verify", headers={"Authorization": "Bearer lixo"}).status_code == 401,
      "token invalido -> 401")


# ============ 5/6. PROXY DE PRODUCAO PRESERVADO ============
print("\nCONV-BF-AUTH-01 — proxy CRM preservado (modo producao)")
auth_router.CONVERSAS_SEED_DEV_DATA = False  # simula producao
r_prod = client.post("/api/auth/login", json={"email": "a@b.c", "password": "x"})
check(proxy_calls["n"] == 1, "com a flag desligada, /login CHAMA o CRM (proxy intacto)")
check(r_prod.status_code == 200 and r_prod.json().get("access_token") == "tok-do-crm",
      "resposta do CRM repassada")
auth_router.CONVERSAS_SEED_DEV_DATA = True
check(client.post("/api/auth/login",
                  json={"email": DEV_EMAIL, "password": DEV_PASS}).status_code == 200,
      "de volta ao modo dev: login local segue funcionando")
check(proxy_calls["n"] == 1, "modo dev nao voltou a chamar o CRM")

# --- Resultado ---
httpx.AsyncClient = _orig_client
client.__exit__(None, None, None)
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE AUTH LOCAL PASSARAM")
