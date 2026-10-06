"""
CONV-BIA-FILA-01 — a Bia nao atropela a vendedora (D5), a falha degradada
preserva o dono atual (D2) e o payload ao agente carrega `na_fila_humana` (D11).

Contexto: com a Bia continuando LIGADA na fila humana, a chamada ao agente (ate
240s) pode terminar DEPOIS de a vendedora ter clicado em "Desativar Bot". Antes
desta rodada o envio nao re-checava o flag: a Bia falava por cima da vendedora.
E a falha degradada zerava o `atendente_id` de uma conversa ja atribuida.

Prova que:
  1. a Bia desligada DURANTE a chamada descarta a resposta: nada vai a Meta,
     nada e persistido, nenhum campo operacional muda;
  2. o mesmo vale para a resposta DEGRADADA — nem o fallback sai, e o estado
     nao e tocado (a conversa nao entra na fila por causa de uma resposta que
     ja nao importa);
  3. controle: com a Bia ligada, as partes sao enviadas e persistidas;
  4. degradado com a Bia ligada e a conversa na fila COM atendente: o atendente
     e a posicao na fila sao preservados, e a Bia e desligada (D2);
  5. o payload enviado ao agente carrega `na_fila_humana` (True com `queued_at`,
     False sem);
  6. o mesmo descarte vale pelo caminho real do debounce.

n8n e Meta mockados (`_fetch_agent_parts` substituido por um fake assincrono);
nenhuma rede, nenhuma credencial.
Roda standalone:  python tests/test_conversas_bia_agente_fila.py
"""
import asyncio
import logging
import os
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone as tz

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_bia_agente_fila_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "true"

sys.path.insert(0, str(CONVERSAS_DIR))

import app.main as main  # noqa: E402  (registra TODOS os models no mapper)
import app.routers.webhook as wh  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.services import whatsapp  # noqa: E402

failures = []


def _safe(text: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="backslashreplace").decode(encoding)


def check(cond, msg):
    if cond:
        print(f"  PASS: {_safe(msg)}")
    else:
        print(f"  FAIL: {_safe(msg)}")
        failures.append(msg)


Base.metadata.create_all(bind=engine)

FALLBACK = wh.AGENT_FALLBACK_REPLY
PARTES = ["Parte um", "Parte dois"]
ATENDENTE = 7                                      # users.id: coluna sem FK
ENTROU_NA_FILA = datetime.now(tz.utc) - timedelta(minutes=10)


# ─── Meta mockada: registra o que SERIA enviado ao cliente ──────────────
enviados = []
_wamid = {"n": 0}


async def _fake_send_text(to, message, db=None):
    enviados.append(message)
    _wamid["n"] += 1
    return {"messages": [{"id": f"wamid.FILA{_wamid['n']}"}]}


_orig_send_text = whatsapp.send_text_message
whatsapp.send_text_message = _fake_send_text


# ─── n8n mockado: `_fetch_agent_parts` e a unica fronteira com a rede ────
agente = {
    "chamadas": 0,
    "payloads": [],
    "durante": None,             # funcao sincrona executada COM a chamada em voo
    "resultado": (list(PARTES), False),
}


async def _fake_fetch(agent_url, payload, conversation_id):
    agente["chamadas"] += 1
    agente["payloads"].append(payload)
    if agente["durante"] is not None:
        agente["durante"](conversation_id)
    return agente["resultado"]


_orig_fetch = wh._fetch_agent_parts
wh._fetch_agent_parts = _fake_fetch


# ─── Helpers ──────────────────────────────────────────────────────────────
_seq = {"n": 0}


def _conversa(*, bia=True, atendente_id=None, queued_at=None):
    _seq["n"] += 1
    s = SessionLocal()
    try:
        c = Conversation(
            lead_id=0, whatsapp=f"551193000{_seq['n']:04d}", nome=f"Cliente Fila {_seq['n']}",
            status="aberta", unread_count=3, ultimo_msg="preview antigo",
            is_bot_active=bia, atendente_id=atendente_id, queued_at=queued_at,
        )
        s.add(c)
        s.commit()
        return c.id
    finally:
        s.close()


def _estado(cid):
    s = SessionLocal()
    try:
        c = s.query(Conversation).filter(Conversation.id == cid).first()
        queued = c.queued_at
        if queued is not None and queued.tzinfo is None:
            queued = queued.replace(tzinfo=tz.utc)   # SQLite devolve naive
        return {
            "bia": c.is_bot_active, "atendente": c.atendente_id, "queued_at": queued,
            "primeira_resposta": c.primeira_resposta_humana_at,
            "ultimo_msg": c.ultimo_msg, "unread": c.unread_count, "status": c.status,
        }
    finally:
        s.close()


