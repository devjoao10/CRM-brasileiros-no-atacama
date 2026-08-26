"""
AUDIT-2026-08-WD — resiliencia do outbound/webhook do Conversas (CONVERSAS_DIR).

Prova, ponta a ponta:
  D1. `service_window_expires_at` (novo campo, so em GET /api/conversations/{id}):
      == last_customer_msg_at + 24h; ausente/None sem inbound; service_window_open
      inalterado.
  D2. status da Meta que chega ANTES do Message existir (orfao por wamid) e
      aplicado quando a linha e inserida; precedencia (nao regride read->delivered,
      'failed' e terminal) vale tanto entre orfaos quanto depois de existir a linha.
  D3. retry HTTP com backoff em whatsapp.py: 429 (com Retry-After) e 5xx retentam
      e podem suceder; 400 NUNCA retenta; falha permanente termina 'failed'.
  D4. duas requisicoes concorrentes de retry na MESMA mensagem 'failed' resultam
      em UMA SO chamada real a Meta (a outra e rejeitada sem tocar a rede).
  D5. trocar a credencial da Meta faz a PROXIMA leitura do catalogo de templates
      ir na Meta de novo, em vez de servir a lista em cache da credencial antiga.

Meta API mockada (HTTP e nivel de funcao); nenhuma credencial real, nenhuma
requisicao de rede. Roda standalone (processo isolado):

    python tests/test_conversas_meta_resiliencia.py
"""
import asyncio
import os
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_meta_resiliencia_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_ACCESS_TOKEN"] = "TESTE_NAO_E_TOKEN_REAL"
os.environ["META_PHONE_NUMBER_ID"] = "0000000000"
os.environ["META_WABA_ID"] = "0000000001"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(CONVERSAS_DIR))

import datetime as _dt  # noqa: E402

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
import app.routers.webhook as wh  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.models.conversation import Conversation, Message, SERVICE_WINDOW  # noqa: E402
from app.models.api_config import ApiConfig  # noqa: E402
from app.services import whatsapp  # noqa: E402
from app.services import meta_templates as _meta_tpl  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


# --- Setup ---
Base.metadata.create_all(bind=engine)


class _DummyUser:
    id = 1
    email = "tester@local"
    is_admin = True


main.app.dependency_overrides[get_current_user] = lambda: _DummyUser()
client = TestClient(main.app)

# Guarda a funcao REAL antes de qualquer teste comecar a substitui-la — a
# secao D3 precisa dela de volta (nao de um mock) para exercitar o retry HTTP
# de verdade.
_real_send_text_message = whatsapp.send_text_message


def q_messages(**filters):
    sess = SessionLocal()
    try:
        q = sess.query(Message)
        for k, v in filters.items():
            q = q.filter(getattr(Message, k) == v)
        return q.all()
    finally:
        sess.close()


def _new_conversation(whatsapp_number, *, window_open=True, **extra):
    sess = SessionLocal()
    kwargs = dict(
        lead_id=1, whatsapp=whatsapp_number, nome=f"Cliente {whatsapp_number}", status="aberta",
    )
    if window_open:
        kwargs["last_customer_msg_at"] = _dt.datetime.now(_dt.timezone.utc)
    kwargs.update(extra)
    conv = Conversation(**kwargs)
    sess.add(conv)
    sess.commit()
    sess.refresh(conv)
    conv_id = conv.id
    sess.close()
    return conv_id


def _status_payload(wamid, status):
    return {"entry": [{"changes": [{"value": {
        "statuses": [{"id": wamid, "status": status, "timestamp": "1700000000"}],
    }}]}]}


def _fixed_wamid_sender(wamid):
    async def _sender(*a, **kw):
        return {"messages": [{"id": wamid}]}
    return _sender


# ===========================================================================
print("D1 — service_window_expires_at (GET /api/conversations/{id})")
now = _dt.datetime.now(_dt.timezone.utc)
conv_open_id = _new_conversation("5511900000020", window_open=False, last_customer_msg_at=now)
conv_none_id = _new_conversation("5511900000021", window_open=False)

r_open = client.get(f"/api/conversations/{conv_open_id}")
check(r_open.status_code == 200, f"GET conversa com inbound responde 200 (got {r_open.status_code})")
data_open = r_open.json()
check(data_open.get("service_window_open") is True, "service_window_open segue True (INALTERADO)")
expires_raw = data_open.get("service_window_expires_at")
check(expires_raw is not None, "service_window_expires_at presente quando ha inbound")
if expires_raw:
    expires_dt = _dt.datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
    expected = now + SERVICE_WINDOW
    delta = abs((expires_dt - expected).total_seconds())
    check(delta < 2, f"service_window_expires_at == last_customer_msg_at + 24h (delta={delta:.3f}s)")

