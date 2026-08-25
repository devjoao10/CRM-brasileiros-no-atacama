"""
CONV-WINDOW-01 — Janela de atendimento de 24h da Meta + envio de templates.

Prova, ponta a ponta, que:
  W. a regra pura de 24h e exata nos limites (23h59 / 24h00 / 24h01 / NULL / naive)
  G. o BACKEND recusa free-form fora da janela ANTES de tocar a Meta e sem
     persistir Message (texto, midia/audio, retry)
  T. template APPROVED continua permitido com a janela fechada, e nao a reabre
  P. a aridade dos parametros vem da Meta e e validada no backend (N-1 / N / N+1)
  L. (name, language) e a chave — mesmo nome em outro idioma e outro template
  R. reaction inbound NAO reabre a janela; os demais tipos reabrem
  I. /initiate sem inbound deixa a conversa com a janela FECHADA
  E. end_service (texto livre por acao humana) respeita a janela, mas o
     encerramento da conversa acontece de qualquer forma
  F. falha ao listar/enviar template nunca libera texto livre

Meta API mockada; nenhuma credencial real, nenhuma requisicao de rede.
Roda standalone (processo isolado):

    python tests/test_conversas_service_window.py
"""
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_service_window_test.db"
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

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
import app.routers.webhook as wh  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.models.conversation import (  # noqa: E402
    Conversation,
    Message,
    service_window_open,
)
from app.models.auto_reply import AutoReply  # noqa: E402
from app.services import whatsapp  # noqa: E402
from app.services import meta_templates  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


UTC = timezone.utc
NOW = datetime(2026, 8, 23, 12, 0, 0, tzinfo=UTC)

# --- Respostas mockadas (contrato do whatsapp.py) ---
FAIL_RESPONSE = {"error": True, "status_code": 400, "summary": "HTTP 400: erro simulado (code 131047)"}
_wamid_seq = {"n": 0}
# Toda chamada real a Meta passa por aqui: se um guard falhar, o contador sobe.
calls = {"text": 0, "media": 0, "upload": 0, "template": 0}


def _ok_response():
    _wamid_seq["n"] += 1
    return {"messages": [{"id": f"wamid.WINDOW_{_wamid_seq['n']}"}]}


def _counting_sender(kind, response="OK"):
    async def _sender(*args, **kwargs):
        calls[kind] += 1
        return _ok_response() if response == "OK" else response
    return _sender


whatsapp.send_text_message = _counting_sender("text")
whatsapp.send_media_message = _counting_sender("media")
whatsapp.send_template_message = _counting_sender("template")


async def _upload_ok(*a, **k):
    calls["upload"] += 1
    return {"id": "media-id-teste"}


whatsapp.upload_media = _upload_ok

# --- Catalogo Meta mockado: espelha a forma REAL da Graph API ---------------
# Estruturas retiradas do inventario read-only da WABA (34/34 APPROVED, nenhum
# header de midia, nenhum botao, nenhum parametro de header).
META_RAW = [
    {  # 0 parametros, sem header — o caso mais comum da conta
        "name": "aviso_simples", "language": "pt_BR", "status": "APPROVED", "category": "UTILITY",
        "components": [{"type": "BODY", "text": "Ola! Retomando nosso contato."}],
    },
    {  # 3 parametros, HEADER TEXT ESTATICO (unico header real da conta)
        "name": "confirmacao_reserva", "language": "pt_BR", "status": "APPROVED", "category": "UTILITY",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "Brasileiros no Atacama"},
            {"type": "BODY", "text": "Ola {{1}}! Reserva #{{2}} para {{3}} confirmada."},
            {"type": "FOOTER", "text": "Equipe BnA"},
        ],
    },
    {  # MESMO name, outro idioma: prova que a chave e (name, language)
        "name": "confirmacao_reserva", "language": "en_US", "status": "APPROVED", "category": "UTILITY",
        "components": [{"type": "BODY", "text": "Hi {{1}}! Booking #{{2}} for {{3}} confirmed."}],
    },
    {  # nao aprovado: nao pode aparecer nem ser enviado
        "name": "promo_pendente", "language": "pt_BR", "status": "PENDING", "category": "MARKETING",
        "components": [{"type": "BODY", "text": "Promocao {{1}}"}],
    },
    {  # estrutura fora das capacidades deste pacote -> listado como indisponivel
        "name": "com_header_midia", "language": "pt_BR", "status": "APPROVED", "category": "MARKETING",
        "components": [
            {"type": "HEADER", "format": "IMAGE"},
            {"type": "BODY", "text": "Veja a foto"},
        ],
    },
]

