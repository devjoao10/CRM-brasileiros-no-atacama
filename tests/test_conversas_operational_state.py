"""
PACOTE-A — estado operacional confiavel do Conversas.

Fonte unica: is_bot_active + atendente_id + queued_at.

  BIA          -> bot=True,  atendente=NULL, queued=NULL
  Fila         -> bot=False, atendente=NULL, queued=<timestamp>
  Atendendo    -> bot=False, atendente=<id>, queued=NULL

Prova que:
  1. Inbound novo nasce em BIA (bot ligado, sem atendente, sem fila).
  2. Handoff desliga a BIA e carimba queued_at (atendente continua NULL).
  3. Handoff e IDEMPOTENTE: retry do n8n nao muda queued_at (nao vai pro fim
     da fila).
  4. RACE — handoff depois de claim: nao recoloca na fila (queued=NULL) e
     mantem o humano atendendo.
  5. Claim: atendente=usuario autenticado, bot=False, queued=NULL.
  6. Claim concorrente continua 409 (trava anti-duplo-atendimento intacta).
  7. Assign: atendente=destino, bot=False, queued=NULL.
  8. Release: atendente=NULL, bot=False, queued=NOVO timestamp (fim da fila).
  9. Initiate: quem inicia assume (atendente=current_user), sem fila.
 10. Reabertura (encerrada + inbound): volta para a BIA, SEM herdar atendente.
 11. Inbound em conversa JA aberta nao toca o estado operacional (FIFO firme).
 12. FIFO: mensagem nova do cliente NAO altera queued_at nem a ordem da fila.
 13. BIA/debounce: agenda antes do handoff, NAO agenda depois, volta a agendar
     apos reabertura.
 14. Nenhuma operacao de fila escreve na tabela `leads`.
 15. Handoff exige autenticacao (sem credencial -> 401).
 16. Migration m008: coluna, indices, correcao de bot legado, dados preservados.

Roda standalone:  python tests/test_conversas_operational_state.py
"""
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_operational_state_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "true"  # PACOTE-A: precisamos observar o debounce

sys.path.insert(0, str(CONVERSAS_DIR))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import app.main as main  # noqa: E402
import app.routers.webhook as wh  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.auth import get_current_user, User  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


Base.metadata.create_all(bind=engine)

# Tabela CRM-shaped `leads` no MESMO sqlite (padrao do
# test_conversas_hotfix_filters_resp): permite provar a fronteira comercial.
with engine.begin() as conn:
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS leads ("
        "id INTEGER PRIMARY KEY, nome VARCHAR(200), whatsapp VARCHAR(30), "
        "email VARCHAR(255), responsavel_id INTEGER)"
    ))
    conn.execute(text(
        "INSERT INTO leads (id, nome, whatsapp, email, responsavel_id) "
        "VALUES (77, 'Lead Fila', '5511900066601', 'l@x.com', 5)"
    ))

# Usuarios reais na tabela compartilhada (assign valida is_active).
_db = SessionLocal()
for uid, nome, email in ((1, "Julia", "julia@local"), (2, "Joao", "joao@local")):
    if not _db.query(User).filter(User.id == uid).first():
        _db.add(User(id=uid, nome=nome, email=email,
                     hashed_password="x", role="user", is_active=True))
_db.commit()
_db.close()


class _U1:
    id = 1
    nome = "Julia"
    email = "julia@local"
    role = "user"
    is_active = True


class _U2:
    id = 2
    nome = "Joao"
    email = "joao@local"
    role = "user"
    is_active = True


def as_user(u):
    main.app.dependency_overrides[get_current_user] = lambda: u


as_user(_U1())
client = TestClient(main.app)


async def _noop(*a, **k):
    return None


async def _noop_false(*a, **k):
    return False


wh.whatsapp.mark_as_read = _noop
wh.whatsapp.send_text_message = _noop
wh.crm_service.auto_link_conversation = _noop_false

# Observa o agendamento da BIA sem disparar rede.
scheduled = []
wh._schedule_agent_debounce = lambda cid: scheduled.append(cid)