r_none = client.get(f"/api/conversations/{conv_none_id}")
check(r_none.status_code == 200, "GET conversa sem inbound responde 200")
data_none = r_none.json()
check(data_none.get("service_window_open") is False, "sem inbound: service_window_open False (INALTERADO)")
check(data_none.get("service_window_expires_at") is None,
      f"sem inbound: service_window_expires_at ausente/None (got {data_none.get('service_window_expires_at')!r})")

# ===========================================================================
print("\nD2 — status orfao (callback da Meta antes do commit do envio)")

# A: orfao simples -> aplicado no INSERT do Message correspondente
r_a = client.post("/webhook", json=_status_payload("wamid.ORPHAN_A", "delivered"))
check(r_a.status_code == 200, "status orfao (wamid desconhecido) nao derruba o webhook")

conv_a_id = _new_conversation("5511900000030")
whatsapp.send_text_message = _fixed_wamid_sender("wamid.ORPHAN_A")
r_send_a = client.post(f"/api/conversations/{conv_a_id}/messages",
                        json={"content": "primeira msg", "msg_type": "text"})
check(r_send_a.status_code == 200, f"envio que gera o wamid orfao responde 200 (got {r_send_a.status_code})")
msg_a = q_messages(conversation_id=conv_a_id, whatsapp_msg_id="wamid.ORPHAN_A")
check(len(msg_a) == 1 and msg_a[0].status == "delivered",
      f"status orfao APLICADO ao inserir a linha (got {msg_a[0].status if msg_a else None!r})")

# B: dois orfaos fora de ordem para o MESMO wamid -> precedencia entre pendentes
# ('read' primeiro, 'delivered' atrasado depois: nao pode regredir)
client.post("/webhook", json=_status_payload("wamid.ORPHAN_B", "read"))
client.post("/webhook", json=_status_payload("wamid.ORPHAN_B", "delivered"))

conv_b_id = _new_conversation("5511900000031")
whatsapp.send_text_message = _fixed_wamid_sender("wamid.ORPHAN_B")
client.post(f"/api/conversations/{conv_b_id}/messages", json={"content": "segunda msg", "msg_type": "text"})
msg_b = q_messages(conversation_id=conv_b_id, whatsapp_msg_id="wamid.ORPHAN_B")
check(len(msg_b) == 1 and msg_b[0].status == "read",
      f"dois orfaos fora de ordem: 'read' prevalece, nao regride para 'delivered' (got {msg_b[0].status if msg_b else None!r})")

# C: 'failed' orfao e terminal mesmo quando o INSERT calcularia 'sent'
# (a Meta aceitou o POST mas reportou falha de entrega quase ao mesmo tempo)
client.post("/webhook", json=_status_payload("wamid.ORPHAN_C", "failed"))

conv_c_id = _new_conversation("5511900000032")
whatsapp.send_text_message = _fixed_wamid_sender("wamid.ORPHAN_C")
r_send_c = client.post(f"/api/conversations/{conv_c_id}/messages",
                        json={"content": "terceira msg", "msg_type": "text"})
check(r_send_c.status_code == 502,
      f"'failed' orfao reconciliado no INSERT -> operador ve 502 mesmo com o POST aceito pela Meta (got {r_send_c.status_code})")
msg_c = q_messages(conversation_id=conv_c_id, whatsapp_msg_id="wamid.ORPHAN_C")
check(len(msg_c) == 1 and msg_c[0].status == "failed",
      f"'failed' orfao prevalece sobre o 'sent' que o INSERT calcularia (got {msg_c[0].status if msg_c else None!r})")

# Depois de a linha existir, um status posterior 'delivered' continua sem
# apagar o 'failed' terminal — regra ja existente, agora compartilhada com D2.
client.post("/webhook", json=_status_payload("wamid.ORPHAN_C", "delivered"))
msg_c_after = q_messages(conversation_id=conv_c_id, whatsapp_msg_id="wamid.ORPHAN_C")
check(msg_c_after[0].status == "failed", "apos existir a linha, 'delivered' continua sem apagar 'failed' terminal")

# ===========================================================================
print("\nD3 — retry HTTP com backoff (whatsapp.py)")
whatsapp.send_text_message = _real_send_text_message  # restaura a funcao REAL (nao mock)

_call_count = {"n": 0}
_resp_queue = []
_orig_async_client = httpx.AsyncClient
_orig_sleep = asyncio.sleep