meta_state = {"raw": META_RAW, "fail": False}


async def _fake_fetch(base_url, waba_id, headers):
    if meta_state["fail"]:
        import httpx
        raise httpx.TimeoutException("meta indisponivel (simulado)")
    return meta_state["raw"]


meta_templates._fetch_meta_templates = _fake_fetch


def reset_catalog():
    meta_templates.invalidate_catalog_cache()


# --- Setup -----------------------------------------------------------------
Base.metadata.create_all(bind=engine)


class _DummyUser:
    id = 1
    email = "tester@local"
    is_admin = True
    nome = "Tester"


main.app.dependency_overrides[get_current_user] = lambda: _DummyUser()
client = TestClient(main.app)


async def _noop(*a, **k):
    return None


async def _noop_false(*a, **k):
    return False


wh.whatsapp.mark_as_read = _noop
wh.crm_service.auto_link_conversation = _noop_false

s = SessionLocal()
s.add(AutoReply(trigger="end_service", title="Encerramento", message="Atendimento encerrado.", is_active=True))
# CONV-CURATION-01: APPROVED na Meta ja nao basta — o template tambem precisa
# estar autorizado no atendimento. Esta suite prova a JANELA de 24h, entao
# autoriza tudo que a Meta aprovou e deixa a curadoria para
# tests/test_conversas_template_curation.py. `promo_pendente` fica de fora por
# nao ser APPROVED: e o proprio caso de "autorizar nao sobrepoe o status Meta".
from app.models.template import ServiceTemplate  # noqa: E402

for _t in META_RAW:
    if _t["status"] == "APPROVED":
        s.add(ServiceTemplate(name=_t["name"], language=_t["language"]))
s.commit()
s.close()


def make_conv(whatsapp_num, *, ago_hours=None, status="aberta"):
    """Cria uma conversa cuja ultima inbound foi ha `ago_hours` (None => NULL)."""
    sess = SessionLocal()
    try:
        last = None if ago_hours is None else datetime.now(UTC) - timedelta(hours=ago_hours)
        c = Conversation(
            lead_id=0, whatsapp=whatsapp_num, nome=f"Lead {whatsapp_num}",
            status=status, unread_count=0, atendente_id=1, is_bot_active=False,
            last_customer_msg_at=last,
        )
        sess.add(c)
        sess.commit()
        sess.refresh(c)
        return c.id
    finally:
        sess.close()


def get_conv(conv_id):
    sess = SessionLocal()
    try:
        return sess.query(Conversation).filter(Conversation.id == conv_id).first()
    finally:
        sess.close()


def count_messages(conv_id):
    sess = SessionLocal()
    try:
        return sess.query(Message).filter(Message.conversation_id == conv_id).count()
    finally:
        sess.close()


def last_message(conv_id):
    sess = SessionLocal()
    try:
        return (
            sess.query(Message)
            .filter(Message.conversation_id == conv_id)
            .order_by(Message.id.desc())
            .first()
        )
    finally:
        sess.close()


def is_window_closed(resp):
    if resp.status_code != 409:
        return False
    d = resp.json().get("detail")
    return isinstance(d, dict) and d.get("code") == "WINDOW_CLOSED"


# ===========================================================================
print("W — regra pura de 24h (mutations A, B, C, K)")

check(service_window_open(NOW - timedelta(hours=23, minutes=59), NOW) is True,
      "23h59 -> ABERTA")
check(service_window_open(NOW - timedelta(hours=24), NOW) is False,
      "exatamente 24h -> FECHADA (mata `<` virando `<=`)")
check(service_window_open(NOW - timedelta(hours=24, minutes=1), NOW) is False,
      "24h01 -> FECHADA")
check(service_window_open(None, NOW) is False,
      "last_customer_msg_at NULL -> FECHADA")
check(service_window_open(NOW - timedelta(hours=47), NOW) is False,
      "47h -> FECHADA (mata 24h virando 48h)")
# naive (SQLite/CI) nao pode levantar TypeError
naive = (NOW - timedelta(hours=1)).replace(tzinfo=None)
try:
    check(service_window_open(naive, NOW) is True, "timestamp naive tratado como UTC (SQLite)")
