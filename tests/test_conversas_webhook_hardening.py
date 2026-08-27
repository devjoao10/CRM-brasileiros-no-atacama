"""
AUDIT-2026-08-W1D — Regressao do endurecimento do webhook do Conversas.

`POST /webhook` e o UNICO ponto de entrada publico nao autenticado do sistema.
Este teste fixa as sete correcoes da wave 1D:

  F1  isolamento por mensagem + 503 so quando a falha e de INFRAESTRUTURA
  F2  a saudacao automatica nao pode mais engolir o lote pendente da Bia
  F3  sem token da Meta fora de development -> outbound 'failed', nunca 'sent'
  F5  status de entrega so AVANCA (sent < delivered < read; failed terminal)
  F6  janela de 24h ancorada no timestamp da META, e nunca anda para tras
  F8  historico enviado ao n8n e limitado e exclui outbound 'failed'

Sem HMAC (development, sem META_APP_SECRET). Toda a rede e neutralizada.

Roda standalone:  python tests/test_conversas_webhook_hardening.py
"""
import asyncio
import logging
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone as tz

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_webhook_hardening_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""       # sem HMAC em development
os.environ["META_ACCESS_TOKEN"] = ""     # sem credenciais Meta (F3)
os.environ["META_PHONE_NUMBER_ID"] = ""
os.environ["N8N_AGENT_ENABLED"] = "true"  # F2 precisa do agente habilitado

sys.path.insert(0, str(CONVERSAS_DIR))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import exc as sa_exc  # noqa: E402

import app.main as main  # noqa: E402
import app.routers.webhook as wh  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.models.api_config import ApiConfig  # noqa: E402
from app.models.auto_reply import AutoReply  # noqa: E402
from app.models.conversation import Conversation, Message, service_window_open  # noqa: E402
from app.services.outbound import record_outbound_message  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


# ─── Neutraliza TODA a rede ──────────────────────────────────────────────
_wamid_seq = {"n": 0}


async def _noop(*a, **k):
    return None


async def _noop_false(*a, **k):
    return False


async def _fake_send(*a, **k):
    """Envio 'aceito pela Meta' com wamid unico (whatsapp_msg_id e UNIQUE)."""
    _wamid_seq["n"] += 1
    return {"messages": [{"id": f"wamid.FAKEOUT{_wamid_seq['n']}"}]}


_orig_send_text = wh.whatsapp.send_text_message
_orig_customer_msg_at = wh._customer_msg_at
_orig_schedule = wh._schedule_agent_debounce

wh.whatsapp.mark_as_read = _noop
wh.whatsapp.send_text_message = _fake_send
wh.crm_service.auto_link_conversation = _noop_false

# O debounce real criaria tasks de 15s dentro do loop do TestClient. Capturamos
# a chamada (inclusive o CUTOFF do F2) e executamos `_debounce_then_forward`
# manualmente onde o teste precisa.
scheduled = []


def _capture_schedule(conversation_id):
    # O corte do F2 viaja em `_debounce_cutoffs` (registrado ANTES da resposta
    # automatica); e exatamente ele que o debounce real leria.
    scheduled.append((conversation_id, wh._debounce_cutoffs.get(conversation_id)))


wh._schedule_agent_debounce = _capture_schedule

Base.metadata.create_all(bind=engine)
client = TestClient(main.app)


def _session():
    return SessionLocal()


def _inbound(numero, texto, msg_id, timestamp=None, extra=None):
    m = {"from": numero, "type": "text", "text": {"body": texto},
         "timestamp": str(int(timestamp if timestamp is not None
                             else datetime.now(tz.utc).timestamp()))}
    if msg_id is not None:
        m["id"] = msg_id
    if extra:
        m.update(extra)
    return m


def _payload(*mensagens, nome="Cliente"):
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"profile": {"name": nome}}],
        "messages": list(mensagens),
    }}]}]}


# =====================================================================
# F1-a — uma mensagem venenosa NAO derruba as irmas do mesmo lote
# =====================================================================
print("AUDIT-2026-08-W1D — F1: mensagem venenosa nao aborta o lote")