def _queued_response(status_code, headers=None):
    body = {"messages": [{"id": "wamid.RETRY_OK"}]} if status_code < 400 else {
        "error": {"message": "erro simulado", "code": status_code}
    }
    resp = httpx.Response(status_code, headers=headers or {}, json=body)
    resp.request = httpx.Request("POST", "https://graph.facebook.com/fake/messages")
    return resp


class _QueueAsyncClient:
    """Substitui httpx.AsyncClient: devolve as respostas de `_resp_queue` em
    ordem, uma por `.post()`, sem tocar rede nenhuma. `_call_count` prova
    QUANTAS tentativas realmente saíram (a unica forma de provar retry)."""

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kwargs):
        _call_count["n"] += 1
        if not _resp_queue:
            raise AssertionError("fila de respostas fake do retry esgotada")
        return _resp_queue.pop(0)


async def _fast_sleep(_seconds):
    return None


httpx.AsyncClient = _QueueAsyncClient
asyncio.sleep = _fast_sleep
whatsapp.asyncio.sleep = _fast_sleep

try:
    # 429 com Retry-After -> 1 retry -> sucesso
    _call_count["n"] = 0
    _resp_queue.clear()
    _resp_queue.append(_queued_response(429, headers={"Retry-After": "0"}))
    _resp_queue.append(_queued_response(200))
    result_429 = asyncio.run(whatsapp.send_text_message("5511900000040", "teste 429", db=None))
    check(_call_count["n"] == 2, f"429: exatamente 2 chamadas HTTP (1 + 1 retry) (got {_call_count['n']})")
    check(isinstance(result_429, dict) and "messages" in result_429, "429: sucesso apos honrar Retry-After e retentar")

    # 500 -> 1 retry -> sucesso
    _call_count["n"] = 0
    _resp_queue.clear()
    _resp_queue.append(_queued_response(500))
    _resp_queue.append(_queued_response(200))
    result_500 = asyncio.run(whatsapp.send_text_message("5511900000041", "teste 500", db=None))
    check(_call_count["n"] == 2, f"500: exatamente 2 chamadas HTTP (1 + 1 retry) (got {_call_count['n']})")
    check(isinstance(result_500, dict) and "messages" in result_500, "500: sucesso apos 1 retry")

    # 400 -> NUNCA retenta (permanente)
    _call_count["n"] = 0
    _resp_queue.clear()
    _resp_queue.append(_queued_response(400))
    result_400 = asyncio.run(whatsapp.send_text_message("5511900000042", "teste 400", db=None))
    check(_call_count["n"] == 1, f"400: NENHUM retry, so 1 chamada (got {_call_count['n']})")
    check(isinstance(result_400, dict) and result_400.get("error") is True, "400: classificado como erro (nao retentado)")

    # Falha permanente (sempre 5xx) -> esgota as tentativas -> Message 'failed'
    _call_count["n"] = 0
    _resp_queue.clear()
    _resp_queue.extend([_queued_response(500), _queued_response(500), _queued_response(500)])
    conv_d3_id = _new_conversation("5511900000043")
    r_perm = client.post(f"/api/conversations/{conv_d3_id}/messages",
                          json={"content": "sempre falha", "msg_type": "text"})
    check(_call_count["n"] == 3, f"falha permanente: esgota as 3 tentativas (1 + 2 retries) antes de desistir (got {_call_count['n']})")
    check(r_perm.status_code == 502, f"falha permanente -> 502 para o operador (got {r_perm.status_code})")
    msg_d3 = [m for m in q_messages(conversation_id=conv_d3_id) if m.content == "sempre falha"]
    check(len(msg_d3) == 1 and msg_d3[0].status == "failed", "falha permanente termina 'failed' (nunca 'sent')")
    check(msg_d3 and msg_d3[0].send_attempts == 1,
          f"send_attempts reflete a tentativa de envio (1; o retry HTTP e interno a send_text_message) (got {msg_d3[0].send_attempts if msg_d3 else None})")
finally:
    httpx.AsyncClient = _orig_async_client
    asyncio.sleep = _orig_sleep
    whatsapp.asyncio.sleep = _orig_sleep

# ===========================================================================
print("\nD4 — duas requisicoes concorrentes de retry -> UMA so chamada real a Meta")
conv_d4_id = _new_conversation("5511900000044")
sess = SessionLocal()
msg_d4 = Message(conversation_id=conv_d4_id, direction="outbound", content="mensagem para retry concorrente",
                  msg_type="text", status="failed", send_attempts=1)
sess.add(msg_d4)
sess.commit()
sess.refresh(msg_d4)
msg_d4_id = msg_d4.id
sess.close()