except TypeError:
    check(False, "timestamp naive tratado como UTC (SQLite)")
# a property do model usa a MESMA funcao
cid_naive = make_conv("5511900000000", ago_hours=1)
check(get_conv(cid_naive).service_window_open is True,
      "property do model concorda com a funcao pura (leitura via SQLite)")

# ===========================================================================
print("\nG — backend bloqueia free-form fora da janela (mutations D, E, F, G)")

before = dict(calls)
cid_fechada = make_conv("5511900000001", ago_hours=25)
check(get_conv(cid_fechada).service_window_open is False, "conversa de 25h nasce FECHADA")

r = client.post(f"/api/conversations/{cid_fechada}/messages",
                json={"content": "texto livre", "msg_type": "text"})
check(is_window_closed(r), f"texto fora da janela -> 409 WINDOW_CLOSED (veio {r.status_code})")
check(count_messages(cid_fechada) == 0, "texto bloqueado NAO persiste Message")
check(calls["text"] == before["text"], "texto bloqueado NAO chama a Meta")

r = client.post(f"/api/conversations/{cid_fechada}/messages/media",
                files={"file": ("a.jpg", b"\xff\xd8\xff-fake", "image/jpeg")},
                data={"caption": ""})
check(is_window_closed(r), f"midia fora da janela -> 409 WINDOW_CLOSED (veio {r.status_code})")
check(count_messages(cid_fechada) == 0, "midia bloqueada NAO persiste Message")
check(calls["upload"] == before["upload"] and calls["media"] == before["media"],
      "midia bloqueada NAO faz upload nem envio a Meta")

r = client.post(f"/api/conversations/{cid_fechada}/messages/media",
                files={"file": ("a.ogg", b"OggS-fake", "audio/ogg")},
                data={"caption": ""})
check(is_window_closed(r), "audio fora da janela -> 409 WINDOW_CLOSED")

# retry de uma mensagem que falhou antes da janela fechar
sess = SessionLocal()
old = Message(conversation_id=cid_fechada, direction="outbound", content="antiga",
              msg_type="text", status="failed")
sess.add(old)
sess.commit()
old_id = old.id
sess.close()
r = client.post(f"/api/conversations/{cid_fechada}/messages/{old_id}/retry")
check(is_window_closed(r), f"retry fora da janela -> 409 WINDOW_CLOSED (veio {r.status_code})")
check(calls["text"] == before["text"], "retry bloqueado NAO chama a Meta")

# dentro da janela o caminho normal segue intacto
cid_aberta = make_conv("5511900000002", ago_hours=1)
r = client.post(f"/api/conversations/{cid_aberta}/messages",
                json={"content": "oi", "msg_type": "text"})
check(r.status_code == 200, f"texto DENTRO da janela continua funcionando (veio {r.status_code})")
check(calls["text"] == before["text"] + 1, "texto dentro da janela chama a Meta exatamente 1x")

# ===========================================================================
print("\nT — template com a janela fechada (mutations H, I, J, Q)")
reset_catalog()

r = client.get("/api/templates/meta/approved")
check(r.status_code == 200, "catalogo Meta responde 200")
names = [(t["name"], t["language"]) for t in r.json()["templates"]]
check(("promo_pendente", "pt_BR") not in names, "template PENDING nao aparece no catalogo")
check(("aviso_simples", "pt_BR") in names, "template APPROVED aparece")
check(("confirmacao_reserva", "pt_BR") in names and ("confirmacao_reserva", "en_US") in names,
      "mesmo name em dois idiomas aparece como DUAS entradas")
unsupported = [t for t in r.json()["templates"] if not t["supported"]]
check(len(unsupported) == 1 and unsupported[0]["name"] == "com_header_midia",
      "header de midia listado como INDISPONIVEL, nao escondido")
check(bool(unsupported[0]["unsupported_reason"]), "indisponivel traz motivo explicito")
by_key = {(t["name"], t["language"]): t for t in r.json()["templates"]}
check(by_key[("confirmacao_reserva", "pt_BR")]["body_params"] == 3,
      "aridade lida dos components reais da Meta")
check(by_key[("aviso_simples", "pt_BR")]["body_params"] == 0, "template sem parametros -> 0")