def _poison_on(poison_ids, error_factory):
    """Faz `_customer_msg_at` explodir so para ids marcados (caminho por-mensagem)."""
    def _patched(msg):
        if msg.get("id") in poison_ids:
            raise error_factory()
        return _orig_customer_msg_at(msg)
    return _patched


wh._customer_msg_at = _poison_on({"wamid.POISON1"}, lambda: RuntimeError("boom na 1a msg"))
r = client.post("/webhook", json=_payload(
    _inbound("5511900000001", "primeira (vai explodir)", "wamid.POISON1"),
    _inbound("5511900000001", "segunda (deve sobreviver)", "wamid.SURVIVOR1"),
))
wh._customer_msg_at = _orig_customer_msg_at

check(r.status_code == 200,
      f"falha de DADOS numa mensagem -> 200 (Meta nao deve reentregar) (got {r.status_code})")
s = _session()
check(s.query(Message).filter(Message.whatsapp_msg_id == "wamid.POISON1").first() is None,
      "mensagem venenosa nao foi persistida")
check(s.query(Message).filter(Message.whatsapp_msg_id == "wamid.SURVIVOR1").first() is not None,
      "IRMA do mesmo lote foi persistida mesmo apos a falha da primeira")
s.close()


# =====================================================================
# F1-b — falha de INFRA pede reentrega (non-2xx); malformada nao
# =====================================================================
print("\nAUDIT-2026-08-W1D — F1: infra -> non-2xx; malformada -> 200")


def _db_down():
    return sa_exc.OperationalError("SELECT 1", {}, Exception("database is down"))


wh._customer_msg_at = _poison_on({"wamid.INFRA1"}, _db_down)
r_infra = client.post("/webhook", json=_payload(
    _inbound("5511900000002", "cai o banco", "wamid.INFRA1"),
))
wh._customer_msg_at = _orig_customer_msg_at

check(r_infra.status_code >= 500,
      f"falha de INFRA -> non-2xx para a Meta reentregar (got {r_infra.status_code})")
s = _session()
check(s.query(Message).filter(Message.whatsapp_msg_id == "wamid.INFRA1").first() is None,
      "mensagem da falha de infra nao ficou meio-persistida")
s.close()

# Malformada de verdade: `text` e string, `.get("body")` estoura AttributeError.
r_bad = client.post("/webhook", json=_payload(
    {"from": "5511900000003", "id": "wamid.MALFORMED1", "type": "text",
     "timestamp": "1700000000", "text": "isto deveria ser um objeto"},
))
check(r_bad.status_code == 200,
      f"mensagem MALFORMADA -> 200 (reentregar nao consertaria) (got {r_bad.status_code})")

# Lote misto: infra + boa. A boa persiste E a Meta ainda recebe non-2xx.
wh._customer_msg_at = _poison_on({"wamid.INFRA2"}, _db_down)
r_mix = client.post("/webhook", json=_payload(
    _inbound("5511900000004", "boa", "wamid.MIXOK"),
    _inbound("5511900000004", "infra", "wamid.INFRA2"),
))
wh._customer_msg_at = _orig_customer_msg_at
check(r_mix.status_code >= 500, f"lote com falha de infra -> non-2xx (got {r_mix.status_code})")
s = _session()
check(s.query(Message).filter(Message.whatsapp_msg_id == "wamid.MIXOK").first() is not None,
      "mensagem boa do lote misto persistiu (reentrega e idempotente por wamid)")
s.close()


# =====================================================================
# F2 — saudacao dispara E a Bia ainda recebe o lote pendente
# =====================================================================
print("\nAUDIT-2026-08-W1D — F2: saudacao nao pode zerar o lote da Bia")

s = _session()
s.add(AutoReply(trigger="greeting", title="Saudação",
                message="Olá! Recebemos sua mensagem.", is_active=True))
s.commit()
s.close()

scheduled.clear()
NUM_F2 = "5511911110000"
r_f2 = client.post("/webhook", json=_payload(
    _inbound(NUM_F2, "quero saber o preco do frete", "wamid.F2_1"),
))
check(r_f2.status_code == 200, f"inbound de conversa NOVA -> 200 (got {r_f2.status_code})")