def inbound(msg_id, sender, body="oi"):
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"profile": {"name": f"Cliente {sender[-4:]}"}}],
        "messages": [{"from": sender, "id": msg_id, "type": "text",
                      "timestamp": "1700000000", "text": {"body": body}}],
    }}]}]}


def state(conv_id):
    """Le o estado operacional direto do banco (nao confia na serializacao)."""
    db = SessionLocal()
    try:
        c = db.query(Conversation).filter(Conversation.id == conv_id).first()
        return (c.is_bot_active, c.atendente_id, c.queued_at, c.status)
    finally:
        db.close()


def conv_id_of(whatsapp):
    db = SessionLocal()
    try:
        c = db.query(Conversation).filter(Conversation.whatsapp == whatsapp).first()
        return c.id if c else None
    finally:
        db.close()


def leads_snapshot():
    db = SessionLocal()
    try:
        return sorted(
            (r.id, r.responsavel_id)
            for r in db.execute(text("SELECT id, responsavel_id FROM leads")).all()
        )
    finally:
        db.close()


LEADS_BEFORE = leads_snapshot()

# ============ 1. BIA — inbound novo ============
print("1 — inbound novo nasce em Atendimentos BIA")
client.post("/webhook", json=inbound("wamid.S1", "5511900066601"))
c1 = conv_id_of("5511900066601")
bot, at, q, st = state(c1)
check(bot is True, "inbound novo: is_bot_active=True")
check(at is None, "inbound novo: atendente_id=NULL")
check(q is None, "inbound novo: queued_at=NULL")
check(st == "aberta", "inbound novo: status=aberta")

# ============ 13a. BIA agenda antes do handoff ============
print("13a — BIA agendada ANTES do handoff")
scheduled.clear()
client.post("/webhook", json=inbound("wamid.S2", "5511900066601"))
check(scheduled == [c1], "pre-handoff: debounce da BIA agendado")

# ============ 2. HANDOFF ============
print("2 — handoff desliga BIA e carimba queued_at")
r = client.post(f"/api/conversations/{c1}/handoff")
check(r.status_code == 200, f"handoff responde 200 (got {r.status_code})")
bot, at, q1, st = state(c1)
check(bot is False, "handoff: is_bot_active=False")
check(at is None, "handoff: atendente_id continua NULL")
check(q1 is not None, "handoff: queued_at preenchido")
check(r.json().get("queued_at") is not None, "handoff: queued_at exposto na API")

# ============ 3. HANDOFF IDEMPOTENTE ============
print("3 — handoff repetido preserva a posicao na fila")
time.sleep(0.05)
r2 = client.post(f"/api/conversations/{c1}/handoff")
check(r2.status_code == 200, "segundo handoff responde 200")
bot, at, q2, st = state(c1)
check(q2 == q1, "retry do n8n NAO altera queued_at")
check(bot is False and at is None, "retry mantem bot=False e atendente=NULL")

# ============ 13b. BIA NAO agenda depois do handoff ============
print("13b — BIA NAO agendada DEPOIS do handoff")
scheduled.clear()
client.post("/webhook", json=inbound("wamid.S3", "5511900066601"))
check(scheduled == [], "pos-handoff: debounce da BIA NAO agendado")

# ============ 12. FIFO — inbound nao move a fila ============
print("12 — FIFO: mensagem do cliente nao muda queued_at")
bot, at, q3, st = state(c1)
check(q3 == q1, "inbound do cliente NAO altera queued_at")

# ============ 11. inbound em conversa aberta preserva estado ============
print("11 — inbound em conversa JA aberta preserva estado operacional")
check(bot is False and at is None, "inbound normal: bot/atendente inalterados")