_race_sent = []
_race_lock = threading.Lock()


async def _racy_send(to, text, db=None):
    with _race_lock:
        _race_sent.append(text)
        wamid = f"wamid.RACE_{len(_race_sent)}"
    # Atraso real (nao mockado) — as duas requisicoes precisam estar "em voo"
    # ao mesmo tempo para a corrida ser genuina, nao so teoricamente possivel.
    await asyncio.sleep(0.3)
    return {"messages": [{"id": wamid}]}


whatsapp.send_text_message = _racy_send

race_results = {}


def _fire_retry(idx):
    race_results[idx] = client.post(f"/api/conversations/{conv_d4_id}/messages/{msg_d4_id}/retry")


t1 = threading.Thread(target=_fire_retry, args=(0,))
t2 = threading.Thread(target=_fire_retry, args=(1,))
t1.start()
t2.start()
t1.join(timeout=10)
t2.join(timeout=10)

check(len(_race_sent) == 1, f"duas retries concorrentes na MESMA mensagem -> UMA so chamada real a Meta (got {len(_race_sent)})")
race_statuses = sorted(r.status_code for r in race_results.values())
check(race_statuses == [200, 409],
      f"uma retry sucede (200) e a outra e rejeitada (409) sem tocar a rede (got {race_statuses})")
msg_d4_after = q_messages(id=msg_d4_id)
check(msg_d4_after and msg_d4_after[0].status == "sent", "a mensagem termina 'sent' (nunca presa em 'retrying')")

whatsapp.send_text_message = _real_send_text_message

# ===========================================================================
print("\nD5 — troca de credencial invalida o catalogo de templates em cache")
_catalog_calls = []


def _sess_ctx():
    return SessionLocal()


async def _fetch_v1(base_url, waba_id, headers):
    _catalog_calls.append(headers.get("Authorization"))
    return [{"name": "tpl_v1", "language": "pt_BR", "status": "APPROVED", "category": "UTILITY",
             "components": [{"type": "BODY", "text": "versao 1"}]}]


_meta_tpl._fetch_meta_templates = _fetch_v1
_meta_tpl.invalidate_catalog_cache()

sess = _sess_ctx()
cfg = sess.query(ApiConfig).filter(ApiConfig.id == 1).first()
if cfg is None:
    cfg = ApiConfig(id=1)
    sess.add(cfg)
cfg.meta_access_token = "TOKEN_V1"
cfg.meta_waba_id = "WABA_V1"
cfg.meta_phone_number_id = "PHONE_V1"
cfg.meta_api_version = "v21.0"
cfg.is_connected = True
sess.commit()
sess.close()

sess = _sess_ctx()
result1 = asyncio.run(_meta_tpl.list_approved_templates(sess))
sess.close()
check(len(_catalog_calls) == 1, f"1a leitura chama a Meta (got {len(_catalog_calls)})")
check(result1.get("ok") and result1["templates"][0]["name"] == "tpl_v1", "catalogo v1 correto na 1a leitura")

sess = _sess_ctx()
result2 = asyncio.run(_meta_tpl.list_approved_templates(sess))
sess.close()
check(len(_catalog_calls) == 1,
      f"2a leitura com a MESMA credencial usa o cache, nao chama a Meta de novo (got {len(_catalog_calls)})")

# Troca de credencial SEM chamar invalidate_catalog_cache() — e exatamente o
# que D5 corrige: o cache precisa invalidar SOZINHO, estruturalmente.
async def _fetch_v2(base_url, waba_id, headers):
    _catalog_calls.append(headers.get("Authorization"))
    return [{"name": "tpl_v2", "language": "pt_BR", "status": "APPROVED", "category": "UTILITY",
             "components": [{"type": "BODY", "text": "versao 2"}]}]


_meta_tpl._fetch_meta_templates = _fetch_v2

sess = _sess_ctx()
cfg = sess.query(ApiConfig).filter(ApiConfig.id == 1).first()
cfg.meta_access_token = "TOKEN_V2"
sess.commit()
sess.close()

sess = _sess_ctx()
result3 = asyncio.run(_meta_tpl.list_approved_templates(sess))
sess.close()
check(len(_catalog_calls) == 2,
      f"credencial trocada -> proxima leitura bate na Meta de novo, SEM invalidate_catalog_cache explicito (got {len(_catalog_calls)} chamadas)")
check(result3.get("ok") and result3["templates"][0]["name"] == "tpl_v2",
      "catalogo passa a ser o da credencial NOVA (v2), nao o v1 preso em cache")

# --- Resultado ---
main.app.dependency_overrides.clear()
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE RESILIENCIA META PASSARAM")