s = _session()
conv_f2 = s.query(Conversation).filter(Conversation.whatsapp == NUM_F2).first()
greet = s.query(Message).filter(
    Message.conversation_id == conv_f2.id, Message.direction == "outbound",
).first()
s.close()

check(greet is not None, "a saudacao automatica REALMENTE disparou (pre-condicao do bug)")
check(len(scheduled) == 1, f"o agente foi agendado apesar da saudacao (got {len(scheduled)})")

# Executa o debounce de verdade com o cutoff que o webhook capturou.
forwarded = []


async def _capture_forward(conversation, message_text, db):
    forwarded.append(message_text)


_orig_forward = wh._forward_to_agent
wh._forward_to_agent = _capture_forward
wh.AGENT_DEBOUNCE_SECONDS = 0
if scheduled:
    _cid, _cutoff = scheduled[0]
    asyncio.run(wh._debounce_then_forward(_cid, _cutoff))
wh._forward_to_agent = _orig_forward

check(len(forwarded) == 1,
      f"o lote pendente NAO veio vazio — a Bia foi chamada (got {len(forwarded)} chamada(s))")
check(bool(forwarded) and "quero saber o preco do frete" in forwarded[0],
      f"a 1a mensagem do lead novo chegou ao agente (got {forwarded[0] if forwarded else None!r})")

s = _session()
s.query(AutoReply).filter(AutoReply.trigger == "greeting").delete()
s.commit()
s.close()


# =====================================================================
# F3 — sem token da Meta fora de development -> 'failed', nunca 'sent'
# =====================================================================
print("\nAUDIT-2026-08-W1D — F3: token ausente nao pode virar 'sent'")

s = _session()
conv_f3 = Conversation(lead_id=0, whatsapp="5511933330000", nome="F3", status="aberta")
s.add(conv_f3)
s.commit()
s.refresh(conv_f3)

# development: caminho simulado continua existindo, mas com status PROPRIO.
wh.whatsapp.ENVIRONMENT = "development"
resp_dev = asyncio.run(_orig_send_text("5511933330000", "oi dev", s))
msg_dev = record_outbound_message(s, conv_f3, "oi dev", "text", resp_dev)
check(msg_dev.status == "simulated",
      f"development sem token -> status 'simulated' (nao 'sent') (got {msg_dev.status!r})")
check(msg_dev.whatsapp_msg_id is None, "simulado nao inventa wamid")

# producao: o caminho simulado deixa de existir — e falha real e visivel.
wh.whatsapp.ENVIRONMENT = "production"
resp_prod = asyncio.run(_orig_send_text("5511933330000", "oi prod", s))
check(isinstance(resp_prod, dict) and resp_prod.get("error") is True,
      f"producao sem token -> resultado de ERRO (got {resp_prod!r})")
check(not (isinstance(resp_prod, dict) and resp_prod.get("simulated")),
      "producao NUNCA devolve o marcador 'simulated'")
msg_prod = record_outbound_message(s, conv_f3, "oi prod", "text", resp_prod)
check(msg_prod.status == "failed",
      f"producao sem token -> Message persistida como 'failed' (got {msg_prod.status!r})")
check(bool(msg_prod.last_error), "a falha registra last_error para o operador ver")

# F4: send_reaction respeita o mesmo contrato (nao devolve mais None cru).
reac = asyncio.run(wh.whatsapp.send_reaction("wamid.X", "\U0001f44d", "5511933330000", s))
check(isinstance(reac, dict) and reac.get("error") is True,
      f"F4: send_reaction sem credenciais devolve o contrato padrao (got {reac!r})")

wh.whatsapp.ENVIRONMENT = "development"
s.close()


# =====================================================================
# F5 — status de entrega so AVANCA
# =====================================================================
print("\nAUDIT-2026-08-W1D — F5: status de entrega nunca regride")


def _status_payload(wamid, status):
    return {"entry": [{"changes": [{"value": {
        "statuses": [{"id": wamid, "status": status, "timestamp": "1700000000"}],
    }}]}]}


def _status_of(wamid):
    s = _session()
    try:
        m = s.query(Message).filter(Message.whatsapp_msg_id == wamid).first()
        return m.status if m else None
    finally:
        s.close()


