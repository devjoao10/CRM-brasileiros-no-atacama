# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-F2 — SILENCIO da Bia nao e FALHA da Bia.

O workflow "WF-01 Agente Bia" em producao ganhou um portao: mensagem composta
so de emoji cai no node `Ignorar mensagem`, que responde SEM CORPO para dizer
"recebi, nao ha o que responder". Deste lado, `_fetch_agent_parts` tratava todo
status != 200 como degradacao e devolvia `[]`, e `[]` faz o chamador enviar:

    "Tive uma instabilidade para processar sua mensagem agora."

Ou seja: quem mandava um polegar pra cima sozinho recebia um pedido de
desculpas por instabilidade — o oposto exato do que o portao foi construido
para produzir — e cada reacao de cliente gravava uma linha de ERRO no log de um
evento perfeitamente normal.

A causa raiz estava escrita na propria docstring da funcao: "quem chama nao
precisa distinguir os modos de falha". Precisa: os dois casos pedem acoes
opostas.

ATENCAO — ESTA E METADE DA CORRECAO. A outra metade e uma alteracao MANUAL no
n8n (`Ignorar mensagem`: 404 -> 204), descrita em docs/audit/N8N_MANUAL_CHANGES.md
e ainda NAO aplicada em producao. O ultimo bloco deste arquivo trava exatamente
isso: enquanto o n8n mandar 404, o cliente CONTINUA recebendo o fallback, e o
teste afirma esse fato em vez de escondê-lo.

n8n e Meta mockados; nenhuma rede, nenhuma credencial. Mesmo padrao de
tests/test_conversas_agent_timeout.py.

