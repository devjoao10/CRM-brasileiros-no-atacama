"""
CONV-AGENT-01 — timeout da chamada Conversas -> Bia (n8n) e fallback de degradacao.

Bug de producao: a Bia e um agente LLM com tools; execucoes de 1m27s e 2m36s
sao NORMAIS. O Conversas chamava o n8n com `httpx.AsyncClient(timeout=60.0)`,
desistia da conexao aos 60s e engolia o `TimeoutException` num `logger.warning`.
O n8n concluia com sucesso depois, `Responder ao Conversas` devolvia JSON
valido — e o cliente nao recebia absolutamente nada.

Prova que:
  T. o timeout configurado e `httpx.Timeout(240.0, connect=10.0)` — leitura
     longa, conexao curta — e a semantica vale para o httpx instalado;
  A. resposta que chega DEPOIS de 60s e antes de 240s e aceita e enviada
     (era exatamente o caso 1m27s / 2m36s de producao);
  X. timeout real (>240s) gera UM unico fallback, enviado e persistido;
  C. conexao recusada / erro de rede -> UM unico fallback;
  N. resposta normal -> ZERO fallback;
  H. HTTP != 200 -> fallback;
  J. corpo nao-JSON e JSON sem `resposta` (ou vazia) -> fallback;
  R. NENHUM retry: exatamente 1 POST ao n8n por chamada, em todos os modos;
  M. o Message outbound do fallback bate com o texto REALMENTE enviado a Meta;
  E. estado operacional (is_bot_active/atendente_id/queued_at) intacto;
  F. se o proprio fallback falhar no envio, fica 'failed' + last_error.

n8n e Meta mockados; nenhuma rede, nenhuma credencial real.
Roda standalone:  python tests/test_conversas_agent_timeout.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_agent_timeout_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(CONVERSAS_DIR))

import asyncio  # noqa: E402

import httpx  # noqa: E402

import app.main  # noqa: E402,F401  (registra TODOS os models no mapper)
from app.database import engine, Base, SessionLocal  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.services import whatsapp  # noqa: E402
import app.routers.webhook as wh  # noqa: E402

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


# ─── Mock da Meta: captura o texto REALMENTE enviado ──────────────────
sent_to_meta = []
_wamid = {"n": 0}
meta_mode = {"fail": False}
FAIL_RESPONSE = {"error": True, "status_code": 400, "summary": "HTTP 400: erro simulado"}


async def _fake_send_text(to, message, db=None):
    sent_to_meta.append({"to": to, "message": message})
    if meta_mode["fail"]:
        return FAIL_RESPONSE
    _wamid["n"] += 1
    return {"messages": [{"id": f"wamid.AGENT{_wamid['n']}"}]}


whatsapp.send_text_message = _fake_send_text


# ─── Mock do n8n: cada modo reproduz uma falha real ───────────────────
# `posts` conta as requisicoes: e o guard de "nenhum retry automatico".
posts = {"n": 0}
agent_mode = {"mode": "ok"}
client_kwargs = {}


class _FakeResp:
    def __init__(self, status_code, payload=None, text="", raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            # Mesma excecao que httpx levanta em corpo nao-JSON.
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


post_kwargs = {}


class _FakeAsyncClient:
    def __init__(self, *a, **k):
        client_kwargs.clear()
        client_kwargs.update(k)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, **k):
        posts["n"] += 1
        post_kwargs.clear()
        post_kwargs.update(k)
        mode = agent_mode["mode"]
        if mode == "timeout":
            raise httpx.ReadTimeout("simulado: n8n passou de 240s")
        if mode == "connect_timeout":
            raise httpx.ConnectTimeout("simulado: n8n nao respondeu ao connect")
        if mode == "connect_refused":
            raise httpx.ConnectError("simulado: conexao recusada")
        if mode == "network":
            raise httpx.RemoteProtocolError("simulado: conexao caiu no meio")
        if mode == "http_500":
            return _FakeResp(500, text="Internal Server Error")
        if mode == "bad_json":
            return _FakeResp(200, raise_json=True, text="<html>502</html>")
        if mode == "no_field":
            return _FakeResp(200, payload={"outro": "campo"})
        if mode == "empty":
            return _FakeResp(200, payload={"resposta": "   "})
        if mode == "slow_ok":
            # Resposta VALIDA que chegaria aos ~2m36s: no mundo antigo (60s)
            # isso virava timeout; agora precisa ser aceita normalmente.
            return _FakeResp(200, payload={"resposta": "Resposta que demorou 2m36s"})
        return _FakeResp(200, payload={"resposta": "Parte um|||Parte dois"})


_orig_client = httpx.AsyncClient
httpx.AsyncClient = _FakeAsyncClient


# ─── Helpers ──────────────────────────────────────────────────────────
_conv_seq = {"n": 0}


def make_conv(**kw):
    _conv_seq["n"] += 1
    sess = SessionLocal()
    try:
        defaults = dict(
            lead_id=0, whatsapp=f"551190000{_conv_seq['n']:04d}",
            nome=f"Cliente {_conv_seq['n']}", status="aberta",
            unread_count=3, ultimo_msg="preview antigo",
            is_bot_active=True, atendente_id=None,
        )
        defaults.update(kw)
        c = Conversation(**defaults)
        sess.add(c)
        sess.commit()
        sess.refresh(c)
        return c.id
    finally:
        sess.close()


def get_conv(cid):
    sess = SessionLocal()
    try:
        return sess.query(Conversation).filter(Conversation.id == cid).first()
    finally:
        sess.close()


def outbound(cid):
    sess = SessionLocal()
    try:
        return sess.query(Message).filter(
            Message.conversation_id == cid, Message.direction == "outbound"
        ).order_by(Message.id).all()
    finally:
        sess.close()


def run_agent(cid, mode):
    """Executa _forward_to_agent no modo dado e devolve (posts, enviados)."""
    agent_mode["mode"] = mode
    posts["n"] = 0
    before_sent = len(sent_to_meta)
    sess = SessionLocal()
    try:
        conv = sess.query(Conversation).filter(Conversation.id == cid).first()
        asyncio.run(wh._forward_to_agent(conv, "mensagem do cliente", sess))
    finally:
        sess.close()
    return posts["n"], sent_to_meta[before_sent:]


# ============ T. Timeout configurado ============
print("T — timeout da chamada ao agente")

t = wh.AGENT_TIMEOUT
check(isinstance(t, httpx.Timeout), "AGENT_TIMEOUT e um httpx.Timeout (por fase)")
check(t.read == 240.0, f"read = 240s (got {t.read})")
check(t.write == 240.0, f"write = 240s (got {t.write})")
check(t.pool == 240.0, f"pool = 240s (got {t.pool})")
check(t.connect == 10.0, f"connect = 10s, curto (got {t.connect})")
check(t.connect < t.read, "conexao curta, leitura longa (a semantica pedida)")

src = (CONVERSAS_DIR / "app" / "routers" / "webhook.py").read_text(encoding="utf-8")
check("timeout=60.0" not in src, "o teto antigo de 60.0s nao existe mais no webhook")
check("httpx.Timeout(240.0, connect=10.0)" in src,
      "timeout declarado exatamente como httpx.Timeout(240.0, connect=10.0)")

# O httpx instalado precisa ter a semantica que o codigo assume.
probe = httpx.Timeout(240.0, connect=10.0)
check((probe.connect, probe.read, probe.write, probe.pool) == (10.0, 240.0, 240.0, 240.0),
      f"httpx {httpx.__version__}: 1o posicional = default de read/write/pool; connect sobrescreve")

# O timeout precisa CHEGAR ao cliente, nao so existir como constante.
run_agent(make_conv(), "ok")
check(client_kwargs.get("timeout") is wh.AGENT_TIMEOUT,
      f"AsyncClient recebe o AGENT_TIMEOUT (got {client_kwargs.get('timeout')!r})")


# ============ D2 — cabecalho de auth do webhook da Bia ============
# AUDIT-2026-08-WF2 — `/webhook/agent-bia` esta aberto na internet. O webhook
# irmao (`/webhook/gerenciador-leads`) ja ganhou Header Auth na D1, mas este NAO
# podia ganhar: o Conversas nao mandava cabecalho nenhum, entao ligar a
# autenticacao no n8n derrubaria a Bia no mesmo instante. O lado-repo agora
# manda o cabecalho quando configurado.
#
# A ordem importa e esta travada aqui: SEM configuracao o comportamento e
# IDENTICO ao de hoje (nenhum cabecalho), o que torna seguro subir o Conversas
# antes de mexer no n8n. O contrario — ligar no n8n primeiro — corta a Bia.
print()
print("D2 — cabecalho de autenticacao no POST para o agente")

run_agent(make_conv(), "ok")
check(post_kwargs.get("headers") == {},
      f"SEM configuracao: nenhum cabecalho de auth (got {post_kwargs.get('headers')!r}) — "
      f"e o comportamento de hoje, byte a byte")

_orig_nome = wh.N8N_WEBHOOK_AUTH_HEADER
_orig_valor = wh.N8N_WEBHOOK_AUTH_VALUE
try:
    wh.N8N_WEBHOOK_AUTH_HEADER = "X-BnA-Webhook-Token"
    wh.N8N_WEBHOOK_AUTH_VALUE = "segredo-de-teste"
    run_agent(make_conv(), "ok")
    check(post_kwargs.get("headers") == {"X-BnA-Webhook-Token": "segredo-de-teste"},
          f"COM configuracao: o cabecalho chega ao POST (got {post_kwargs.get('headers')!r})")

    # Meio-configurado nao vale: mandar um cabecalho com valor vazio seria pior
    # que nao mandar — o n8n recusaria e a Bia cairia, com a configuracao
    # parecendo feita.
    wh.N8N_WEBHOOK_AUTH_VALUE = ""
    run_agent(make_conv(), "ok")
    check(post_kwargs.get("headers") == {},
          f"nome sem valor -> NENHUM cabecalho (got {post_kwargs.get('headers')!r})")

    wh.N8N_WEBHOOK_AUTH_HEADER = ""
    wh.N8N_WEBHOOK_AUTH_VALUE = "segredo-de-teste"
    run_agent(make_conv(), "ok")
    check(post_kwargs.get("headers") == {},
          f"valor sem nome -> NENHUM cabecalho (got {post_kwargs.get('headers')!r})")
finally:
    wh.N8N_WEBHOOK_AUTH_HEADER = _orig_nome
    wh.N8N_WEBHOOK_AUTH_VALUE = _orig_valor


# ============ A. Resposta lenta (>60s, <240s) e ACEITA ============
print("\nA — resposta que antes estourava os 60s")

cid = make_conv()
n_posts, enviados = run_agent(cid, "slow_ok")
check(n_posts == 1, f"exatamente 1 POST ao n8n (got {n_posts})")
check([e["message"] for e in enviados] == ["Resposta que demorou 2m36s"],
      f"resposta lenta enviada ao cliente (got {[e['message'] for e in enviados]})")
check(FALLBACK not in [e["message"] for e in enviados], "NENHUM fallback numa resposta lenta valida")
rows = outbound(cid)
check(len(rows) == 1 and rows[0].content == "Resposta que demorou 2m36s" and rows[0].status == "sent",
      f"persistida como outbound 'sent' (got {[(m.content, m.status) for m in rows]})")


# ============ N. Resposta normal -> ZERO fallback ============
print("\nN — resposta normal")

cid = make_conv()
n_posts, enviados = run_agent(cid, "ok")
check(n_posts == 1, f"1 POST (got {n_posts})")
check([e["message"] for e in enviados] == ["Parte um", "Parte dois"],
      f"duas partes enviadas na ordem (got {[e['message'] for e in enviados]})")
check(all(FALLBACK != e["message"] for e in enviados), "ZERO mensagens de fallback")
rows = outbound(cid)
check(len(rows) == 2 and all(m.status == "sent" for m in rows), "2 outbound 'sent'")
check(get_conv(cid).ultimo_msg == "Parte dois", "preview = ultima parte enviada")
check(get_conv(cid).unread_count == 0, "unread zerado no sucesso")


# ============ X / C. Falhas reais -> UM unico fallback ============
print("\nX/C — timeout, conexao e rede")

FAIL_MODES = [
    ("timeout", "timeout de LEITURA (>240s)"),
    ("connect_timeout", "timeout de CONEXAO"),
    ("connect_refused", "conexao recusada"),
    ("network", "erro de rede no meio da resposta"),
    ("http_500", "HTTP 500 do n8n"),
    ("bad_json", "corpo que nao e JSON"),
    ("no_field", "JSON 200 sem o campo `resposta`"),
    ("empty", "JSON 200 com `resposta` vazia"),
]

for mode, rotulo in FAIL_MODES:
    cid = make_conv()
    n_posts, enviados = run_agent(cid, mode)
    msgs = [e["message"] for e in enviados]
    rows = outbound(cid)
    check(n_posts == 1, f"{rotulo}: exatamente 1 POST — nenhum retry (got {n_posts})")
    check(msgs == [FALLBACK], f"{rotulo}: UMA mensagem, o fallback (got {msgs})")
    check(len(rows) == 1 and rows[0].content == FALLBACK and rows[0].status == "sent",
          f"{rotulo}: 1 outbound 'sent' com o texto do fallback (got {[(m.content[:20], m.status) for m in rows]})")


# ============ M. Message.content == o que foi enviado ============
print("\nM — historico bate com o que a Meta recebeu")

cid = make_conv()
_, enviados = run_agent(cid, "timeout")
rows = outbound(cid)
check(rows[0].content == enviados[0]["message"],
      "Message.content e identico ao texto entregue a Meta")
check(rows[0].msg_type == "text" and rows[0].direction == "outbound",
      "fallback persistido como outbound de texto, igual a qualquer resposta")
check(enviados[0]["to"] == get_conv(cid).whatsapp, "enviado para o numero da propria conversa")


# ============ Fallback nunca expoe erro tecnico ============
print("\nCONTEUDO — fallback generico")

for termo in ("timeout", "n8n", "erro", "http", "exception", "traceback", "240"):
    check(termo not in FALLBACK.lower(), f"fallback nao menciona {termo!r}")
check(FALLBACK == ("Tive uma instabilidade para processar sua mensagem agora. "
                   "Pode me enviar novamente em alguns instantes? \U0001F642"),
      "texto do fallback e exatamente o acordado")


# ============ E. Estado operacional intacto ============
print("\nE — estado operacional da conversa")

cid = make_conv(is_bot_active=True, atendente_id=None, unread_count=7)
before = get_conv(cid)
b_bot, b_atend, b_queue, b_status = (
    before.is_bot_active, before.atendente_id, before.queued_at, before.status,
)
run_agent(cid, "timeout")
after = get_conv(cid)
# AUDIT-2026-08-WA — REGRA INVERTIDA, de proposito.
#
# Este bloco afirmava que uma falha da Bia NAO mexe no estado operacional. A
# intencao era boa (um blip de rede nao deve reorganizar a fila), mas o efeito
# real era o oposto do pretendido: a conversa ficava em ATENDIMENTOS BIA com
# `is_bot_active=True` e NENHUM humano a via. O cliente escrevia, recebia o
# fallback pedindo para reenviar, escrevia de novo, o agente falhava de novo —
# o "loop do repita sua mensagem" que a operacao relatou, com o cliente
# invisivel para a equipe o tempo todo.
#
# Agora uma falha DEGRADADA joga a conversa na FILA DE ESPERA humana: quem
# escreveu e nao foi atendido pela automacao precisa alcancar uma pessoa. O
# custo (a Bia para de responder essa conversa) e reversivel pelo proprio
# atendente, que pode religar o bot no painel.
check(after.is_bot_active is False,
      "AUDIT-2026-08-WA: falha da Bia DESLIGA o bot (conversa vai para humano)")
check(after.atendente_id == b_atend,
      "atendente_id NAO muda — isto e excecao, nao handoff de triagem concluida")
check(after.queued_at is not None,
      "AUDIT-2026-08-WA: conversa entra na FILA DE ESPERA (nao fica invisivel)")
check(after.primeira_resposta_humana_at is None,
      "continua aguardando humano — o fallback da Bia nao e atendimento")
check(after.status == b_status, "status da conversa NAO muda")
check(after.ultimo_msg == FALLBACK, "preview reflete o fallback, que o cliente recebeu")
check(after.unread_count == 0, "unread zerado — houve outbound entregue")

# Idempotencia: uma segunda falha nao empurra a conversa para o fim da fila.
fila_1 = after.queued_at
run_agent(cid, "timeout")
after2 = get_conv(cid)
check(after2.queued_at == fila_1,
      "AUDIT-2026-08-WA: falha repetida PRESERVA a posicao na fila (FIFO intacto)")


# ============ F. Fallback que falha no envio ============
print("\nF — o proprio fallback falhando na Meta")

meta_mode["fail"] = True
cid = make_conv(unread_count=4, ultimo_msg="preview antigo")
n_posts, enviados = run_agent(cid, "timeout")
rows = outbound(cid)
check(n_posts == 1, "ainda 1 POST — falha da Meta nao provoca retry no n8n")
check(len(rows) == 1 and rows[0].content == FALLBACK, "fallback persistido mesmo falhando")
check(rows[0].status == "failed", f"status 'failed', nao 'sent' (got {rows[0].status})")
check(bool(rows[0].last_error), f"last_error preenchido (got {rows[0].last_error!r})")
after = get_conv(cid)
check(after.ultimo_msg == "preview antigo", "preview NAO sobrescrito quando o fallback falha")
check(after.unread_count == 4, "unread NAO zerado quando o fallback falha")
meta_mode["fail"] = False


# ============ R. Nenhum retry em lugar nenhum ============
print("\nR — ausencia de retry automatico")

check("tenacity" not in src and "backoff" not in src, "nenhuma biblioteca de retry no webhook")
check("transport=" not in src and "HTTPTransport" not in src,
      "sem HTTPTransport(retries=...) escondido no cliente")
check(src.count("client.post(agent_url") == 1, "existe UM unico POST ao agente no codigo")
for pattern in ("for _ in range", "while True"):
    check(pattern not in src.split("_fetch_agent_parts")[1].split("async def _forward_to_agent")[0],
          f"nenhum laco {pattern!r} em torno da chamada ao agente")


# ─── Resultado ────────────────────────────────────────────────────────
httpx.AsyncClient = _orig_client

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FALHA(S):")
    for f in failures:
        print(f"  - {_safe(f)}")
    sys.exit(1)
print("TODOS OS CHECKS PASSARAM")