s = _session()
conv_f5 = Conversation(lead_id=0, whatsapp="5511944440000", nome="F5", status="aberta")
s.add(conv_f5)
s.commit()
s.refresh(conv_f5)
s.add_all([
    Message(conversation_id=conv_f5.id, direction="outbound", content="a",
            msg_type="text", whatsapp_msg_id="wamid.F5_A", status="sent"),
    Message(conversation_id=conv_f5.id, direction="outbound", content="b",
            msg_type="text", whatsapp_msg_id="wamid.F5_B", status="sent"),
])
s.commit()
s.close()

client.post("/webhook", json=_status_payload("wamid.F5_A", "read"))
check(_status_of("wamid.F5_A") == "read", "'sent' -> 'read' avanca normalmente")
client.post("/webhook", json=_status_payload("wamid.F5_A", "delivered"))
check(_status_of("wamid.F5_A") == "read",
      f"'delivered' ATRASADO nao regride um 'read' (got {_status_of('wamid.F5_A')!r})")

client.post("/webhook", json=_status_payload("wamid.F5_B", "failed"))
check(_status_of("wamid.F5_B") == "failed", "'failed' e aplicado")
client.post("/webhook", json=_status_payload("wamid.F5_B", "sent"))
check(_status_of("wamid.F5_B") == "failed",
      f"'sent' VELHO nao apaga um 'failed' terminal (got {_status_of('wamid.F5_B')!r})")
client.post("/webhook", json=_status_payload("wamid.F5_B", "delivered"))
check(_status_of("wamid.F5_B") == "failed", "'delivered' tambem nao apaga o 'failed'")

r_unk = client.post("/webhook", json=_status_payload("wamid.NAO_EXISTE", "delivered"))
check(r_unk.status_code == 200, "status para wamid desconhecido nao derruba o webhook (e e logado)")


# =====================================================================
# F6 — janela ancorada no relogio da META, e nunca anda para tras
# =====================================================================
print("\nAUDIT-2026-08-W1D — F6: janela ancorada no timestamp da Meta")

NUM_F6 = "5511955550000"
now = datetime.now(tz.utc)
ts_23h = now - timedelta(hours=23)

client.post("/webhook", json=_payload(
    _inbound(NUM_F6, "mensagem antiga reentregue", "wamid.F6_1",
             timestamp=ts_23h.timestamp()),
))

s = _session()
conv_f6 = s.query(Conversation).filter(Conversation.whatsapp == NUM_F6).first()
anchor = conv_f6.last_customer_msg_at
if anchor is not None and anchor.tzinfo is None:
    anchor = anchor.replace(tzinfo=tz.utc)
s.close()

check(anchor is not None, "conversa criada com ancora de janela")
check(anchor is not None and abs((anchor - ts_23h).total_seconds()) <= 2,
      f"ancora == timestamp da META, nao o now() do servidor (delta="
      f"{None if anchor is None else round((anchor - ts_23h).total_seconds())}s)")
check(anchor is not None and service_window_open(anchor, now) is True,
      "com 23h a janela ainda esta aberta")
check(anchor is not None and service_window_open(anchor, ts_23h + timedelta(hours=24)) is False,
      "a janela FECHA 24h apos o timestamp da Meta (e nao 24h apos o recebimento)")

# Reentrega de uma mensagem AINDA MAIS antiga nao pode encolher a janela.
client.post("/webhook", json=_payload(
    _inbound(NUM_F6, "outra ainda mais antiga", "wamid.F6_2",
             timestamp=(now - timedelta(hours=40)).timestamp()),
))
s = _session()
conv_f6 = s.query(Conversation).filter(Conversation.whatsapp == NUM_F6).first()
anchor2 = conv_f6.last_customer_msg_at
if anchor2 is not None and anchor2.tzinfo is None:
    anchor2 = anchor2.replace(tzinfo=tz.utc)
s.close()
check(anchor2 is not None and anchor is not None and anchor2 >= anchor,
      "timestamp mais ANTIGO nao move a ancora para tras")


# =====================================================================
# F8 — historico enviado ao n8n: limitado e sem outbound 'failed'
# =====================================================================
print("\nAUDIT-2026-08-W1D — F8: historico do n8n limitado e sem 'failed'")