# FIFO com tres conversas
print("12b — ordem FIFO estavel apos cutucada do cliente do meio")
client.post("/webhook", json=inbound("wamid.F1", "5511900066611"))
cA = conv_id_of("5511900066611")
client.post(f"/api/conversations/{cA}/handoff")
time.sleep(0.02)
client.post("/webhook", json=inbound("wamid.F2", "5511900066612"))
cB = conv_id_of("5511900066612")
client.post(f"/api/conversations/{cB}/handoff")
time.sleep(0.02)
client.post("/webhook", json=inbound("wamid.F3", "5511900066613"))
cC = conv_id_of("5511900066613")
client.post(f"/api/conversations/{cC}/handoff")
qB_antes = state(cB)[2]
time.sleep(0.05)
client.post("/webhook", json=inbound("wamid.F2b", "5511900066612", "oi?"))
qB_depois = state(cB)[2]
check(qB_depois == qB_antes, "B cutucou: queued_at de B inalterado")
ordem = sorted([(state(x)[2], x) for x in (cA, cB, cC)])
check([x for _, x in ordem] == [cA, cB, cC], "ordem FIFO permanece A, B, C")

# ============ 5. CLAIM ============
print("5 — claim: assume, desliga BIA, sai da fila")
r = client.post(f"/api/conversations/{c1}/claim")
check(r.status_code == 200, f"claim responde 200 (got {r.status_code})")
bot, at, q, st = state(c1)
check(at == 1, "claim: atendente_id = usuario autenticado")
check(bot is False, "claim: is_bot_active=False")
check(q is None, "claim: queued_at=NULL")

# ============ 5b. CLAIM DIRETO DA BIA (mutante F) ============
# Isolado de proposito: no fluxo acima o handoff ja tinha desligado a BIA,
# entao "bot=False apos claim" passava trivialmente. Aqui a conversa esta em
# BIA PURA, provando a transicao True -> False feita pelo proprio claim.
print("5b — claim direto de uma conversa em BIA desliga a BIA")
client.post("/webhook", json=inbound("wamid.B1", "5511900066621"))
cBia = conv_id_of("5511900066621")
bot_antes, at_antes, q_antes, _ = state(cBia)
check(bot_antes is True and at_antes is None and q_antes is None,
      "pre-condicao: conversa em BIA pura (bot=True, sem atendente, sem fila)")
r = client.post(f"/api/conversations/{cBia}/claim")
check(r.status_code == 200, "claim direto responde 200")
bot, at, q, _ = state(cBia)
check(bot is False, "claim a partir da BIA: is_bot_active True -> False")
check(at == 1, "claim a partir da BIA: atendente_id = current_user")
check(q is None, "claim a partir da BIA: queued_at continua NULL")

# ============ 6. CLAIM CONCORRENTE ============
print("6 — claim de outro usuario continua 409")
as_user(_U2())
r = client.post(f"/api/conversations/{c1}/claim")
check(r.status_code == 409, f"claim concorrente -> 409 (got {r.status_code})")
as_user(_U1())

# ============ 4. RACE — handoff DEPOIS do claim ============
print("4 — handoff atrasado NAO recoloca conversa ja assumida na fila")
r = client.post(f"/api/conversations/{c1}/handoff")
check(r.status_code == 200, "handoff pos-claim responde 200")
bot, at, q, st = state(c1)
check(q is None, "handoff pos-claim: queued_at continua NULL")
check(at == 1, "handoff pos-claim: humano continua atendendo")
check(bot is False, "handoff pos-claim: bot permanece desligado")

# ============ 7. ASSIGN ============
print("7 — assign transfere e mantem fora da fila")
r = client.post(f"/api/conversations/{c1}/assign", json={"user_id": 2})
check(r.status_code == 200, f"assign responde 200 (got {r.status_code})")
bot, at, q, st = state(c1)
check(at == 2, "assign: atendente_id = destino")
check(bot is False, "assign: is_bot_active=False")
check(q is None, "assign: queued_at=NULL")

# ============ 7b. ASSIGN A PARTIR DA FILA (mutante G) ============
# No fluxo acima o claim ja havia zerado queued_at, entao "queued=NULL apos
# assign" passava trivialmente. Aqui a conversa esta ESPERANDO na fila.
print("7b — assign a partir da fila zera queued_at")
client.post("/webhook", json=inbound("wamid.G1", "5511900066631"))
cFila = conv_id_of("5511900066631")
client.post(f"/api/conversations/{cFila}/handoff")
bot_antes, at_antes, q_antes, _ = state(cFila)
check(q_antes is not None and at_antes is None,
      "pre-condicao: conversa na fila (queued_at preenchido, sem atendente)")