def _outbound(cid):
    s = SessionLocal()
    try:
        rows = s.query(Message).filter(
            Message.conversation_id == cid, Message.direction == "outbound",
        ).order_by(Message.id).all()
        return [(m.content, m.status) for m in rows]
    finally:
        s.close()


def _vendedora_desliga_bia(cid):
    """O botao 'Desativar Bot': OUTRA sessao, no meio da chamada ao agente."""
    s = SessionLocal()
    try:
        s.query(Conversation).filter(Conversation.id == cid).update(
            {Conversation.is_bot_active: False})
        s.commit()
    finally:
        s.close()


def _roda(cid, *, durante=None, resultado=None):
    """Executa `_forward_to_agent` e devolve (chamadas ao agente, textos enviados)."""
    agente["chamadas"] = 0
    agente["durante"] = durante
    agente["resultado"] = resultado if resultado is not None else (list(PARTES), False)
    antes = len(enviados)
    s = SessionLocal()
    try:
        conv = s.query(Conversation).filter(Conversation.id == cid).first()
        asyncio.run(wh._forward_to_agent(conv, "mensagem do cliente", s))
    finally:
        s.close()
    agente["durante"] = None
    return agente["chamadas"], enviados[antes:]


class _CapturaLog(logging.Handler):
    def __init__(self):
        super().__init__()
        self.registros = []

    def emit(self, record):
        self.registros.append((record.levelname, record.getMessage()))


# =====================================================================
# 1. Bia desligada DURANTE a chamada -> resposta descartada
# =====================================================================
print("1 — a vendedora desliga a Bia durante a chamada: a resposta e descartada")

cid = _conversa(bia=True, atendente_id=ATENDENTE, queued_at=ENTROU_NA_FILA)
captura = _CapturaLog()
wh.logger.addHandler(captura)
_nivel = wh.logger.level
wh.logger.setLevel(logging.INFO)
try:
    chamadas, textos = _roda(cid, durante=_vendedora_desliga_bia)
finally:
    wh.logger.removeHandler(captura)
    wh.logger.setLevel(_nivel)

est = _estado(cid)
check(chamadas == 1, f"o agente FOI chamado (a corrida e real) (got {chamadas})")
check(textos == [], f"NADA foi enviado a Meta (got {textos})")
check(_outbound(cid) == [], f"nenhuma outbound persistida (got {_outbound(cid)})")
check(est["bia"] is False, "a Bia continua desligada — a decisao da vendedora vale")
check(est["atendente"] == ATENDENTE, f"atendente intacto (got {est['atendente']})")
check(est["queued_at"] == ENTROU_NA_FILA, f"queued_at intacto (got {est['queued_at']})")
check(est["primeira_resposta"] is None, "primeira_resposta_humana_at intacta")
check(est["ultimo_msg"] == "preview antigo" and est["unread"] == 3,
      f"preview e unread intactos (got {est['ultimo_msg']!r}, {est['unread']})")
descartes = [m for nivel, m in captura.registros if nivel == "INFO" and "descartada" in m]
check(len(descartes) == 1 and re.search(rf"\b{cid}\b", descartes[0]) is not None,
      f"o descarte e registrado em INFO com o id da conversa (got {captura.registros!r})")


# =====================================================================
# 2. O mesmo com resposta DEGRADADA -> nem o fallback sai
# =====================================================================
print("\n2 — resposta degradada com a Bia desligada no meio: nada enviado, nada alterado")

cid = _conversa(bia=True, atendente_id=None, queued_at=None)
chamadas, textos = _roda(cid, durante=_vendedora_desliga_bia, resultado=([], False))
est = _estado(cid)
check(chamadas == 1, f"o agente FOI chamado (got {chamadas})")
check(textos == [], f"nem o FALLBACK vai ao cliente (got {textos})")
check(_outbound(cid) == [], f"nenhuma outbound persistida (got {_outbound(cid)})")
check(est["queued_at"] is None,
      f"o ramo degradado NAO rodou: a conversa nao entrou na fila (got {est['queued_at']})")
check(est["atendente"] is None, f"atendente intacto (got {est['atendente']})")
check(est["bia"] is False, "a Bia continua desligada")
check(est["ultimo_msg"] == "preview antigo" and est["unread"] == 3,
      f"preview e unread intactos (got {est['ultimo_msg']!r}, {est['unread']})")


# =====================================================================
# 3. Controle: Bia continua ligada -> partes enviadas e persistidas
# =====================================================================
print("\n3 — controle: com a Bia ligada, a resposta segue normal")

cid = _conversa(bia=True, atendente_id=ATENDENTE, queued_at=ENTROU_NA_FILA)
chamadas, textos = _roda(cid)
est = _estado(cid)
check(textos == PARTES, f"as duas partes foram a Meta, na ordem (got {textos})")
check(_outbound(cid) == [(PARTES[0], "sent"), (PARTES[1], "sent")],
      f"e foram persistidas como 'sent' (got {_outbound(cid)})")