t_calls = calls["template"]
snapshot = get_conv(cid_fechada).last_customer_msg_at
r = client.post(f"/api/conversations/{cid_fechada}/messages",
                json={"content": "ignorado", "msg_type": "template",
                      "template_name": "aviso_simples", "template_language": "pt_BR",
                      "template_params": []})
check(r.status_code == 200, f"template APPROVED PERMITIDO com janela fechada (veio {r.status_code})")
check(calls["template"] == t_calls + 1, "template chamou a Meta 1x")
msg = last_message(cid_fechada)
check(msg.msg_type == "template" and msg.status == "sent" and msg.whatsapp_msg_id,
      "template persistido com msg_type='template', status e wamid")
conv_after = get_conv(cid_fechada)
check(conv_after.last_customer_msg_at == snapshot,
      "template NAO escreve last_customer_msg_at (mata mutation H)")
check(conv_after.service_window_open is False,
      "template enviado NAO reabre a janela (mata mutation Q)")

r = client.post(f"/api/conversations/{cid_fechada}/messages",
                json={"content": "corpo", "msg_type": "template",
                      "template_name": "promo_pendente", "template_language": "pt_BR",
                      "template_params": ["x"]})
check(r.status_code == 404, f"template PENDING recusado (veio {r.status_code})")
check(count_messages(cid_fechada) == 2, "template recusado nao cria Message")

r = client.post(f"/api/conversations/{cid_fechada}/messages",
                json={"content": "Veja a foto", "msg_type": "template",
                      "template_name": "com_header_midia", "template_language": "pt_BR",
                      "template_params": []})
check(r.status_code == 422, "template com estrutura nao suportada recusado (422)")

# fallback de template -> texto livre NAO PODE existir em lugar nenhum
js = (ROOT / "conversas" / "static" / "js" / "conversas.js").read_text(encoding="utf-8")
check("msg_type: 'text'" not in js.split("function templateItemEl")[0].split("sendTemplate")[-1],
      "seletor de template nao possui fallback para texto (mata mutation J)")
check(js.count("Mensagem enviada como texto") == 0,
      "o fallback antigo 'enviar body_text como texto' foi REMOVIDO")

# ===========================================================================
print("\nP — aridade dos parametros validada no backend (mutation N)")
t_calls = calls["template"]
for params, expect, label in (
    (["Ana", "123"], 422, "N-1 parametros -> 422"),
    (["Ana", "123", "15/05"], 200, "N parametros -> 200"),
    (["Ana", "123", "15/05", "extra"], 422, "N+1 parametros -> 422"),
):
    r = client.post(f"/api/conversations/{cid_fechada}/messages",
                    json={"content": "x", "msg_type": "template",
                          "template_name": "confirmacao_reserva", "template_language": "pt_BR",
                          "template_params": params})
    check(r.status_code == expect, f"{label} (veio {r.status_code})")
check(calls["template"] == t_calls + 1, "so a aridade correta chegou a Meta")

msg = last_message(cid_fechada)
check("{{1}}" not in msg.content and "Ana" in msg.content,
      "historico guarda o corpo RENDERIZADO, nao os placeholders")

# ===========================================================================
print("\nL — (name, language) e a chave (mutation implicita)")
t_calls = calls["template"]
r = client.post(f"/api/conversations/{cid_fechada}/messages",
                json={"content": "x", "msg_type": "template",
                      "template_name": "aviso_simples", "template_language": "en_US",
                      "template_params": []})
check(r.status_code == 404,
      f"name existente + idioma inexistente -> 404 (veio {r.status_code})")
check(calls["template"] == t_calls, "par invalido nao chega a Meta")
r = client.post(f"/api/conversations/{cid_fechada}/messages",
                json={"content": "x", "msg_type": "template",
                      "template_name": "confirmacao_reserva", "template_language": "en_US",
                      "template_params": ["Ann", "9", "May 5"]})
check(r.status_code == 200, "mesmo name em en_US e um template proprio e valido")

# ===========================================================================
print("\nR — inbound reabre a janela; reaction NAO (mutations C, M)")