r = client.post(f"/api/conversations/{cFila}/assign", json={"user_id": 2})
check(r.status_code == 200, "assign da fila responde 200")
bot, at, q, _ = state(cFila)
check(q is None, "assign a partir da fila: queued_at preenchido -> NULL")
check(at == 2, "assign a partir da fila: atendente_id = destino")
check(bot is False, "assign a partir da fila: is_bot_active=False")

# ============ 8. RELEASE ============
print("8 — release volta para o FIM da fila com queued_at novo")
r = client.post(f"/api/conversations/{c1}/release")
check(r.status_code == 200, f"release responde 200 (got {r.status_code})")
bot, at, q_rel, st = state(c1)
check(at is None, "release: atendente_id=NULL")
check(bot is False, "release: is_bot_active=False (nao volta para a BIA)")
check(q_rel is not None, "release: queued_at preenchido")
check(q_rel != q1, "release cria posicao NOVA (nao preserva a antiga)")

# ============ 8b. RELEASE SOBRESCREVE POSICAO ANTIGA (mutante H) ============
# Estado (atendente definido + queued_at definido) e inalcancavel pela API
# sadia, mas E alcancavel por dado legado ou pelo bypass do PUT /{id}
# (RISCO RESIDUAL documentado no PR). Injetamos direto no banco para provar
# que o release NUNCA preserva uma posicao antiga.
print("8b — release sobrescreve queued_at mesmo vindo de estado corrompido")
import datetime as _dt  # noqa: E402

_stale = _dt.datetime(2020, 1, 1, tzinfo=_dt.timezone.utc)
_db = SessionLocal()
_c = _db.query(Conversation).filter(Conversation.id == cFila).first()
_c.atendente_id = 2
_c.queued_at = _stale          # estado contraditorio, injetado de proposito
_db.commit()
_db.close()
check(state(cFila)[2] is not None, "pre-condicao: queued_at antigo presente com atendente")
r = client.post(f"/api/conversations/{cFila}/release")
check(r.status_code == 200, "release de estado corrompido responde 200")
bot, at, q_novo, _ = state(cFila)
check(at is None, "release corrompido: atendente_id=NULL")
check(q_novo is not None, "release corrompido: queued_at preenchido")
check(q_novo.replace(tzinfo=_dt.timezone.utc) != _stale,
      "release NUNCA preserva a posicao antiga (fim da fila, sempre)")

# ============ 9. INITIATE ============
print("9 — initiate: quem inicia assume o atendimento")
r = client.post("/api/conversations/initiate", json={"whatsapp": "5511900066699",
                                                     "nome": "Novo Contato"})
check(r.status_code == 200, f"initiate responde 200 (got {r.status_code})")
cN = conv_id_of("5511900066699")
bot, at, q, st = state(cN)
check(at == 1, "initiate: atendente_id = current_user (sem hardcode)")
check(bot is False, "initiate: is_bot_active=False")
check(q is None, "initiate: queued_at=NULL (nao entra na fila)")

# ============ 10. REABERTURA ============
print("10 — reabertura volta para a BIA sem herdar atendente")
client.post(f"/api/conversations/{c1}/claim")
r = client.put(f"/api/conversations/{c1}", json={"status": "encerrada"})
check(r.status_code == 200, "encerrar responde 200")
check(state(c1)[3] == "encerrada", "conversa encerrada")
scheduled.clear()
client.post("/webhook", json=inbound("wamid.S9", "5511900066601"))
bot, at, q, st = state(c1)
check(st == "aberta", "reabertura: status=aberta")
check(at is None, "reabertura: atendente_id=NULL (nao herda o anterior)")
check(bot is True, "reabertura: is_bot_active=True (volta para a BIA)")
check(q is None, "reabertura: queued_at=NULL")
check(scheduled == [c1], "13c — reabertura volta a agendar a BIA")

# ============ 14. FRONTEIRA COMERCIAL ============
print("14 — nenhuma operacao de fila escreve em leads")
check(leads_snapshot() == LEADS_BEFORE,
      "tabela leads intacta apos handoff/claim/assign/release/initiate/reabertura")

