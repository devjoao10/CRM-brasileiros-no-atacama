"""
QA-CONV-01 / CONV-08 — Regressao do envio outbound do Conversas.

Prova que o endpoint POST /api/conversations/{id}/messages:
  1. Sucesso (Meta aceita) -> 200, mensagem status 'sent', whatsapp_msg_id gravado.
  2. Falha real da API (send_* retorna None) -> 502, mensagem NAO fica 'sent'
     (fica 'failed'), preview da conversa nao e sobrescrito.
  3. O corpo do erro 502 nao vaza token/segredo.

A Meta API e mockada (monkeypatch de whatsapp.send_text_message). Nenhuma
credencial real e usada. Roda standalone (processo isolado):

    python tests/test_conversas_outbound.py

Isolamento: precisa que o pacote `app` resolva para conversas/app, por isso
insere conversas/ no inicio do sys.path. NAO rode no mesmo processo que os
testes do CRM (ambos usam o nome de pacote `app`).
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_outbound_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

# Sentinela de segredo: garante que nunca aparece numa resposta de erro.
SECRET_SENTINEL = "TOKEN_SECRETO_NAO_VAZAR_123"

# Env ANTES de importar qualquer modulo de app.*
os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"  # sem seed (nao exige dev email/senha)
os.environ["META_ACCESS_TOKEN"] = SECRET_SENTINEL
os.environ["META_PHONE_NUMBER_ID"] = "0000000000"

# conversas/ no inicio do path para `import app.*` = conversas/app
sys.path.insert(0, str(CONVERSAS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.services import whatsapp  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


# --- Setup DB + fixtures ---
Base.metadata.create_all(bind=engine)

db = SessionLocal()
conv = Conversation(
    lead_id=1,
    whatsapp="5511999998888",
    nome="Cliente Teste",
    status="aberta",
    unread_count=3,
    ultimo_msg="preview original",
)
db.add(conv)
db.commit()
db.refresh(conv)
CONV_ID = conv.id
db.close()


class _DummyUser:
    id = 1
    email = "tester@local"
    is_admin = True


main.app.dependency_overrides[get_current_user] = lambda: _DummyUser()

client = TestClient(main.app)


def _get_messages(status=None):
    s = SessionLocal()
    try:
        q = s.query(Message).filter(Message.conversation_id == CONV_ID)
        if status:
            q = q.filter(Message.status == status)
        return q.all()
    finally:
        s.close()


def _get_conv():
    s = SessionLocal()
    try:
        return s.query(Conversation).filter(Conversation.id == CONV_ID).first()
    finally:
        s.close()


# ============ CASO 1: SUCESSO ============
print("CONV-08 — caso sucesso (Meta aceita)")


async def _fake_ok(to, text, db):
    return {"messages": [{"id": "wamid.TESTOK"}]}


whatsapp.send_text_message = _fake_ok

r = client.post(f"/api/conversations/{CONV_ID}/messages",
                json={"content": "ola mundo", "msg_type": "text"})
check(r.status_code == 200, f"status 200 no sucesso (got {r.status_code})")
if r.status_code == 200:
    body = r.json()
    check(body.get("status") == "sent", "mensagem retornada com status 'sent'")
    check(body.get("whatsapp_msg_id") == "wamid.TESTOK", "whatsapp_msg_id persistido")
sent_rows = _get_messages(status="sent")
check(len(sent_rows) == 1, "exatamente 1 mensagem 'sent' no banco")
check(_get_conv().ultimo_msg == "ola mundo", "preview atualizado no sucesso")


# ============ CASO 2: FALHA REAL DA API ============
print("\nCONV-08 — caso falha (API retorna None)")


async def _fake_fail(to, text, db):
    return None  # falha real da Meta (HTTP error / excecao capturada no service)


whatsapp.send_text_message = _fake_fail

r2 = client.post(f"/api/conversations/{CONV_ID}/messages",
                 json={"content": "mensagem que falha", "msg_type": "text"})
check(r2.status_code == 502, f"status 502 na falha (got {r2.status_code})")

# NAO pode haver nenhuma mensagem 'sent' com o conteudo que falhou
sent_after = [m for m in _get_messages(status="sent") if m.content == "mensagem que falha"]
check(len(sent_after) == 0, "nenhuma mensagem falha marcada como 'sent'")

failed_rows = [m for m in _get_messages(status="failed") if m.content == "mensagem que falha"]
check(len(failed_rows) == 1, "mensagem que falhou persistida como 'failed'")

# Preview nao pode ter sido sobrescrito pela mensagem que falhou
check(_get_conv().ultimo_msg == "ola mundo", "preview NAO sobrescrito pela falha")


# ============ CASO 3: SEM VAZAMENTO DE SEGREDO ============
print("\nCONV-08 — erro nao vaza segredo")
check(SECRET_SENTINEL not in r2.text, "token/segredo ausente no corpo do erro 502")
check("detail" in r2.json(), "erro tem 'detail' acionavel para o operador")


# --- Resultado ---
main.app.dependency_overrides.clear()
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES OUTBOUND PASSARAM")