def inbound(numero, tipo, extra=None, msg_id=None, ts_offset_h=0):
    # AUDIT-2026-08-W1D-orq: era "timestamp": "1" fixo — epoch 1, ou seja
    # 1970-01-01. Servia enquanto o webhook lia o campo e o descartava; agora que
    # a janela e ancorada no relogio da META (webhook.py:_customer_msg_at), "1"
    # significa "mensagem de 1970", que corretamente NAO reabre uma janela de 24h.
    # A Meta manda o epoch do momento do envio, entao a fixture manda o mesmo.
    # `ts_offset_h` permite exercitar o passado de proposito (ver bloco R2).
    ts = int((datetime.now(UTC) - timedelta(hours=ts_offset_h)).timestamp())
    body = {"entry": [{"changes": [{"value": {
        "contacts": [{"profile": {"name": "Cliente"}}],
        "messages": [dict({"from": numero, "id": msg_id or f"wamid.IN_{numero}_{tipo}_{_wamid_seq['n']}",
                           "type": tipo, "timestamp": str(ts)}, **(extra or {}))],
    }}]}]}
    _wamid_seq["n"] += 1
    return client.post("/webhook", json=body)


TIPOS = [
    ("text", {"text": {"body": "oi"}}),
    ("image", {"image": {"id": "m1", "mime_type": "image/jpeg"}}),
    ("audio", {"audio": {"id": "m2", "mime_type": "audio/ogg"}}),
    ("video", {"video": {"id": "m3", "mime_type": "video/mp4"}}),
    ("document", {"document": {"id": "m4", "mime_type": "application/pdf"}}),
    ("location", {"location": {"latitude": 1, "longitude": 2}}),
    ("contacts", {"contacts": [{"name": {"formatted_name": "X"}}]}),
    ("sticker", {"sticker": {"id": "m5"}}),
    ("interactive", {"interactive": {"type": "button_reply", "button_reply": {"title": "Sim"}}}),
    ("desconhecido", {}),
]
for i, (tipo, extra) in enumerate(TIPOS):
    numero = f"55119111000{i:02d}"
    cid = make_conv(numero, ago_hours=30)
    check(get_conv(cid).service_window_open is False, f"[{tipo}] parte de janela FECHADA")
    inbound(numero, tipo, extra)
    check(get_conv(cid).service_window_open is True, f"[{tipo}] inbound REABRE a janela")

# ---------------------------------------------------------------------------
# R2 — a ancora e o relogio da META, nao o nosso (AUDIT-2026-08-W1D F6)
#
# Sem estes tres checks a correcao F6 nao tem cobertura: bastava alguem voltar a
# ancorar em datetime.now() para o bloco R acima seguir verde.
print("\nR2 — a janela segue o timestamp da Meta, e a ancora nao anda para tras")

_n_velha = "5511922200001"
_cid_velha = make_conv(_n_velha, ago_hours=30)
check(get_conv(_cid_velha).service_window_open is False, "parte de janela FECHADA")
# Mensagem cujo timestamp da META e de 30h atras: chegou agora (fila/reentrega),
# mas para a Meta ela e velha. Ancorar em now() abriria a janela indevidamente e
# o operador levaria 131047 na cara ao mandar texto livre.
inbound(_n_velha, "text", {"text": {"body": "atrasada"}}, ts_offset_h=30)
check(get_conv(_cid_velha).service_window_open is False,
      "inbound com timestamp ANTIGO da Meta NAO reabre a janela")

# E o contrario: nova abre, e a reentrega da antiga nao pode encolher.
inbound(_n_velha, "text", {"text": {"body": "recente"}}, ts_offset_h=0)
check(get_conv(_cid_velha).service_window_open is True,
      "inbound com timestamp atual reabre a janela")
inbound(_n_velha, "text", {"text": {"body": "reentrega antiga"}}, ts_offset_h=40)
check(get_conv(_cid_velha).service_window_open is True,
      "reentrega antiga NAO encolhe a janela aberta pela mensagem mais nova")

numero_r = "5511922200000"
cid_r = make_conv(numero_r, ago_hours=30)
antes_reaction = get_conv(cid_r).last_customer_msg_at
inbound(numero_r, "reaction", {"reaction": {"message_id": "wamid.X", "emoji": "\U0001f44d"}})
conv_r = get_conv(cid_r)
check(conv_r.service_window_open is False, "reaction NAO reabre a janela (decisao conservadora)")
check(conv_r.last_customer_msg_at == antes_reaction,
      "reaction deixa last_customer_msg_at INTACTO (nao avanca o relogio)")
sess = SessionLocal()
n_react = sess.query(Message).filter(Message.conversation_id == cid_r,
                                     Message.msg_type == "reaction").count()
sess.close()
check(n_react == 1, "reaction continua persistida no historico (so nao move a janela)")