s = _session()
conv_f8 = Conversation(lead_id=0, whatsapp="5511966660000", nome="F8", status="aberta")
s.add(conv_f8)
s.commit()
s.refresh(conv_f8)
conv_f8_id = conv_f8.id

total = wh.AGENT_HISTORY_LIMIT + 15
for i in range(total):
    s.add(Message(conversation_id=conv_f8_id, direction="inbound",
                  content=f"inbound-{i}", msg_type="text", status="received"))
s.add(Message(conversation_id=conv_f8_id, direction="outbound",
              content="ESTA NUNCA CHEGOU AO CLIENTE", msg_type="text", status="failed"))
s.add(Message(conversation_id=conv_f8_id, direction="outbound",
              content="esta chegou", msg_type="text",
              whatsapp_msg_id="wamid.F8_OK", status="sent"))
s.commit()
s.close()

captured = {}


async def _capture_agent(agent_url, payload, conversation_id):
    # AUDIT-2026-08-F2: `_fetch_agent_parts` passou a devolver (partes, silencio)
    # para distinguir "a Bia decidiu nao responder" de "a Bia nao conseguiu".
    # O stub acompanha a assinatura real — senao o teste morre com ValueError no
    # desempacotamento e os checks de F8 abaixo nunca chegam a rodar.
    captured.update(payload)
    return ["resposta da bia"], False


_orig_fetch = wh._fetch_agent_parts
wh._fetch_agent_parts = _capture_agent
s = _session()
conv_f8 = s.query(Conversation).filter(Conversation.id == conv_f8_id).first()
asyncio.run(wh._forward_to_agent(conv_f8, "ultima do cliente", s))
s.close()
wh._fetch_agent_parts = _orig_fetch

hist = captured.get("historico", [])
check(len(hist) <= wh.AGENT_HISTORY_LIMIT,
      f"historico limitado a {wh.AGENT_HISTORY_LIMIT} (got {len(hist)} de {total + 2} no banco)")
check(all(h["content"] != "ESTA NUNCA CHEGOU AO CLIENTE" for h in hist),
      "outbound 'failed' NAO vai para o agente (o cliente nunca a recebeu)")
check(any(h["content"] == "esta chegou" for h in hist),
      "outbound entregue continua no historico")
# DESC+limit pega as N ULTIMAS; o `.reverse()` devolve a ordem cronologica.
# Se alguem trocar por `.asc().limit()` o agente passa a receber o COMECO da
# conversa (as mensagens mais velhas) — o inverso do que precisa.
_idx = [int(h["content"].split("-")[1]) for h in hist if h["content"].startswith("inbound-")]
check(_idx == sorted(_idx), f"historico em ordem cronologica (got {_idx[:5]}...)")
check(bool(_idx) and max(_idx) == total - 1 and min(_idx) > 0,
      f"o corte mantem as mensagens MAIS NOVAS (min={min(_idx) if _idx else None}, "
      f"max={max(_idx) if _idx else None}, total no banco={total})")


# =====================================================================
# AUDIT-2026-08-WF2 (2) — GET /webhook: segredo conferido em tempo
# constante e sem ecoar no log o que o terceiro mandou
# =====================================================================
# `GET /webhook` e PUBLICO: a assinatura HMAC so existe no POST. Quem chega ali
# escolhe `hub.mode`, `hub.verify_token` e `hub.challenge`, e a rota dizia se o
# palpite estava certo comparando com `==` (sai no primeiro byte diferente) e
# devolvia o palpite inteiro para dentro do log da aplicacao.
print("\nAUDIT-2026-08-WF2 (2) — verificacao do webhook: segredo e log")

VERIFY_TOKEN_FIXTURE = "verify-token-fixture-WF2"  # fixture local, nao e segredo


class _CapturaLog(logging.Handler):
    """Guarda (nivel, mensagem JA FORMATADA) do logger do router do webhook."""

    def __init__(self):
        super().__init__()
        self.registros = []

    def emit(self, record):
        self.registros.append((record.levelname, record.getMessage()))


