"""
CONV-06 — Fila de atendimento do Conversas.

Fonte unica de estado: status ('aberta'|'encerrada') + atendente_id
(NULL = aguardando). 'aguardando' e DERIVADO — nunca persistido.

Prova que:
  1. Inbound novo entra na fila (?queue=fila).
  2. Fila ordena por espera (last_customer_msg_at asc — mais antigo primeiro).
  3. Claim tira da fila; atendente = usuario autenticado (nunca do request).
  4. TRAVA: segundo atendente recebe 409; o mesmo atendente pode re-claim.
  5. Release devolve a fila (idempotente).
  6. Encerrada sai da fila; claim em encerrada -> 409.
  7. Transicao invalida: PUT status 'aguardando'/'banana' -> 422.
  8. ?queue invalida -> 422; em_atendimento lista so as assumidas.
  9. Sem auth -> 401.
 10. Frontend (grep): filtro derivado + botao claim/release.

Roda standalone:  python tests/test_conversas_queue.py
"""
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_queue_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(CONVERSAS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
import app.routers.webhook as wh  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


Base.metadata.create_all(bind=engine)


class _User1:
    id = 1
    email = "atendente1@local"
    is_admin = False


class _User2:
    id = 2
    email = "atendente2@local"
    is_admin = False


def as_user(u):
    main.app.dependency_overrides[get_current_user] = lambda: u


as_user(_User1())
client = TestClient(main.app)


async def _noop(*a, **k):
    return None


async def _noop_false(*a, **k):
    return False


wh.whatsapp.mark_as_read = _noop
wh.whatsapp.send_text_message = _noop
wh.crm_service.auto_link_conversation = _noop_false


def inbound(msg_id, sender):
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"profile": {"name": f"Cliente {sender[-4:]}"}}],
        "messages": [{"from": sender, "id": msg_id, "type": "text",
                      "timestamp": "1700000000", "text": {"body": "oi"}}],
    }}]}]}


# ============ 1/2. INBOUND ENTRA NA FILA + ORDENACAO ============
print("CONV-06 — inbound entra na fila; ordenacao por espera")
client.post("/webhook", json=inbound("wamid.Q1", "5511900077771"))
time.sleep(0.05)  # garante last_customer_msg_at distinto
client.post("/webhook", json=inbound("wamid.Q2", "5511900077772"))

fila = client.get("/api/conversations?queue=fila").json()
check(fila["total"] == 2, f"2 conversas novas na fila (got {fila['total']})")
check(fila["conversations"][0]["whatsapp"] == "5511900077771",
      "mais antiga PRIMEIRO na fila")
CID_A = fila["conversations"][0]["id"]
CID_B = fila["conversations"][1]["id"]


# ============ 3/4. CLAIM + TRAVA ============
print("\nCONV-06 — claim com trava anti-duplo-atendimento")
r = client.post(f"/api/conversations/{CID_A}/claim")
check(r.status_code == 200 and r.json()["atendente_id"] == 1,
      "claim atribui ao usuario AUTENTICADO (id=1)")
check(client.get("/api/conversations?queue=fila").json()["total"] == 1,
      "conversa assumida saiu da fila")
em_at = client.get("/api/conversations?queue=em_atendimento").json()
check(em_at["total"] == 1 and em_at["conversations"][0]["id"] == CID_A,
      "em_atendimento lista a assumida")

as_user(_User2())
r409 = client.post(f"/api/conversations/{CID_A}/claim")
check(r409.status_code == 409, f"segundo atendente -> 409 (got {r409.status_code})")

as_user(_User1())
check(client.post(f"/api/conversations/{CID_A}/claim").status_code == 200,
      "re-claim pelo MESMO atendente e ok (idempotente)")


# ============ 5. RELEASE ============
print("\nCONV-06 — release devolve a fila")
r5 = client.post(f"/api/conversations/{CID_A}/release")
check(r5.status_code == 200 and r5.json()["atendente_id"] is None, "release limpa atendente")
check(client.get("/api/conversations?queue=fila").json()["total"] == 2, "voltou para a fila")
check(client.post(f"/api/conversations/{CID_A}/release").status_code == 200,
      "release idempotente")


# ============ 6. ENCERRADA ============
print("\nCONV-06 — encerrada sai da fila")
client.put(f"/api/conversations/{CID_B}", json={"status": "encerrada"})
check(client.get("/api/conversations?queue=fila").json()["total"] == 1,
      "encerrada nao aparece na fila")
check(client.post(f"/api/conversations/{CID_B}/claim").status_code == 409,
      "claim em encerrada -> 409")


# ============ 7/8. TRANSICOES/PARAMS INVALIDOS ============
print("\nCONV-06 — validacoes")
check(client.put(f"/api/conversations/{CID_A}", json={"status": "aguardando"}).status_code == 422,
      "'aguardando' como status persistido -> 422 (e derivado)")
check(client.put(f"/api/conversations/{CID_A}", json={"status": "banana"}).status_code == 422,
      "status desconhecido -> 422")
check(client.get("/api/conversations?queue=banana").status_code == 422,
      "?queue invalida -> 422")
check(client.put(f"/api/conversations/{CID_B}", json={"status": "aberta"}).status_code == 200,
      "reabrir (encerrada->aberta) permitido")


# ============ 9. AUTH ============
print("\nCONV-06 — auth")
main.app.dependency_overrides.clear()
check(client.post(f"/api/conversations/{CID_A}/claim").status_code == 401, "claim sem auth -> 401")
check(client.get("/api/conversations?queue=fila").status_code == 401, "fila sem auth -> 401")
as_user(_User1())


# ============ 10. FRONTEND ============
print("\nCONV-06 — frontend (grep)")
js = (CONVERSAS_DIR / "static" / "js" / "conversas.js").read_text(encoding="utf-8")
html = (CONVERSAS_DIR / "templates" / "conversas.html").read_text(encoding="utf-8")
check("c.status === 'aberta' && !c.atendente_id" in js, "aba Aguardando filtra por estado DERIVADO")
check("updateClaimButton" in js and "claimOrRelease" in js, "botao assumir/liberar presente")
check('id="btnClaim"' in html, "botao no header do chat")
check("Em atendimento" in js, "indicador de conversa assumida por outro")

# --- Resultado ---
main.app.dependency_overrides.clear()
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE FILA PASSARAM")