# reaction como PRIMEIRA inbound de uma conversa nova -> nasce fechada
inbound("5511933300000", "reaction", {"reaction": {"message_id": "wamid.Y", "emoji": "❤"}})
sess = SessionLocal()
nova = sess.query(Conversation).filter(Conversation.whatsapp == "5511933300000").first()
sess.close()
check(nova is not None and nova.service_window_open is False,
      "conversa criada por reaction nasce com a janela FECHADA")

# duplicata da Meta nao estende a janela
numero_d = "5511944400000"
cid_d = make_conv(numero_d, ago_hours=30)
inbound(numero_d, "text", {"text": {"body": "um"}}, msg_id="wamid.DUP")
t1 = get_conv(cid_d).last_customer_msg_at
inbound(numero_d, "text", {"text": {"body": "um"}}, msg_id="wamid.DUP")
check(get_conv(cid_d).last_customer_msg_at == t1, "webhook duplicado nao estende a janela")

# status webhook nao mexe na janela
client.post("/webhook", json={"entry": [{"changes": [{"value": {
    "statuses": [{"id": "wamid.DUP", "status": "delivered"}]}}]}]})
check(get_conv(cid_d).last_customer_msg_at == t1, "status webhook nao mexe na janela")

# ===========================================================================
print("\nI — /initiate sem inbound deixa a janela FECHADA (mutation P)")
r = client.post("/api/conversations/initiate",
                json={"whatsapp": "5511955500000", "nome": "Novo Lead", "lead_id": 0})
check(r.status_code == 200, "/initiate cria a conversa")
cid_new = r.json()["conversation_id"]
check(get_conv(cid_new).last_customer_msg_at is None, "/initiate nao inventa inbound")
check(get_conv(cid_new).service_window_open is False,
      "conversa nova (sem inbound) tem a janela FECHADA")
r = client.get(f"/api/conversations/{cid_new}")
check(r.json()["service_window_open"] is False,
      "o detalhe entrega service_window_open=false -> composer cai no bloco de template")
r = client.post(f"/api/conversations/{cid_new}/messages",
                json={"content": "primeira mensagem", "msg_type": "text"})
check(is_window_closed(r), "texto livre em conversa nova e BLOQUEADO")

# /initiate com template invalido nao envia payload adivinhado
t_calls = calls["template"]
r = client.post("/api/conversations/initiate",
                json={"whatsapp": "5511955500001", "template_name": "confirmacao_reserva",
                      "template_language": "pt_BR", "template_params": ["so um"]})
check(r.status_code == 422, f"/initiate valida aridade do template (veio {r.status_code})")
check(calls["template"] == t_calls, "/initiate com aridade errada nao chama a Meta")

# ===========================================================================
print("\nE — end_service respeita a janela, mas o encerramento acontece")
txt_calls = calls["text"]
cid_end = make_conv("5511966600000", ago_hours=30)
r = client.put(f"/api/conversations/{cid_end}", json={"status": "encerrada"})
check(r.status_code == 200, "PUT status=encerrada responde 200")
check(get_conv(cid_end).status == "encerrada",
      "a conversa E ENCERRADA mesmo sem poder enviar a frase automatica")
check(calls["text"] == txt_calls, "frase de encerramento NAO e enviada fora da janela")

cid_end2 = make_conv("5511966600001", ago_hours=2)
client.put(f"/api/conversations/{cid_end2}", json={"status": "encerrada"})
check(calls["text"] == txt_calls + 1, "frase de encerramento E enviada dentro da janela")

# ===========================================================================
print("\nF — falhas nunca liberam texto livre")
reset_catalog()
meta_state["fail"] = True
r = client.get("/api/templates/meta/approved")
check(r.status_code == 503, f"Meta indisponivel -> 503 ao listar (veio {r.status_code})")
r = client.post(f"/api/conversations/{cid_fechada}/messages",
                json={"content": "texto", "msg_type": "text"})
check(is_window_closed(r), "com a Meta fora do ar, texto livre CONTINUA bloqueado")
meta_state["fail"] = False
reset_catalog()

whatsapp.send_template_message = _counting_sender("template", FAIL_RESPONSE)
n_before = count_messages(cid_fechada)
r = client.post(f"/api/conversations/{cid_fechada}/messages",
                json={"content": "x", "msg_type": "template",
                      "template_name": "aviso_simples", "template_language": "pt_BR",
                      "template_params": []})