def _capturar(fn, *args, **kwargs):
    """Roda `fn` e devolve (resultado, registros de log emitidos por wh.logger)."""
    handler = _CapturaLog()
    nivel = wh.logger.level
    wh.logger.addHandler(handler)
    wh.logger.setLevel(logging.DEBUG)
    try:
        return fn(*args, **kwargs), handler.registros
    finally:
        wh.logger.removeHandler(handler)
        wh.logger.setLevel(nivel)


# raise_server_exceptions=False: um 500 tem de aparecer como STATUS para virar
# FAIL com mensagem. Com o default, a excecao subiria e mataria o arquivo
# inteiro — justamente o caso de `hmac.compare_digest` sobre str nao-ASCII.
client_bruto = TestClient(main.app, raise_server_exceptions=False)


def _verify(token, mode="subscribe", challenge="1234567890"):
    params = {"hub.mode": mode, "hub.challenge": challenge}
    if token is not None:
        params["hub.verify_token"] = token
    return client_bruto.get("/webhook", params=params)


# --- servidor SEM verify token configurado nao autoriza ninguem ---
s = _session()
s.query(ApiConfig).delete()
s.commit()
s.close()
check(_verify(VERIFY_TOKEN_FIXTURE).status_code == 403,
      "sem verify token configurado, nenhum palpite passa")
check(_verify("").status_code == 403,
      "sem verify token configurado, nem o token VAZIO passa")

s = _session()
s.add(ApiConfig(id=1, meta_verify_token=VERIFY_TOKEN_FIXTURE))
s.commit()
s.close()

# --- matriz de comparacao ---
r_ok = _verify(VERIFY_TOKEN_FIXTURE)
check(r_ok.status_code == 200, f"token correto -> 200 (veio {r_ok.status_code})")
check(r_ok.json() == 1234567890, f"token correto devolve o challenge (veio {r_ok.text!r})")

check(_verify("token-errado").status_code == 403, "token errado -> 403")
check(_verify(VERIFY_TOKEN_FIXTURE[:-1]).status_code == 403,
      "token que e PREFIXO do correto -> 403")
check(_verify(VERIFY_TOKEN_FIXTURE + "x").status_code == 403,
      "token correto + sufixo -> 403")
check(_verify(None).status_code == 403, "sem hub.verify_token -> 403 (nao estoura)")
check(_verify(VERIFY_TOKEN_FIXTURE, mode="unsubscribe").status_code == 403,
      "hub.mode diferente de 'subscribe' -> 403")

# O caso que `hmac.compare_digest` sobre `str` transformaria em 500: a query
# string aceita qualquer Unicode, e compare_digest com str nao-ASCII levanta
# TypeError. Comparar em BYTES e o que mantem isto num 403.
r_uni = _verify("token-nao-ascii-\u00e7\u00e3o-\U0001f525")
check(r_uni.status_code == 403,
      f"token nao-ASCII -> 403, nunca 500 (veio {r_uni.status_code})")

# --- o log nao pode carregar o que o terceiro mandou ---
TOKEN_ATACANTE = "palpite-do-atacante-NAO-PODE-IR-PRO-LOG"
r_log, regs = _capturar(_verify, TOKEN_ATACANTE)
check(r_log.status_code == 403, "palpite recusado -> 403")
check(bool(regs), f"a recusa CONTINUA sendo registrada (got {regs!r})")
check(all(TOKEN_ATACANTE not in msg for _, msg in regs),
      f"o token submetido NAO aparece no log (got {regs!r})")

MODO_ATACANTE = "modo-do-atacante-TAMBEM-NAO"
_, regs_modo = _capturar(_verify, "x", MODO_ATACANTE)
check(all(MODO_ATACANTE not in msg for _, msg in regs_modo),
      f"o hub.mode submetido tambem NAO aparece no log (got {regs_modo!r})")

# Log injection: a query string aceita quebra de linha, entao um valor ecoado
# no log deixa o terceiro FORJAR uma linha inteira dentro do arquivo de log.
INJECAO = "x\nWARNING:app.routers.webhook:Webhook verificado com sucesso!"
_, regs_inj = _capturar(_verify, INJECAO)
check(all("verificado com sucesso" not in msg for _, msg in regs_inj),
      f"terceiro nao consegue forjar uma linha de log (got {regs_inj!r})")