# ============ 15. AUTH DO HANDOFF ============
print("15 — handoff exige autenticacao")
main.app.dependency_overrides.pop(get_current_user, None)
anon = TestClient(main.app)
r = anon.post(f"/api/conversations/{c1}/handoff")
check(r.status_code == 401, f"handoff sem credencial -> 401 (got {r.status_code})")
r = anon.post(f"/api/conversations/{c1}/handoff", headers={"X-API-Key": "invalida"})
check(r.status_code == 401, f"handoff com API key invalida -> 401 (got {r.status_code})")
check("test-secret-key" not in r.text, "sem segredo na resposta do handoff")
as_user(_U1())

# ============ 16. MIGRATION m008 ============
print("16 — migration m008 num banco legado")
import importlib.util  # noqa: E402
from sqlalchemy import create_engine, inspect  # noqa: E402

LEGACY_DB = SCRATCH / "conv_m008_legacy.db"
if LEGACY_DB.exists():
    LEGACY_DB.unlink()
legacy = create_engine(f"sqlite:///{LEGACY_DB.as_posix()}")
with legacy.begin() as conn:
    # Schema PRE-m008 (sem queued_at, sem indices novos).
    conn.execute(text(
        "CREATE TABLE conversations ("
        "id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL, whatsapp VARCHAR(30) NOT NULL, "
        "nome VARCHAR(200), status VARCHAR(20) NOT NULL DEFAULT 'aberta', "
        "ultimo_msg TEXT, unread_count INTEGER NOT NULL DEFAULT 0, "
        "atendente_id INTEGER, is_bot_active BOOLEAN NOT NULL DEFAULT 1, "
        "responsavel_id INTEGER, responsavel_nome VARCHAR(200), "
        "created_at TIMESTAMP, updated_at TIMESTAMP, last_customer_msg_at TIMESTAMP)"
    ))
    # 2 contraditorias (atendente + bot ligado) e 2 que NAO podem ser tocadas.
    conn.execute(text(
        "INSERT INTO conversations (id, lead_id, whatsapp, atendente_id, is_bot_active) VALUES "
        "(1, 10, '551190001', 5, 1),"    # contraditoria -> corrigir
        "(2, 11, '551190002', 3, 1),"    # contraditoria -> corrigir
        "(3, 12, '551190003', NULL, 1)," # BIA legitima  -> preservar
        "(4, 13, '551190004', 5, 0)"     # ja correta    -> preservar
    ))

spec = importlib.util.spec_from_file_location(
    "m008", ROOT / "migrations" / "m008_conversas_queued_at.py")
m008 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m008)

acoes = m008.run(engine=legacy)
insp = inspect(legacy)
cols = {c["name"] for c in insp.get_columns("conversations")}
idx = {i["name"] for i in insp.get_indexes("conversations")}
check("queued_at" in cols, "m008: coluna queued_at criada")
check("ix_conversations_queued_at" in idx, "m008: indice em queued_at")
check("ix_conversations_atendente_id" in idx, "m008: indice em atendente_id")
check("bot-legado-desligado:2" in acoes, f"m008: 2 linhas legadas corrigidas ({acoes})")

with legacy.begin() as conn:
    rows = dict(conn.execute(text(
        "SELECT id, is_bot_active FROM conversations")).all())
    queued = conn.execute(text(
        "SELECT COUNT(*) FROM conversations WHERE queued_at IS NOT NULL")).scalar()
    total = conn.execute(text("SELECT COUNT(*) FROM conversations")).scalar()
check(rows[1] == 0 and rows[2] == 0, "m008: contraditorias com bot desligado")
check(rows[3] == 1, "m008: BIA legitima (sem atendente) preservada")
check(rows[4] == 0, "m008: linha ja correta preservada")
check(queued == 0, "m008: NENHUM backfill de queued_at (dados legados = NULL)")
check(total == 4, "m008: nenhuma linha perdida")

acoes2 = m008.run(engine=legacy)
check("queued_at:already-present" in acoes2, "m008: idempotente na 2a execucao")
check("bot-legado-desligado:0" in acoes2, "m008: 2a execucao nao altera mais nada")

print()
if failures:
    print(f"FALHOU ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("TODOS OS CHECKS PASSARAM")
