# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-WE (M8) — GET /api/conversations/inativas.

docs/audit/N8N_MANUAL_CHANGES.md § M8: o cliente para de responder e fica no
limbo. O n8n (Schedule Trigger a cada 30 min, fora deste repositorio) precisa
perguntar a este endpoint quais conversas estao silenciosas ha `horas` para
decidir a quem mandar o follow-up ("quer que eu siga com o seu roteiro, ou
prefere falar com alguem do nosso time agora?").

Elegivel = TODAS as condicoes (ver docstring de `list_inactive_conversations`
em conversas/app/routers/conversations.py):
  1. status aberto;
  2. `last_customer_msg_at` dentro da janela de 24h da Meta (SERVICE_WINDOW) E
     com pelo menos `horas` de silencio;
  3. nenhuma mensagem, em qualquer direcao, nas ultimas `horas`;
  4. no maximo UMA mensagem outbound desde a ultima entrada do cliente — o
     guard anti-perseguicao: a PROVA central deste arquivo (bloco "idempotencia"
     abaixo) e que, apos simular o proprio follow-up, a MESMA conversa some da
     proxima chamada.

Fixture set (todas relativas a NOW = agora, capturado uma vez):
  A(1)=silenciosa (2h, 1 outbound antigo)      -> APARECE
  B(2)=recente (30min)                          -> NAO aparece (regra 2/3)
  C(3)=janela fechada (> SERVICE_WINDOW)         -> NAO aparece (regra 2)
  D(4)=outbound recente (dentro de `horas`)      -> NAO aparece (regra 3)
  E(5)=dois outbound desde o ultimo inbound      -> NAO aparece (regra 4)
  F(6)=encerrada                                 -> NUNCA aparece (regra 1)
  G(7)=ainda com a Bia (is_bot_active=True)      -> APARECE (decisao, ver doc)
  H(8)=mais silenciosa que A (5h)                -> APARECE, primeira na ordem

Roda standalone:  python tests/test_conversas_followup_inatividade.py
"""
import datetime as dt
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_followup_inatividade_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(CONVERSAS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.database import SessionLocal, Base, engine  # noqa: E402
from app.auth import get_current_user, User  # noqa: E402
from app.models.conversation import Conversation, Message, SERVICE_WINDOW  # noqa: E402

failures = []


def check(cond, msg):
    print(("  PASS: " if cond else "  FAIL: ") + msg)
    if not cond:
        failures.append(msg)


Base.metadata.create_all(bind=engine)

NOW = dt.datetime.now(dt.timezone.utc)
H = dt.timedelta(hours=1)
M = dt.timedelta(minutes=1)

_db = SessionLocal()


def seed(id_, nome, *, last_customer_msg_at, outbound_ats=(), status="aberta",
         is_bot_active=False):
    """
    Conversa + a mensagem inbound que fixou `last_customer_msg_at` + uma
    outbound por instante em `outbound_ats` (todas POSTERIORES ao inbound —
    e a partir dele que a rota conta "outbound desde o ultimo inbound").
    """
    c = Conversation(
        id=id_, lead_id=id_, whatsapp=f"55119999{id_:04d}", nome=nome,
        status=status, is_bot_active=is_bot_active,
        last_customer_msg_at=last_customer_msg_at,
    )
    _db.add(c)
    _db.add(Message(conversation_id=id_, direction="inbound", content="oi",
                     msg_type="text", status="sent", created_at=last_customer_msg_at))
    for i, at in enumerate(outbound_ats):
        _db.add(Message(conversation_id=id_, direction="outbound", content=f"resp {i}",
                         msg_type="text", status="sent", created_at=at))


seed(1, "A silenciosa", last_customer_msg_at=NOW - 2 * H, outbound_ats=[NOW - 2 * H + 5 * M])
seed(2, "B recente", last_customer_msg_at=NOW - 30 * M)
seed(3, "C janela fechada", last_customer_msg_at=NOW - SERVICE_WINDOW - H)
seed(4, "D outbound recente", last_customer_msg_at=NOW - 3 * H, outbound_ats=[NOW - 10 * M])
seed(5, "E dois outbound", last_customer_msg_at=NOW - 3 * H,
     outbound_ats=[NOW - 2 * H - 50 * M, NOW - 2 * H - 45 * M])
seed(6, "F encerrada", last_customer_msg_at=NOW - 3 * H, status="encerrada")
seed(7, "G com bia", last_customer_msg_at=NOW - 2 * H, outbound_ats=[NOW - 2 * H + 5 * M],
     is_bot_active=True)
seed(8, "H mais silenciosa", last_customer_msg_at=NOW - 5 * H, outbound_ats=[NOW - 4 * H - 55 * M])
_db.commit()
_db.close()


class _U1:
    id = 1
    nome = "Julia"
    email = "julia@local"
    role = "user"
    is_active = True


def as_user(u):
    main.app.dependency_overrides[get_current_user] = lambda: u


as_user(_U1())
client = TestClient(main.app)


def call(horas=1, limite=50):
    return client.get(f"/api/conversations/inativas?horas={horas}&limite={limite}")


def ids_inativas(horas=1, limite=50):
    r = call(horas=horas, limite=limite)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    return [c["id"] for c in r.json()["conversations"]]


# ============ 1. CLASSIFICACAO + ORDENACAO ============
print("1 — classificacao (horas=1) e ordenacao (mais silenciosa primeiro, id desempata)")
r1 = ids_inativas(horas=1, limite=50)
check(r1 == [8, 1, 7],
      f"elegiveis, na ordem: H(8,5h), A(1,2h), G(7,2h — empate com A, id desempata) — veio {r1}")
check(2 not in r1, "B (silencio 30min < horas=1) nao aparece")
check(3 not in r1, "C (last_customer_msg_at ha mais de 24h — janela fechada) nao aparece")
check(4 not in r1, "D (outbound ha 10min, dentro de horas=1) nao aparece")
check(5 not in r1, "E (2 outbound desde o ultimo inbound) nao aparece")
check(6 not in r1, "F (encerrada) nunca aparece")
check(7 in r1, "G (ainda com a Bia) APARECE — is_bot_active nao filtra (decisao documentada na rota)")

print("1b — payload traz pelo menos id/nome/whatsapp (o que o n8n precisa)")
primeira = call(horas=1, limite=50).json()["conversations"][0]
check({"id", "nome", "whatsapp"} <= set(primeira.keys()),
      f"campos minimos presentes — veio {sorted(primeira.keys())}")

# ============ 2. LIMITE E TOTAL ============
print("2 — limite corta a pagina, total reflete o total elegivel")
resp_lim = call(horas=1, limite=2).json()
ids_lim = [c["id"] for c in resp_lim["conversations"]]
check(ids_lim == [8, 1], f"limite=2 mantem a ordem e corta em 2 — veio {ids_lim}")
check(resp_lim["total"] == 3, f"total = 3 elegiveis (nao a pagina de 2) — veio {resp_lim['total']}")

# ============ 3. VALIDACAO (ge=/le=) ============
print("3 — validacao dos parametros")
check(client.get("/api/conversations/inativas?horas=0").status_code == 422,
      "horas=0 -> 422 (nao pode zerar a regra 3 e reabrir o loop de perseguicao)")
check(client.get("/api/conversations/inativas?horas=0.001").status_code == 422,
      "horas abaixo do piso (0.01) -> 422")
check(client.get("/api/conversations/inativas?horas=0.01").status_code == 200,
      "piso minimo (0.01h = 36s) e aceito — permite validacao manual sem esperar 8h")
check(client.get("/api/conversations/inativas?horas=25").status_code == 422,
      "horas=25 (> janela de 24h da Meta, derivada de SERVICE_WINDOW) -> 422")
check(client.get("/api/conversations/inativas?limite=0").status_code == 422,
      "limite=0 -> 422")
check(client.get("/api/conversations/inativas?limite=201").status_code == 422,
      "limite=201 (> teto de 200) -> 422")

# ============ 4. AUTENTICACAO ============
print("4 — autenticacao")
main.app.dependency_overrides.pop(get_current_user, None)
anon = TestClient(main.app)
check(anon.get("/api/conversations/inativas").status_code == 401, "sem auth -> 401")
check("test-secret-key" not in anon.get("/api/conversations/inativas").text,
      "sem segredo na resposta")
as_user(_U1())

# ============ 5. CUSTO — bounded queries, nao 1-por-linha ============
print("5 — custo em queries nao cresce por linha")
from sqlalchemy import event as _event  # noqa: E402

qs = []
_ev = lambda conn, cur, st, params, ctx, many: qs.append(st)  # noqa: E731,E731
_event.listen(engine, "before_cursor_execute", _ev)
call(horas=1, limite=50)
_event.remove(engine, "before_cursor_execute", _ev)
sel = [q for q in qs if q.strip().lower().startswith("select")]
check(len(sel) <= 3,
      f"no maximo 3 SELECTs por chamada (candidatas + tags em lote + agregado "
      f"outbound), nunca 1 por conversa — vieram {len(sel)}")

# ============ 6. IDEMPOTENCIA — a prova central deste arquivo ============
print("6 — idempotencia: apos simular o follow-up, a MESMA conversa NAO volta")
antes = ids_inativas(horas=1, limite=50)
check(1 in antes, "pre-condicao: A(1) esta elegivel antes do follow-up simulado")

d = SessionLocal()
d.add(Message(
    conversation_id=1, direction="outbound",
    content="oi! vi que nossa conversa ficou parada — quer que eu continue ou prefere um humano?",
    msg_type="text", status="sent", created_at=dt.datetime.now(dt.timezone.utc),
))
d.commit()
d.close()

depois = ids_inativas(horas=1, limite=50)
check(1 not in depois,
      "A(1) NAO volta apos o follow-up — vira a 2a outbound desde o ultimo "
      "inbound e reprova a regra 4 (o guard anti-perseguicao)")
check(depois == [8, 7], f"os demais elegiveis (H, G) nao mudam — veio {depois}")

print()
if failures:
    print(f"FALHOU ({len(failures)}):")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("TODOS OS CHECKS PASSARAM")