# Tempo constante nao da para provar por cronometro sem teste instavel; o que
# da para observar EM EXECUCAO e que a conferencia passa por
# `hmac.compare_digest` — o `==` de string sai no primeiro byte diferente e o
# tempo da resposta vaza quanto do segredo o palpite acertou.
_chamadas_compare = []
_orig_compare = wh.hmac.compare_digest


def _spy_compare(a, b):
    _chamadas_compare.append((a, b))
    return _orig_compare(a, b)


wh.hmac.compare_digest = _spy_compare
try:
    r_spy = _verify(VERIFY_TOKEN_FIXTURE)
finally:
    wh.hmac.compare_digest = _orig_compare
check(r_spy.status_code == 200, "o spy nao altera o caminho feliz")
check(bool(_chamadas_compare),
      "a conferencia do verify token passa por hmac.compare_digest, nao por `==`")


# =====================================================================
# AUDIT-2026-08-WF2 (3) — meia-configuracao do Header Auth do agente
# =====================================================================
# Nao e atacavel de fora: e armadilha de deploy. Com so UMA das duas variaveis
# definidas o retorno era `{}`, indistinguivel de "nao configurado". O operador
# entao seguia a ordem documentada, ligava o Header Auth no n8n, e a Bia parava
# de responder a TODOS os clientes sem uma linha sequer no log.
print("\nAUDIT-2026-08-WF2 (3) — meia-configuracao do Header Auth grita no log")

SEGREDO_FIXTURE = "valor-secreto-do-header-NAO-PODE-IR-PRO-LOG"


def _headers_do_agente(nome, valor):
    """Monta o cabecalho do agente com esta configuracao e captura o log."""
    _n, _v = wh.N8N_WEBHOOK_AUTH_HEADER, wh.N8N_WEBHOOK_AUTH_VALUE
    wh.N8N_WEBHOOK_AUTH_HEADER = nome
    wh.N8N_WEBHOOK_AUTH_VALUE = valor
    try:
        return _capturar(wh._agent_auth_headers)
    finally:
        wh.N8N_WEBHOOK_AUTH_HEADER = _n
        wh.N8N_WEBHOOK_AUTH_VALUE = _v


_h, _regs = _headers_do_agente("", "")
check(_h == {}, f"nada configurado -> nenhum cabecalho (got {_h!r})")
check(_regs == [], f"nada configurado -> SILENCIO no log (got {_regs!r})")

_h, _regs = _headers_do_agente("X-BnA-Webhook-Token", SEGREDO_FIXTURE)
check(_h == {"X-BnA-Webhook-Token": SEGREDO_FIXTURE},
      f"tudo configurado -> o cabecalho e montado (got {_h!r})")
check(_regs == [], f"tudo configurado -> nenhum aviso (got {_regs!r})")

_h, _regs = _headers_do_agente("X-BnA-Webhook-Token", "")
check(_h == {}, f"NOME sem valor -> retorno inalterado, dict vazio (got {_h!r})")
check(any(nivel == "WARNING" for nivel, _ in _regs),
      f"NOME sem valor -> WARNING (got {_regs!r})")
check(any("N8N_WEBHOOK_AUTH_VALUE" in msg for _, msg in _regs),
      f"o aviso nomeia a variavel que FALTA (got {_regs!r})")

_h, _regs = _headers_do_agente("", SEGREDO_FIXTURE)
check(_h == {}, f"VALOR sem nome -> retorno inalterado, dict vazio (got {_h!r})")
check(any(nivel == "WARNING" for nivel, _ in _regs),
      f"VALOR sem nome -> WARNING (got {_regs!r})")
check(any("N8N_WEBHOOK_AUTH_HEADER" in msg for _, msg in _regs),
      f"o aviso nomeia a variavel que FALTA (got {_regs!r})")
check(all(SEGREDO_FIXTURE not in msg for _, msg in _regs),
      f"o aviso NUNCA carrega o VALOR do segredo (got {_regs!r})")

# --- Resultado ---
wh.whatsapp.send_text_message = _orig_send_text
wh._schedule_agent_debounce = _orig_schedule

if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE ENDURECIMENTO DO WEBHOOK PASSARAM")