check(est["bia"] is True, "Bia continua ligada depois de uma resposta normal")
check(est["atendente"] == ATENDENTE and est["queued_at"] == ENTROU_NA_FILA,
      "resposta normal nao mexe em atendente nem na fila")
check(est["ultimo_msg"] == PARTES[1] and est["unread"] == 0,
      f"preview = ultima parte e unread zerado (got {est['ultimo_msg']!r}, {est['unread']})")


# =====================================================================
# 4. Degradado com a Bia ligada, na fila COM atendente -> dono preservado
# =====================================================================
print("\n4 — falha degradada preserva o atendente e a posicao na fila")

cid = _conversa(bia=True, atendente_id=ATENDENTE, queued_at=ENTROU_NA_FILA)
chamadas, textos = _roda(cid, resultado=([], False))
est = _estado(cid)
check(textos == [FALLBACK], f"o fallback vai ao cliente, uma vez (got {textos})")
check(_outbound(cid) == [(FALLBACK, "sent")], f"e e persistido (got {_outbound(cid)})")
check(est["atendente"] == ATENDENTE,
      f"o atendente atual e PRESERVADO — nao vira None (got {est['atendente']})")
check(est["bia"] is False, "a falha degradada continua DESLIGANDO a Bia (evita o loop do 'repita')")
check(est["queued_at"] == ENTROU_NA_FILA,
      f"a posicao na fila e preservada (got {est['queued_at']})")

# Controle do comportamento antigo: sem dono e sem fila, a falha manda para a fila SEM dono.
cid = _conversa(bia=True, atendente_id=None, queued_at=None)
_roda(cid, resultado=([], False))
est = _estado(cid)
check(est["bia"] is False and est["queued_at"] is not None and est["atendente"] is None,
      f"sem dono: entra na fila sem dono e a Bia desliga (got {est})")


# =====================================================================
# 5. Payload ao agente: na_fila_humana
# =====================================================================
print("\n5 — payload do agente carrega na_fila_humana")

cid = _conversa(bia=True, atendente_id=ATENDENTE, queued_at=ENTROU_NA_FILA)
agente["payloads"].clear()
_roda(cid)
check(len(agente["payloads"]) == 1 and agente["payloads"][0].get("na_fila_humana") is True,
      f"conversa com queued_at -> na_fila_humana True (got {agente['payloads'][-1:]!r})")

cid = _conversa(bia=True, atendente_id=None, queued_at=None)
agente["payloads"].clear()
_roda(cid)
check(len(agente["payloads"]) == 1 and agente["payloads"][0].get("na_fila_humana") is False,
      f"conversa sem queued_at -> na_fila_humana False (got {agente['payloads'][-1:]!r})")
check(all(k in agente["payloads"][0] for k in ("conversation_id", "mensagem", "historico")),
      "os campos que ja existiam no payload continuam la")


# =====================================================================
# 6. O mesmo descarte pelo caminho real do debounce
# =====================================================================
print("\n6 — via _debounce_then_forward: a resposta tardia tambem e descartada")

_deb_orig = wh.AGENT_DEBOUNCE_SECONDS
wh.AGENT_DEBOUNCE_SECONDS = 0
cid = _conversa(bia=True, atendente_id=ATENDENTE, queued_at=ENTROU_NA_FILA)
s = SessionLocal()
s.add(Message(conversation_id=cid, direction="inbound", content="quero saber mais",
              msg_type="text", status="received"))
s.commit()
s.close()

agente["chamadas"] = 0
agente["durante"] = _vendedora_desliga_bia
agente["resultado"] = (list(PARTES), False)
antes = len(enviados)
asyncio.run(wh._debounce_then_forward(cid, None))
agente["durante"] = None
wh.AGENT_DEBOUNCE_SECONDS = _deb_orig

est = _estado(cid)
check(agente["chamadas"] == 1, f"o lote chegou ao agente (got {agente['chamadas']})")
check(enviados[antes:] == [], f"nada foi a Meta (got {enviados[antes:]})")
check(_outbound(cid) == [], f"nada persistido (got {_outbound(cid)})")
check(est["bia"] is False and est["atendente"] == ATENDENTE and est["queued_at"] == ENTROU_NA_FILA,
      f"estado operacional intacto (got {est})")
check(not wh._agent_locks[cid].locked(), "o lock da conversa foi liberado")


# ─── Resultado ──────────────────────────────────────────────────────────
whatsapp.send_text_message = _orig_send_text
wh._fetch_agent_parts = _orig_fetch
main.app.dependency_overrides.clear()

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FALHA(S):")
    for f in failures:
        print(f"  - {_safe(f)}")
    sys.exit(1)
print("TODOS OS TESTES DA BIA NA FILA (AGENTE) PASSARAM")
