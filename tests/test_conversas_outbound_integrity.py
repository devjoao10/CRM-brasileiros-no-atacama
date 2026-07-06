"""
CONV-08b — Integridade de outbound do Conversas (todos os caminhos).

Prova que NENHUM caminho outbound persiste 'sent' quando a Meta falha:
  P2. POST /initiate (template na criacao de conversa)
  P3. auto-reply end_service (PUT status 'encerrada')
  P5. auto-replies do webhook (saudacao em conversa nova)
  P6. respostas do agente n8n (_forward_to_agent)
E que a base de retry existe:
  - last_error (resumo seguro), send_attempts, last_attempt_at persistidos
  - retry manual: failed->sent na MESMA linha, attempts incrementa, sem duplicar
  - retry recusado para 'sent' (409), inbound (400) e template (400)
  - envio simulado (sem credenciais) NAO e falha (explicito)
  - migration m003 e idempotente em banco antigo

Meta API mockada; nenhuma credencial real. Roda standalone (processo isolado):

    python tests/test_conversas_outbound_integrity.py
"""
import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_integrity_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

SECRET_SENTINEL = "TOKEN_SECRETO_NAO_VAZAR_456"

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_ACCESS_TOKEN"] = SECRET_SENTINEL
os.environ["META_PHONE_NUMBER_ID"] = "0000000000"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(CONVERSAS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
import app.routers.webhook as wh  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.models.auto_reply import AutoReply  # noqa: E402
from app.services import whatsapp  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


# --- Respostas mockadas (contrato do whatsapp.py) ---
FAIL_RESPONSE = {"error": True, "status_code": 400, "summary": "HTTP 400: Recipient not valid (code 131026)"}
SIM_RESPONSE = {"simulated": True}

_wamid_seq = {"n": 0}


def _ok_response():
    # wamids sao unicos na Meta real; o mock respeita o UNIQUE de whatsapp_msg_id
    _wamid_seq["n"] += 1
    return {"messages": [{"id": f"wamid.INTEG_OK_{_wamid_seq['n']}"}]}


def make_sender(response):
    async def _sender(*args, **kwargs):
        return _ok_response() if response == "OK" else response
    return _sender


# --- Setup ---
Base.metadata.create_all(bind=engine)


class _DummyUser:
    id = 1
    email = "tester@local"
    is_admin = True


main.app.dependency_overrides[get_current_user] = lambda: _DummyUser()
client = TestClient(main.app)

# Neutraliza efeitos externos do webhook
async def _noop(*a, **k):
    return None


async def _noop_false(*a, **k):
    return False


wh.whatsapp.mark_as_read = _noop
wh.crm_service.auto_link_conversation = _noop_false

# Seeds de auto-reply usados pelos caminhos P3 e P5
s = SessionLocal()
s.add(AutoReply(trigger="greeting", title="Saudacao", message="Ola! Bem-vindo.", is_active=True))
s.add(AutoReply(trigger="end_service", title="Encerramento", message="Atendimento encerrado. Obrigado!", is_active=True))
s.commit()
s.close()


def q_messages(**filters):
    sess = SessionLocal()
    try:
        q = sess.query(Message)
        for k, v in filters.items():
            q = q.filter(getattr(Message, k) == v)
        return q.all()
    finally:
        sess.close()


def get_conv(conv_id):
    sess = SessionLocal()
    try:
        return sess.query(Conversation).filter(Conversation.id == conv_id).first()
    finally:
        sess.close()


# ============ P2: /initiate com template — FALHA ============
print("P2 — initiate (template) em falha da Meta")
whatsapp.send_template_message = make_sender(FAIL_RESPONSE)

r = client.post("/api/conversations/initiate", json={
    "whatsapp": "5511900000001", "nome": "Lead P2", "template_name": "boas_vindas",
})
check(r.status_code == 200, f"initiate responde 200 (got {r.status_code})")
body = r.json()
check(body.get("message_sent") is False, "message_sent=False quando o template falha")
rows = [m for m in q_messages(msg_type="template") if m.content]
check(len(rows) == 1 and rows[0].status == "failed", "template persistido como 'failed' (nao 'sent')")
if rows:
    m = rows[0]
    check(m.last_error and "HTTP 400" in m.last_error, "last_error com resumo seguro")
    check(m.send_attempts == 1, "send_attempts=1")
    check(m.last_attempt_at is not None, "last_attempt_at preenchido")
conv_p2 = get_conv(body["conversation_id"])
check(conv_p2.ultimo_msg in (None, ""), "preview NAO atualizado na falha")

# ============ P2: /initiate — SUCESSO ============
print("\nP2 — initiate (template) em sucesso")
whatsapp.send_template_message = make_sender("OK")
r2 = client.post("/api/conversations/initiate", json={
    "whatsapp": "5511900000002", "nome": "Lead P2b", "template_name": "boas_vindas",
})
check(r2.json().get("message_sent") is True, "message_sent=True no sucesso")
sent_tpl = [m for m in q_messages(msg_type="template", status="sent")]
check(len(sent_tpl) == 1 and sent_tpl[0].whatsapp_msg_id.startswith("wamid.INTEG_OK_"), "template 'sent' + wamid")
check(get_conv(r2.json()["conversation_id"]).ultimo_msg is not None, "preview atualizado no sucesso")

# ============ P3: auto-reply end_service — FALHA ============
print("\nP3 — auto-reply end_service em falha")
whatsapp.send_text_message = make_sender(FAIL_RESPONSE)
conv_id_p3 = r2.json()["conversation_id"]  # conversa aberta do caso anterior
r3 = client.put(f"/api/conversations/{conv_id_p3}", json={"status": "encerrada"})
check(r3.status_code == 200, f"PUT status encerrada responde 200 (got {r3.status_code})")
p3_rows = [m for m in q_messages(conversation_id=conv_id_p3) if m.content == "Atendimento encerrado. Obrigado!"]
check(len(p3_rows) == 1 and p3_rows[0].status == "failed", "auto-reply end_service persistido 'failed'")
check(p3_rows and p3_rows[0].last_error and "HTTP 400" in p3_rows[0].last_error, "last_error seguro no auto-reply")

# ============ P5: auto-reply do webhook (saudacao) — FALHA ============
print("\nP5 — auto-reply de saudacao (webhook) em falha")
whatsapp.send_text_message = make_sender(FAIL_RESPONSE)
wh.whatsapp.send_text_message = whatsapp.send_text_message  # mesmo modulo; garante o patch

inbound = {"entry": [{"changes": [{"value": {
    "contacts": [{"profile": {"name": "Cliente P5"}}],
    "messages": [{"from": "5511900000005", "id": "wamid.P5IN", "type": "text",
                  "timestamp": "1700000000", "text": {"body": "oi"}}],
}}]}]}
r5 = client.post("/webhook", json=inbound)
check(r5.status_code == 200, "webhook responde 200")
p5_rows = [m for m in q_messages(direction="outbound") if m.content == "Ola! Bem-vindo."]
check(len(p5_rows) == 1 and p5_rows[0].status == "failed", "saudacao persistida 'failed' (nao 'sent')")
check(p5_rows and p5_rows[0].send_attempts == 1 and p5_rows[0].last_attempt_at is not None,
      "metadados de tentativa na saudacao")

# ============ P6: agente n8n — FALHA ============
print("\nP6 — respostas do agente n8n em falha")


class _FakeAgentResp:
    status_code = 200
    text = ""

    def json(self):
        return {"resposta": "Parte um da Bia|||Parte dois da Bia"}


class _FakeAsyncClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _FakeAgentResp()


import httpx  # noqa: E402
_orig_async_client = httpx.AsyncClient
httpx.AsyncClient = _FakeAsyncClient

sess = SessionLocal()
conv_p6 = Conversation(lead_id=1, whatsapp="5511900000006", nome="Cliente P6",
                       status="aberta", unread_count=5, ultimo_msg="preview antigo")
sess.add(conv_p6)
sess.commit()
sess.refresh(conv_p6)
conv_p6_id = conv_p6.id

whatsapp.send_text_message = make_sender(FAIL_RESPONSE)
asyncio.run(wh._forward_to_agent(conv_p6, "pergunta do cliente", sess))
sess.close()

p6_rows = q_messages(conversation_id=conv_p6_id, direction="outbound")
check(len(p6_rows) == 2, f"2 partes da Bia persistidas (got {len(p6_rows)})")
check(all(m.status == "failed" for m in p6_rows), "ambas as partes 'failed' (nao 'sent')")
conv_p6_after = get_conv(conv_p6_id)
check(conv_p6_after.ultimo_msg == "preview antigo", "preview NAO sobrescrito quando todas as partes falham")
check(conv_p6_after.unread_count == 5, "unread NAO zerado quando todas as partes falham")

# ============ P6: agente n8n — SUCESSO ============
print("\nP6 — respostas do agente n8n em sucesso")
whatsapp.send_text_message = make_sender("OK")
sess = SessionLocal()
conv_p6b = Conversation(lead_id=1, whatsapp="5511900000007", nome="Cliente P6b",
                        status="aberta", unread_count=3)
sess.add(conv_p6b)
sess.commit()
sess.refresh(conv_p6b)
conv_p6b_id = conv_p6b.id
asyncio.run(wh._forward_to_agent(conv_p6b, "outra pergunta", sess))
sess.close()
httpx.AsyncClient = _orig_async_client

p6b_rows = q_messages(conversation_id=conv_p6b_id, direction="outbound")
check(len(p6b_rows) == 2 and all(m.status == "sent" for m in p6b_rows), "partes 'sent' no sucesso")
conv_p6b_after = get_conv(conv_p6b_id)
check(conv_p6b_after.ultimo_msg == "Parte dois da Bia", "preview = ultima parte enviada")
check(conv_p6b_after.unread_count == 0, "unread zerado no sucesso")

# ============ RETRY manual ============
print("\nRETRY — reenvio manual de mensagem failed")
# cria uma mensagem failed via endpoint principal
whatsapp.send_text_message = make_sender(FAIL_RESPONSE)
sess = SessionLocal()
conv_rt = Conversation(lead_id=1, whatsapp="5511900000008", nome="Cliente RT", status="aberta")
sess.add(conv_rt)
sess.commit()
sess.refresh(conv_rt)
conv_rt_id = conv_rt.id
sess.close()

r_fail = client.post(f"/api/conversations/{conv_rt_id}/messages",
                     json={"content": "msg para retry", "msg_type": "text"})
check(r_fail.status_code == 502, "envio inicial falha com 502")
failed_msg = [m for m in q_messages(conversation_id=conv_rt_id) if m.content == "msg para retry"][0]
check(failed_msg.status == "failed" and failed_msg.send_attempts == 1, "mensagem failed com attempts=1")

count_before = len(q_messages(conversation_id=conv_rt_id))

# retry com falha -> continua failed, attempts=2
r_retry_fail = client.post(f"/api/conversations/{conv_rt_id}/messages/{failed_msg.id}/retry")
check(r_retry_fail.status_code == 502, "retry em falha responde 502")
m_after = [m for m in q_messages(conversation_id=conv_rt_id) if m.id == failed_msg.id][0]
check(m_after.status == "failed" and m_after.send_attempts == 2, "retry falho: continua failed, attempts=2")
check(SECRET_SENTINEL not in r_retry_fail.text, "sem segredo no corpo do erro de retry")

# retry com sucesso -> sent na MESMA linha, attempts=3, wamid, last_error limpo
whatsapp.send_text_message = make_sender("OK")
r_retry_ok = client.post(f"/api/conversations/{conv_rt_id}/messages/{failed_msg.id}/retry")
check(r_retry_ok.status_code == 200, f"retry com sucesso responde 200 (got {r_retry_ok.status_code})")
m_ok = [m for m in q_messages(conversation_id=conv_rt_id) if m.id == failed_msg.id][0]
check(m_ok.status == "sent", "retry ok: failed -> sent na mesma linha")
check(m_ok.send_attempts == 3, "attempts=3 apos 2 retries")
check(m_ok.whatsapp_msg_id.startswith("wamid.INTEG_OK_"), "wamid gravado no retry ok")
check(m_ok.last_error is None, "last_error limpo apos sucesso")
check(len(q_messages(conversation_id=conv_rt_id)) == count_before, "retry NAO duplicou mensagem")
check(get_conv(conv_rt_id).ultimo_msg == "msg para retry", "preview atualizado apos retry ok")

# retry recusado: mensagem ja 'sent'
r_conflict = client.post(f"/api/conversations/{conv_rt_id}/messages/{failed_msg.id}/retry")
check(r_conflict.status_code == 409, "retry de mensagem 'sent' -> 409 (nao duplica)")

# retry recusado: mensagem inbound
sess = SessionLocal()
inb = Message(conversation_id=conv_rt_id, direction="inbound", content="msg inbound",
              msg_type="text", status="received")
sess.add(inb)
sess.commit()
sess.refresh(inb)
inb_id = inb.id
# template failed nao reenviavel
tpl = Message(conversation_id=conv_rt_id, direction="outbound", content="corpo do template",
              msg_type="template", status="failed")
sess.add(tpl)
sess.commit()
sess.refresh(tpl)
tpl_id = tpl.id
sess.close()

check(client.post(f"/api/conversations/{conv_rt_id}/messages/{inb_id}/retry").status_code == 400,
      "retry de inbound -> 400")
check(client.post(f"/api/conversations/{conv_rt_id}/messages/{tpl_id}/retry").status_code == 400,
      "retry de template -> 400 (nao suportado)")

# ============ SIMULADO (dev sem credenciais) ============
print("\nSIMULADO — envio sem credenciais NAO e falha")
whatsapp.send_text_message = make_sender(SIM_RESPONSE)
r_sim = client.post(f"/api/conversations/{conv_rt_id}/messages",
                    json={"content": "msg simulada dev", "msg_type": "text"})
check(r_sim.status_code == 200, "envio simulado responde 200 (nao e falha)")
sim_msg = [m for m in q_messages(conversation_id=conv_rt_id) if m.content == "msg simulada dev"][0]
check(sim_msg.status == "sent" and sim_msg.whatsapp_msg_id is None,
      "simulado: 'sent' explicito SEM wamid (distinguivel de envio real)")
check(sim_msg.last_error is None, "simulado nao registra erro")

# ============ MIGRATION m003 idempotente ============
print("\nm003 — migration aditiva idempotente em banco antigo")
import importlib.util  # noqa: E402
from sqlalchemy import create_engine as _ce, text as _text, inspect as _inspect  # noqa: E402

OLD_DB = SCRATCH / "conv_m003_old.db"
if OLD_DB.exists():
    OLD_DB.unlink()
old_engine = _ce(f"sqlite:///{OLD_DB.as_posix()}")
with old_engine.begin() as conn:  # tabela ANTIGA, sem as colunas novas
    conn.execute(_text(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INTEGER, "
        "direction VARCHAR(10), content TEXT, msg_type VARCHAR(20), media_url TEXT, "
        "whatsapp_msg_id VARCHAR(100), status VARCHAR(20), created_at TIMESTAMP)"
    ))

spec = importlib.util.spec_from_file_location(
    "m003", ROOT / "migrations" / "m003_conversas_message_error_fields.py")
m003 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m003)

acts1 = m003.run(old_engine)
check(sum(1 for a in acts1 if ":added" in a) == 3, f"1a execucao adiciona 3 colunas ({acts1})")
acts2 = m003.run(old_engine)
check(all(":already-present" in a for a in acts2), f"2a execucao e no-op ({acts2})")
cols = {c["name"] for c in _inspect(old_engine).get_columns("messages")}
check({"last_error", "send_attempts", "last_attempt_at"} <= cols, "colunas presentes apos m003")

# ============ SEM VAZAMENTO ============
print("\nSEGREDO — sentinela ausente de todas as respostas de erro")
check(SECRET_SENTINEL not in r_fail.text, "sem segredo no 502 do envio")
check(SECRET_SENTINEL not in r.text, "sem segredo na resposta do initiate")

# --- Resultado ---
main.app.dependency_overrides.clear()
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE INTEGRIDADE OUTBOUND PASSARAM")
