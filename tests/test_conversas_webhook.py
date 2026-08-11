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
s = _session()
_c = s.query(Conversation).filter(Conversation.whatsapp == "5511888887777").first()
before_unread, before_ultimo = _c.unread_count, _c.ultimo_msg
s.close()

client.post("/webhook", json=inbound)  # reenvia identico
s = _session()
count = s.query(Message).filter(Message.whatsapp_msg_id == "wamid.IN1").count()
check(count == 1, "mensagem nao duplicada apos reenvio")
_c = s.query(Conversation).filter(Conversation.whatsapp == "5511888887777").first()
check(_c.unread_count == before_unread,
      f"retry NAO altera unread_count (antes={before_unread}, depois={_c.unread_count})")
check(_c.ultimo_msg == before_ultimo, "retry NAO altera ultimo_msg")
s.close()


# ====== CASO 2b: DUPLICADA + LEGITIMA NO MESMO PAYLOAD ======
print("\nQA-CONV-01 — lote com duplicada + nova: efeitos da duplicada nao persistem")
mixed = {
    "entry": [{"changes": [{"value": {
        "contacts": [{"profile": {"name": "Fulano de Tal"}}],
        "messages": [
            dict(inbound["entry"][0]["changes"][0]["value"]["messages"][0]),  # wamid.IN1 (duplicada)
            {"from": "5511888887777", "id": "wamid.IN2", "type": "text",
             "timestamp": "1700000002", "text": {"body": "segunda mensagem"}},
        ],
    }}]}]
}
client.post("/webhook", json=mixed)
s = _session()
check(s.query(Message).filter(Message.whatsapp_msg_id == "wamid.IN1").count() == 1,
      "duplicada continua sem duplicar Message")
check(s.query(Message).filter(Message.whatsapp_msg_id == "wamid.IN2").first() is not None,
      "mensagem legitima do mesmo lote foi persistida")
_c = s.query(Conversation).filter(Conversation.whatsapp == "5511888887777").first()
check(_c.unread_count == before_unread + 1,
      f"unread_count subiu exatamente 1 (esperado={before_unread + 1}, got={_c.unread_count})")
check(_c.ultimo_msg == "segunda mensagem", "ultimo_msg reflete apenas a mensagem nova")
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


# ====== CASO 4 e 5: MENSAGEM SEM ID DA META ======
# `whatsapp_msg_id` e UNIQUE e nullable. Com `msg.get("id", "")`, duas mensagens
# distintas sem id viravam "" e a segunda era descartada como duplicata da
# primeira — perda silenciosa, com 200 devolvido a Meta. A correcao normaliza
# para None e so dedupa quando HA id.

def _sem_id(numero, texto, id_literal=None):
    """Payload de inbound. id_literal=None -> chave 'id' AUSENTE."""
    m = {"from": numero, "type": "text", "timestamp": "1700000009",
         "text": {"body": texto}}
    if id_literal is not None:
        m["id"] = id_literal
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"profile": {"name": "Sem Id"}}], "messages": [m]}}]}]}


def _conv_msgs(numero):
    s = _session()
    try:
        conv = s.query(Conversation).filter(Conversation.whatsapp == numero).first()
        msgs = (s.query(Message)
                .filter(Message.conversation_id == conv.id)
                .order_by(Message.id).all()) if conv else []
        return conv, [(m.content, m.whatsapp_msg_id) for m in msgs]
    finally:
        s.close()


for _caso, _num, _idlit in (("CASO 4 — chave 'id' AUSENTE", "5511666665555", None),
                            ("CASO 5 — id vazio (\"\")", "5511555554444", "")):
    print(f"\nQA-CONV-01 — {_caso}")
    client.post("/webhook", json=_sem_id(_num, "primeira sem id", _idlit))
    client.post("/webhook", json=_sem_id(_num, "segunda sem id", _idlit))

    _conv, _msgs = _conv_msgs(_num)
    check(len(_msgs) == 2, f"2 mensagens persistidas (got {len(_msgs)}) — a segunda NAO foi descartada")
    check(all(mid is None for _, mid in _msgs),
          f"ambas com whatsapp_msg_id NULL (got {[mid for _, mid in _msgs]})")
    check(_conv is not None and _conv.unread_count == 2,
          f"unread_count == 2 (got {_conv.unread_count if _conv else None})")
    check(_conv is not None and _conv.ultimo_msg == "segunda sem id",
          f"ultimo_msg reflete a SEGUNDA (got {_conv.ultimo_msg if _conv else None!r})")

# Trade-off deliberado: sem chave de idempotencia nao existe forma confiavel de
# reconhecer um retry. Duas entregas IDENTICAS sem id sao persistidas duas vezes.
# Isso NAO e bug desta correcao — e a escolha de preferir possivel duplicacao a
# perda silenciosa. Mensagem duplicada e visivel e corrigivel; perdida, nao.
print("\nQA-CONV-01 — trade-off: entrega repetida SEM id duplica (esperado)")
client.post("/webhook", json=_sem_id("5511666665555", "primeira sem id", None))
_conv, _msgs = _conv_msgs("5511666665555")
check(len(_msgs) == 3, f"reentrega sem id foi persistida (got {len(_msgs)} msgs) — duplicacao aceita por decisao")


# --- Resultado ---
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE WEBHOOK PASSARAM")