check(r.status_code == 502, f"falha ao enviar template -> 502 (veio {r.status_code})")
msg = last_message(cid_fechada)
check(msg.status == "failed" and count_messages(cid_fechada) == n_before + 1,
      "template falho persiste 'failed' — nunca um 'sent' falso")
check(get_conv(cid_fechada).service_window_open is False,
      "template falho mantem a janela fechada")
r = client.post(f"/api/conversations/{cid_fechada}/messages",
                json={"content": "texto", "msg_type": "text"})
check(is_window_closed(r), "apos falha de template, texto livre continua bloqueado")
whatsapp.send_template_message = _counting_sender("template")

# legado: dict de erro truthy nao pode virar 200
from app.models.template import MessageTemplate  # noqa: E402
sess = SessionLocal()
sess.add(MessageTemplate(name="legado_aprovado", category="UTILITY", language="pt_BR",
                         status="APPROVED", body_text="corpo"))
sess.commit()
tid = sess.query(MessageTemplate).filter(MessageTemplate.name == "legado_aprovado").first().id
sess.close()
whatsapp.send_template_message = _counting_sender("template", FAIL_RESPONSE)
r = client.post(f"/api/templates/{tid}/send", json={"to": "5511900000009"})
check(r.status_code == 502,
      f"/api/templates/id/send devolve erro quando a Meta falha (veio {r.status_code})")
whatsapp.send_template_message = _counting_sender("template")

# ===========================================================================
print("\nU — o frontend nao reimplementa 24h e nao deixa atalho aberto (mutations G, L, O)")
check("service_window_open" in js, "o JS consome service_window_open do backend")
# Recalcular 24h no cliente exigiria LER last_customer_msg_at. Se o campo nunca e
# acessado, nao ha como o frontend discordar do backend no minuto da virada.
# (`.` a esquerda: acesso de propriedade, nao a mencao em comentario.)
check(".last_customer_msg_at" not in js and "['last_customer_msg_at']" not in js,
      "o JS NAO le last_customer_msg_at — impossivel recalcular 24h no cliente")
check("windowClosed()" in js and "applyWindowState" in js, "estado de janela aplicado no composer")
for guard in ("if (windowClosed()) { applyWindowState(activeConversation); return; }",):
    check(js.count(guard) >= 4,
          "texto, Enter, midia e retry checam a janela antes de enviar")
# A CONDICAO do polling, nao so a declaracao da variavel: trocar o `||` por nada
# deixaria `windowChanged` declarada e um teste de presenca passaria em falso.
check("service_window_open !== data.service_window_open" in js
      and "if (newCount !== oldCount || windowChanged) {" in js,
      "polling re-renderiza quando a janela vira, sem mensagem nova (mata mutation L)")
# `btn.disabled = true` sozinho aparece em outros pontos do arquivo (player de
# audio, por exemplo). A sequencia com `tplSending` prende o guard ao envio de
# template — que e o unico lugar onde o duplo clique dispara dois envios.
check("tplSending = true;\n        btn.disabled = true;" in js,
      "guard de duplo clique no envio de template (mata mutation O)")
check("WINDOW_CLOSED" in js, "frontend distingue WINDOW_CLOSED de falha generica da Meta")

py = (ROOT / "conversas" / "app" / "routers" / "conversations.py").read_text(encoding="utf-8")
check(py.count("_require_open_window(conversation)") == 3,
      "o guard esta nas 3 rotas de operador (mata mutations D, E, F)")
wh_py = (ROOT / "conversas" / "app" / "routers" / "webhook.py").read_text(encoding="utf-8")
check("opens_window" in wh_py, "webhook diferencia reaction dos demais tipos")

# ===========================================================================
print("\nB — BIA / auto-replies / n8n intocados")
check("_require_open_window" not in wh_py,
      "nenhum guard no webhook: BIA, greeting, waiting e out_of_hours seguem iguais")
wa_py = (ROOT / "conversas" / "app" / "services" / "whatsapp.py").read_text(encoding="utf-8")
check("service_window_open" not in wa_py,
      "nenhum guard em whatsapp.send_* (sem query extra por envio, sem afetar n8n)")

# ===========================================================================
print()
if failures:
    print(f"FALHAS: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("TODOS OS INVARIANTES DA JANELA DE 24H PASSARAM")