Rodar:  python tests/test_conversas_agent_silence.py
"""
import asyncio
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_agent_silence_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(CONVERSAS_DIR))

import httpx  # noqa: E402

import app.main as main  # noqa: E402,F401
import app.routers.webhook as wh  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.services import whatsapp  # noqa: E402

Base.metadata.create_all(bind=engine)

# AUDIT-2026-08-F2: o texto de fallback tem emoji, e o codec padrao do Windows
# (cp1252) nao consegue imprimi-lo — o teste morria com UnicodeEncodeError em vez
# de reprovar. Mesma classe de defeito que a fase anterior corrigiu nas 16
# chamadas de subprocess.run sem `encoding`: verde no CI Linux, quebrado na
# maquina de quem escreve o codigo.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover - stdout exotico
    pass

falhas = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        falhas.append(msg)


# ─── Meta mockada: registra o que seria enviado ao cliente ───────────────
enviados = []


async def _fake_send_text(numero, texto, db=None):
    enviados.append(texto)
    return {"messages": [{"id": f"wamid.SIL{len(enviados)}"}]}


whatsapp.send_text_message = _fake_send_text
wh.whatsapp.send_text_message = _fake_send_text


# ─── n8n mockado ────────────────────────────────────────────────────────
posts = {"n": 0}
modo = {"m": "ok"}


class _FakeResp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("sem corpo")
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, **k):
        posts["n"] += 1
        m = modo["m"]
        if m == "silencio_204":
            return _FakeResp(204)
        if m == "silencio_ignorar":
            return _FakeResp(200, {"ignorar": True})
        if m == "404_atual":
            return _FakeResp(404, None, "")
        if m == "erro_500":
            return _FakeResp(500, None, "boom")
        if m == "timeout":
            raise httpx.ReadTimeout("simulado")
        return _FakeResp(200, {"resposta": "oi|||como posso ajudar?"})


httpx.AsyncClient = _FakeAsyncClient


# ─── Helpers ────────────────────────────────────────────────────────────
_seq = {"n": 0}


def nova_conversa():
    _seq["n"] += 1
    s = SessionLocal()
    try:
        c = Conversation(lead_id=0, whatsapp=f"55119222000{_seq['n']:02d}",
                         nome="Cliente Silencio", status="aberta",
                         unread_count=0, is_bot_active=True)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id
    finally:
        s.close()


def roda(cid, m):
    """Executa _forward_to_agent e devolve (posts, textos enviados, outbounds)."""
    modo["m"] = m
    posts["n"] = 0
    antes = len(enviados)
    s = SessionLocal()
    try:
        conv = s.query(Conversation).filter(Conversation.id == cid).first()
        asyncio.run(wh._forward_to_agent(conv, "mensagem do cliente", s))
    finally:
        s.close()
    s = SessionLocal()
    try:
        outs = s.query(Message).filter(
            Message.conversation_id == cid,
            Message.direction == "outbound").all()
    finally:
        s.close()
    return posts["n"], enviados[antes:], outs


# ═══ 1. SILENCIO: 204 ═══════════════════════════════════════════════════
print("1) 204 = a Bia decidiu nao responder")

cid = nova_conversa()
n_posts, textos, outs = roda(cid, "silencio_204")
check(n_posts == 1, f"exatamente um POST ao agente (got {n_posts})")
check(textos == [], f"NADA foi enviado ao cliente (got {textos})")
check(len(outs) == 0, f"nenhuma mensagem outbound persistida (got {len(outs)})")
check(wh.AGENT_FALLBACK_REPLY not in textos,
      "o cliente NAO recebeu 'tive uma instabilidade'")


# ═══ 2. SILENCIO: 200 {"ignorar": true} ═════════════════════════════════
print()
print("2) 200 com ignorar=true tambem e silencio")

cid = nova_conversa()
n_posts, textos, outs = roda(cid, "silencio_ignorar")
check(textos == [], f"NADA enviado ao cliente (got {textos})")
check(len(outs) == 0, "nenhuma outbound persistida")


# ═══ 3. FALHA continua sendo falha ══════════════════════════════════════
print()
print("3) erro de verdade continua gerando UM fallback")

cid = nova_conversa()
n_posts, textos, outs = roda(cid, "erro_500")
check(textos == [wh.AGENT_FALLBACK_REPLY],
      f"exatamente um fallback no 500 (got {len(textos)} mensagem(ns))")
check(len(outs) == 1, f"o fallback foi persistido (got {len(outs)})")

cid = nova_conversa()
n_posts, textos, outs = roda(cid, "timeout")
check(textos == [wh.AGENT_FALLBACK_REPLY], "timeout tambem gera fallback")
check(n_posts == 1, "sem retry: um unico POST mesmo no timeout")


# ═══ 4. Caminho normal intacto ══════════════════════════════════════════
print()
print("4) resposta normal continua sendo entregue, dividida por |||")

cid = nova_conversa()
n_posts, textos, outs = roda(cid, "ok")
check(textos == ["oi", "como posso ajudar?"],
      f"as duas partes chegaram ao cliente (got {textos})")
check(wh.AGENT_FALLBACK_REPLY not in textos, "nenhum fallback no caminho feliz")


# ═══ 5. A METADE QUE FALTA — n8n ainda manda 404 ════════════════════════
print()
print("5) o que AINDA acontece em producao ate a mudanca manual no n8n")

cid = nova_conversa()
n_posts, textos, outs = roda(cid, "404_atual")
check(textos == [wh.AGENT_FALLBACK_REPLY],
      "com 404 o cliente AINDA recebe o fallback — a mudanca no n8n e obrigatoria")

# O export de producao versionado ainda diz 404: enquanto disser, o item
# continua aberto. Se alguem versionar um export com 204, este check acusa que
# a metade pendente foi feita e o proximo passo e reavaliar o finding.
export = ROOT / "n8n" / "workflows" / "live_exports" / "20260825_fase2" / "wf01_agente_bia.json"
check(export.exists(), "export de producao versionado como evidencia")
wf = json.load(open(export, encoding="utf-8"))
ignorar = [n for n in wf["nodes"] if n["name"] == "Ignorar mensagem"]
check(len(ignorar) == 1, "node 'Ignorar mensagem' existe no export")
codigo = ignorar[0]["parameters"]["options"].get("responseCode")
check(codigo == 404,
      f"o export AINDA responde {codigo} — a alteracao manual do n8n segue PENDENTE. "
      "Se este check falhar porque virou 204, atualize N8N_MANUAL_CHANGES.md e "
      "RELEASE_READINESS.md: o item deixou de ser BLOCKED_OPERATOR.")

proposto = ROOT / "docs" / "audit" / "proposed_n8n" / "wf01_agente_bia.PROPOSTO.json"
check(proposto.exists(), "existe JSON proposto para o operador aplicar")
if proposto.exists():
    wp = json.load(open(proposto, encoding="utf-8"))
    ip = [n for n in wp["nodes"] if n["name"] == "Ignorar mensagem"][0]
    check(ip["parameters"]["options"].get("responseCode") == 204,
          "o JSON proposto ja responde 204")


print()
if falhas:
    print(f"{len(falhas)} FALHA(S)")
    sys.exit(1)
print("OK: silencio e falha sao coisas diferentes deste lado da ponte")
