"""
QA-CONV-01 — Regressao do webhook do Conversas (recebimento).

Prova que POST /webhook:
  1. Inbound de texto -> cria conversa e persiste Message inbound (status 'received').
  2. Idempotencia -> o mesmo msg_id nao duplica a mensagem.
  3. Status update -> atualiza o status de uma mensagem outbound existente
     (ex.: 'sent' -> 'delivered') casando pelo whatsapp_msg_id.

Sem HMAC (development, sem META_APP_SECRET). Chamadas externas (mark_as_read,
auto-link CRM) sao neutralizadas — nenhuma rede real e usada.

Roda standalone (processo isolado):  python tests/test_conversas_webhook.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_webhook_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""   # sem HMAC em development
os.environ["N8N_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(CONVERSAS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
import app.routers.webhook as wh  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


# --- Neutraliza efeitos externos do processamento inbound ---
async def _noop(*a, **k):
    return None


async def _noop_false(*a, **k):
    return False


if hasattr(wh, "whatsapp"):
    wh.whatsapp.mark_as_read = _noop
    wh.whatsapp.send_text_message = _noop
if hasattr(wh, "crm_service"):
    wh.crm_service.auto_link_conversation = _noop_false

Base.metadata.create_all(bind=engine)
client = TestClient(main.app)


def _session():
    return SessionLocal()


# ============ CASO 1: INBOUND TEXT ============
print("QA-CONV-01 — webhook inbound de texto")

inbound = {
    "entry": [{"changes": [{"value": {
        "contacts": [{"profile": {"name": "Fulano de Tal"}}],
        "messages": [{
            "from": "5511888887777",
            "id": "wamid.IN1",
            "type": "text",
            "timestamp": "1700000000",
            "text": {"body": "ola, quero informacoes"},
        }],
    }}]}]
}

r = client.post("/webhook", json=inbound)
check(r.status_code == 200, f"webhook responde 200 (got {r.status_code})")

s = _session()
conv = s.query(Conversation).filter(Conversation.whatsapp == "5511888887777").first()
check(conv is not None, "conversa criada a partir do inbound")
msg = s.query(Message).filter(Message.whatsapp_msg_id == "wamid.IN1").first()
check(msg is not None, "mensagem inbound persistida")
if msg:
    check(msg.direction == "inbound", "direcao 'inbound'")
    check(msg.content == "ola, quero informacoes", "conteudo persistido")
    check(msg.status == "received", "status 'received'")
s.close()


# ============ CASO 2: IDEMPOTENCIA ============
print("\nQA-CONV-01 — idempotencia (mesmo msg_id nao duplica)")
client.post("/webhook", json=inbound)  # reenvia identico
s = _session()
count = s.query(Message).filter(Message.whatsapp_msg_id == "wamid.IN1").count()
check(count == 1, "mensagem nao duplicada apos reenvio")
s.close()


# ============ CASO 3: STATUS UPDATE ============
print("\nQA-CONV-01 — status update de mensagem outbound")
# semear uma mensagem outbound 'sent' com um wamid conhecido
s = _session()
conv2 = Conversation(lead_id=1, whatsapp="5511777776666", nome="Beltrano", status="aberta")
s.add(conv2)
s.commit()
s.refresh(conv2)
out = Message(conversation_id=conv2.id, direction="outbound", content="oi",
              msg_type="text", whatsapp_msg_id="wamid.OUT1", status="sent")
s.add(out)
s.commit()
s.close()

status_payload = {
    "entry": [{"changes": [{"value": {
        "statuses": [{"id": "wamid.OUT1", "status": "delivered", "timestamp": "1700000001"}],
    }}]}]
}
r3 = client.post("/webhook", json=status_payload)
check(r3.status_code == 200, f"status webhook responde 200 (got {r3.status_code})")

s = _session()
updated = s.query(Message).filter(Message.whatsapp_msg_id == "wamid.OUT1").first()
check(updated is not None and updated.status == "delivered",
      "status da mensagem outbound atualizado para 'delivered'")
s.close()


# --- Resultado ---
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE WEBHOOK PASSARAM")
