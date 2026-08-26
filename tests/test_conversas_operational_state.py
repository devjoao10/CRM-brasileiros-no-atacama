"""
PACOTE-A — estado operacional confiavel do Conversas.

Fonte unica: is_bot_active + atendente_id + queued_at + primeira_resposta_humana_at.

  BIA               -> bot=True,  atendente=NULL, queued=NULL,        prh=NULL
  Fila (sem dono)    -> bot=False, atendente=NULL, queued=<timestamp>, prh=NULL
  Fila (com dono)    -> bot=False, atendente=<id>, queued=<timestamp>, prh=NULL   <- ATRIBUIDA != ATENDIDA
  Atendendo (meus)   -> bot=False, atendente=<id>, queued=NULL,        prh=<timestamp>

AUDIT-2026-08-WA — mudanca de regra de negocio (nao e regressao): antes
`_apply_human_state` zerava queued_at assim que um atendente era definido, o que
fazia *atribuir* sinonimo de *atender* — a FILA DE ESPERA ficava vazia enquanto a
Bia dizia ao cliente que ele estava nela. Agora o eixo fila vs atendimento e
`primeira_resposta_humana_at`: NULL = ainda esperando (mesmo com dono), NOT NULL =
alguem respondeu de fato. Ver conversas/app/services/atendimento.py.

Prova que:
  1. Inbound novo nasce em BIA (bot ligado, sem atendente, sem fila, sem prh).
  2. Handoff desliga a BIA, RESOLVE um atendente elegivel e carimba queued_at —
     mas a conversa CONTINUA na fila (prh continua NULL: atribuir != atender).
  3. Handoff e IDEMPOTENTE: retry do n8n nao muda queued_at nem reresolve o
     atendente (nao vai pro fim da fila, nao troca de dono).
  4. RACE — handoff depois de claim: nao mexe na posicao da fila nem no dono.
  5. Claim: atendente=usuario autenticado, bot=False, queued_at PRESERVADO
     (assumir nao e atender).
  6. Claim concorrente continua 409 (trava anti-duplo-atendimento intacta).
  7. Assign: atendente=destino, bot=False, continua fora do universo da BIA e
     dentro da fila (prh continua NULL ate a primeira resposta).
  8. Release: atendente=NULL, bot=False, prh=NULL, queued_at=NOVO timestamp
     (fim da fila) — inclusive quando libera uma conversa ja atendida.
  9. Initiate: quem inicia assume E ja atendeu (prh preenchido), sem fila.
 10. Reabertura (encerrada + inbound): volta para a BIA, SEM herdar atendente
     nem a resposta humana anterior (prh volta a NULL).
 11. Inbound em conversa JA aberta nao toca o estado operacional (FIFO firme).
 12. FIFO: mensagem nova do cliente NAO altera queued_at nem a ordem da fila.
 13. BIA/debounce: agenda antes do handoff, NAO agenda depois, volta a agendar
     apos reabertura.
 14. Nenhuma operacao de fila escreve na tabela `leads`.
 15. Handoff exige autenticacao (sem credencial -> 401).
 16. Migration m008: coluna, indices, correcao de bot legado, dados preservados.
 17. resolver_atendente_elegivel: sem config usa um ativo; ATENDENTES_ELEGIVEIS
     fixa o id; sem ninguem elegivel fica sem dono (sem excecao, continua na fila).
 18. Ciclo completo do bug principal: atribuida-mas-nao-respondida fica na fila;
     abrir (dono ou outro usuario) nao move; resposta da BIA e envio humano que
     FALHA tambem nao movem; a PRIMEIRA resposta humana bem-sucedida move para
     MEUS e carimba prh; a segunda NAO reescreve o timestamp; release apos
     atendida devolve para o FIM da fila com prh limpo.
 19. Migration m012: coluna primeira_resposta_humana_at, indice, backfill
     conservador (so quem ja tinha dono + outbound), idempotente.

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
# AUDIT-2026-08-WA — fixa um UNICO elegivel para o fluxo principal ficar
# deterministico (mesmo efeito de "hoje": uma unica atendente operacional, ver
# docstring de resolver_atendente_elegivel). A secao 17 exercita explicitamente
# sem-config / com-config / sem-ninguem-elegivel.
os.environ["ATENDENTES_ELEGIVEIS"] = "1"

sys.path.insert(0, str(CONVERSAS_DIR))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import app.main as main  # noqa: E402
import app.routers.webhook as wh  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.auth import get_current_user, User  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
from app.services.outbound import record_outbound_message  # noqa: E402

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
        return (c.is_bot_active, c.atendente_id, c.queued_at,
                c.primeira_resposta_humana_at, c.status)
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
bot, at, q, prh, st = state(c1)
check(bot is True, "inbound novo: is_bot_active=True")
check(at is None, "inbound novo: atendente_id=NULL")
check(q is None, "inbound novo: queued_at=NULL")
check(prh is None, "inbound novo: primeira_resposta_humana_at=NULL")
check(st == "aberta", "inbound novo: status=aberta")

# ============ 13a. BIA agenda antes do handoff ============
print("13a — BIA agendada ANTES do handoff")
scheduled.clear()
client.post("/webhook", json=inbound("wamid.S2", "5511900066601"))
check(scheduled == [c1], "pre-handoff: debounce da BIA agendado")

# ============ 2. HANDOFF ============
print("2 — handoff desliga BIA, RESOLVE atendente e carimba queued_at (continua na fila)")
r = client.post(f"/api/conversations/{c1}/handoff")
check(r.status_code == 200, f"handoff responde 200 (got {r.status_code})")
bot, at, q1, prh, st = state(c1)
check(bot is False, "handoff: is_bot_active=False")
# AUDIT-2026-08-WA — inverte "atendente_id continua NULL": handoff agora
# RESOLVE um atendente elegivel (ATENDENTES_ELEGIVEIS=1 fixado no topo do
# arquivo -> sempre o usuario 1, o comportamento de hoje com uma unica atendente).
check(at == 1, "handoff: atendente_id RESOLVIDO (ATENDENTES_ELEGIVEIS=1)")
check(q1 is not None, "handoff: queued_at preenchido")
check(r.json().get("queued_at") is not None, "handoff: queued_at exposto na API")
# AUDIT-2026-08-WA — REGRESSAO GUARD do bug principal: ter dono nao e ter sido
# atendida. E exatamente o oposto do bug relatado (fila vazia enquanto a Bia
# dizia ao cliente que ele estava nela).
check(prh is None, "handoff: primeira_resposta_humana_at continua NULL (ATRIBUIDO != ATENDIDO)")
check(at is not None and prh is None,
      "REGRESSAO GUARD: atribuida-mas-nao-respondida continua elegivel para a FILA "
      "(_inbox_predicates('fila') so exige prh IS NULL, nao atendente NULL)")

# ============ 3. HANDOFF IDEMPOTENTE ============
print("3 — handoff repetido preserva a posicao na fila e nao rerresolve o dono")
time.sleep(0.05)
r2 = client.post(f"/api/conversations/{c1}/handoff")
check(r2.status_code == 200, "segundo handoff responde 200")
bot, at, q2, prh, st = state(c1)
check(q2 == q1, "retry do n8n NAO altera queued_at")
# AUDIT-2026-08-WA — inverte "bot=False e atendente=NULL": atendente agora e 1
# (resolvido no primeiro handoff); o retry NAO rerresolve nem troca de dono.
check(bot is False and at == 1, "retry mantem bot=False e atendente=1 (nao rerresolve)")
check(prh is None, "retry: primeira_resposta_humana_at continua NULL")

# ============ 13b. BIA NAO agenda depois do handoff ============
print("13b — BIA NAO agendada DEPOIS do handoff")
scheduled.clear()
client.post("/webhook", json=inbound("wamid.S3", "5511900066601"))
check(scheduled == [], "pos-handoff: debounce da BIA NAO agendado")

# ============ 12. FIFO — inbound nao move a fila ============
print("12 — FIFO: mensagem do cliente nao muda queued_at")
bot, at, q3, prh, st = state(c1)
check(q3 == q1, "inbound do cliente NAO altera queued_at")

# ============ 11. inbound em conversa aberta preserva estado ============
print("11 — inbound em conversa JA aberta preserva estado operacional")
# AUDIT-2026-08-WA — atendente agora e 1 (nao mais NULL) desde o handoff.
check(bot is False and at == 1, "inbound normal: bot/atendente inalterados (permanece 1)")
check(prh is None, "inbound normal: primeira_resposta_humana_at inalterado (continua NULL)")

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
print("5 — claim: assume, desliga BIA, MANTEM a posicao na fila")
r = client.post(f"/api/conversations/{c1}/claim")
check(r.status_code == 200, f"claim responde 200 (got {r.status_code})")
bot, at, q, prh, st = state(c1)
check(at == 1, "claim: atendente_id = usuario autenticado")
check(bot is False, "claim: is_bot_active=False")
# AUDIT-2026-08-WA — inverte "claim: queued_at=NULL": assumir NAO e atender. A
# conversa so sai da fila na primeira resposta humana (ver secao 18).
check(q == q1, "claim: queued_at PRESERVADO (assumir != atender)")
check(prh is None, "claim: primeira_resposta_humana_at continua NULL")

# ============ 5b. CLAIM DIRETO DA BIA (mutante F) ============
# Isolado de proposito: no fluxo acima o handoff ja tinha desligado a BIA,
# entao "bot=False apos claim" passava trivialmente. Aqui a conversa esta em
# BIA PURA, provando a transicao True -> False feita pelo proprio claim.
print("5b — claim direto de uma conversa em BIA desliga a BIA e entra na fila")
client.post("/webhook", json=inbound("wamid.B1", "5511900066621"))
cBia = conv_id_of("5511900066621")
bot_antes, at_antes, q_antes, prh_antes, _ = state(cBia)
check(bot_antes is True and at_antes is None and q_antes is None and prh_antes is None,
      "pre-condicao: conversa em BIA pura (bot=True, sem atendente, sem fila, sem prh)")
r = client.post(f"/api/conversations/{cBia}/claim")
check(r.status_code == 200, "claim direto responde 200")
bot, at, q, prh, _ = state(cBia)
check(bot is False, "claim a partir da BIA: is_bot_active True -> False")
check(at == 1, "claim a partir da BIA: atendente_id = current_user")
# AUDIT-2026-08-WA — inverte "queued_at continua NULL": ao sair da BIA sem
# resposta humana a conversa PASSA a esperar — o relogio da fila comeca agora.
check(q is not None, "claim a partir da BIA: queued_at PREENCHIDO (entra na fila ao sair da BIA)")
check(prh is None, "claim a partir da BIA: primeira_resposta_humana_at continua NULL")

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
bot, at, q, prh, st = state(c1)
# AUDIT-2026-08-WA — inverte "queued_at continua NULL": agora ha uma posicao
# REAL na fila (desde o claim) e o handoff atrasado PRESERVA essa posicao.
check(q == q1, "handoff pos-claim: queued_at PRESERVADO (mesma posicao, nao reordena)")
check(at == 1, "handoff pos-claim: humano continua atendendo")
check(bot is False, "handoff pos-claim: bot permanece desligado")
check(prh is None, "handoff pos-claim: continua sem resposta humana")

# ============ 7. ASSIGN ============
print("7 — assign transfere e MANTEM a conversa na fila")
r = client.post(f"/api/conversations/{c1}/assign", json={"user_id": 2})
check(r.status_code == 200, f"assign responde 200 (got {r.status_code})")
bot, at, q, prh, st = state(c1)
check(at == 2, "assign: atendente_id = destino")
check(bot is False, "assign: is_bot_active=False")
# AUDIT-2026-08-WA — inverte "assign: queued_at=NULL": atribuir nao tira da
# fila. ACHADO: assign_conversation (conversas/app/routers/conversations.py)
# chama _apply_human_state SEM keep_queue_position=True (diferente de claim e
# handoff), entao na pratica ele troca queued_at por um NOVO "agora" em vez de
# preservar o antigo. O que esta suite pode garantir e que ele NAO fica NULL;
# a divergencia de posicao exata esta reportada no resumo final.
check(q is not None, "assign: queued_at continua preenchido (NAO fica NULL)")
check(prh is None, "assign: primeira_resposta_humana_at continua NULL (ainda nao foi atendida)")

# ============ 7b. ASSIGN A PARTIR DA FILA (mutante G) ============
# AUDIT-2026-08-WA — sob a regra nova a conversa do passo 7 acima JA estava na
# fila mesmo depois do assign. Aqui isolamos o caminho FILA -> assign a partir
# de uma conversa que so passou pelo handoff (nunca foi claim/atendida), para
# garantir que reatribuir NUNCA zera a fila.
print("7b — assign a partir da fila mantem a conversa na fila")
client.post("/webhook", json=inbound("wamid.G1", "5511900066631"))
cFila = conv_id_of("5511900066631")
client.post(f"/api/conversations/{cFila}/handoff")
bot_antes, at_antes, q_antes, prh_antes, _ = state(cFila)
# AUDIT-2026-08-WA — inverte a pre-condicao "sem atendente": o handoff agora
# resolve um dono (1) e MESMO ASSIM a conversa fica na fila (prh NULL).
check(q_antes is not None and at_antes == 1 and prh_antes is None,
      "pre-condicao: na fila, JA com dono (1), ainda NAO atendida")
r = client.post(f"/api/conversations/{cFila}/assign", json={"user_id": 2})
check(r.status_code == 200, "assign da fila responde 200")
bot, at, q, prh, _ = state(cFila)
# AUDIT-2026-08-WA — inverte "queued_at preenchido -> NULL": reatribuir uma
# conversa da fila NAO tira ela da fila.
check(q is not None, "assign a partir da fila: queued_at continua preenchido (NAO fica NULL)")
check(at == 2, "assign a partir da fila: atendente_id = destino")
check(bot is False, "assign a partir da fila: is_bot_active=False")
check(prh is None, "assign a partir da fila: continua sem resposta humana (continua na FILA)")

# ============ 8. RELEASE ============
print("8 — release volta para o FIM da fila com queued_at novo")
r = client.post(f"/api/conversations/{c1}/release")
check(r.status_code == 200, f"release responde 200 (got {r.status_code})")
bot, at, q_rel, prh, st = state(c1)
check(at is None, "release: atendente_id=NULL")
check(bot is False, "release: is_bot_active=False (nao volta para a BIA)")
check(q_rel is not None, "release: queued_at preenchido")
check(q_rel != q1, "release cria posicao NOVA (nao preserva a antiga)")
check(prh is None, "release: primeira_resposta_humana_at continua NULL (nunca foi atendida)")

# ============ 8b. RELEASE SOBRESCREVE POSICAO ANTIGA (mutante H) ============
# AUDIT-2026-08-WA — sob a regra NOVA, "dono definido + queued_at definido" ja
# NAO e mais um estado contraditorio/inalcancavel: e o estado NORMAL de uma
# conversa atribuida-mas-nao-respondida (ver secoes 2, 7, 7b). O que ainda
# provamos aqui e mais estreito: mesmo vindo de um queued_at ANTIGO DEMAIS
# (injetado direto no banco), release nunca herda uma posicao velha.
print("8b — release sobrescreve queued_at mesmo vindo de estado corrompido")
import datetime as _dt  # noqa: E402

_stale = _dt.datetime(2020, 1, 1, tzinfo=_dt.timezone.utc)
_db = SessionLocal()
_c = _db.query(Conversation).filter(Conversation.id == cFila).first()
_c.atendente_id = 2
_c.queued_at = _stale          # posicao antiga demais, injetada de proposito
_db.commit()
_db.close()
check(state(cFila)[2] is not None, "pre-condicao: queued_at antigo presente com atendente")
r = client.post(f"/api/conversations/{cFila}/release")
check(r.status_code == 200, "release de estado corrompido responde 200")
bot, at, q_novo, _, _ = state(cFila)
check(at is None, "release corrompido: atendente_id=NULL")
check(q_novo is not None, "release corrompido: queued_at preenchido")
check(q_novo.replace(tzinfo=_dt.timezone.utc) != _stale,
      "release NUNCA preserva a posicao antiga (fim da fila, sempre)")

# ============ 9. INITIATE ============
print("9 — initiate: quem inicia assume o atendimento (ja atendida, sem fila)")
r = client.post("/api/conversations/initiate", json={"whatsapp": "5511900066699",
                                                     "nome": "Novo Contato"})
check(r.status_code == 200, f"initiate responde 200 (got {r.status_code})")
cN = conv_id_of("5511900066699")
bot, at, q, prh, st = state(cN)
check(at == 1, "initiate: atendente_id = current_user (sem hardcode)")
check(bot is False, "initiate: is_bot_active=False")
check(q is None, "initiate: queued_at=NULL (nao entra na fila)")
# AUDIT-2026-08-WA — nova asserção: quem inicia ja atendeu (nao fica
# "atribuida mas esperando"); sem isto a conversa nasceria elegivel para a fila.
check(prh is not None, "initiate: primeira_resposta_humana_at preenchido (nasce ja atendida)")

# ============ 10. REABERTURA ============
print("10 — reabertura volta para a BIA sem herdar atendente nem resposta humana")
client.post(f"/api/conversations/{c1}/claim")
r = client.put(f"/api/conversations/{c1}", json={"status": "encerrada"})
check(r.status_code == 200, "encerrar responde 200")
check(state(c1)[4] == "encerrada", "conversa encerrada")
scheduled.clear()
client.post("/webhook", json=inbound("wamid.S9", "5511900066601"))
bot, at, q, prh, st = state(c1)
check(st == "aberta", "reabertura: status=aberta")
check(at is None, "reabertura: atendente_id=NULL (nao herda o anterior)")
check(bot is True, "reabertura: is_bot_active=True (volta para a BIA)")
check(q is None, "reabertura: queued_at=NULL")
# AUDIT-2026-08-WA — nova asserção: reabertura e um ciclo NOVO de atendimento;
# sem isto a conversa reabriria ja "atendida" e nunca apareceria na fila.
check(prh is None, "reabertura: primeira_resposta_humana_at=NULL (novo ciclo)")
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

# ============ 17. RESOLUCAO DE ATENDENTE ELEGIVEL ============
print("17 — resolver_atendente_elegivel: sem config, com config, sem ninguem")

print("17a — sem ATENDENTES_ELEGIVEIS: usa algum usuario ATIVO")
os.environ.pop("ATENDENTES_ELEGIVEIS", None)
client.post("/webhook", json=inbound("wamid.EL1", "5511900066651"))
cEl1 = conv_id_of("5511900066651")
r = client.post(f"/api/conversations/{cEl1}/handoff")
check(r.status_code == 200, "handoff sem ATENDENTES_ELEGIVEIS responde 200")
bot, at, q, prh, st = state(cEl1)
check(at in (1, 2), f"sem config: escolhe algum usuario ATIVO (got {at})")
check(prh is None and q is not None, "sem config: continua na fila (nao atendida)")

print("17b — ATENDENTES_ELEGIVEIS=2: sempre esse id")
os.environ["ATENDENTES_ELEGIVEIS"] = "2"
client.post("/webhook", json=inbound("wamid.EL2", "5511900066652"))
cEl2 = conv_id_of("5511900066652")
r = client.post(f"/api/conversations/{cEl2}/handoff")
check(r.status_code == 200, "handoff com ATENDENTES_ELEGIVEIS=2 responde 200")
at2 = state(cEl2)[1]
check(at2 == 2, f"config fixa o id configurado (got {at2})")

print("17c — nenhum atendente elegivel: fica sem dono, sem excecao, continua na fila")
_db = SessionLocal()
_db.query(User).filter(User.id.in_([1, 2])).update(
    {User.is_active: False}, synchronize_session=False)
_db.commit()
_db.close()
os.environ.pop("ATENDENTES_ELEGIVEIS", None)
client.post("/webhook", json=inbound("wamid.EL3", "5511900066653"))
cEl3 = conv_id_of("5511900066653")
r = client.post(f"/api/conversations/{cEl3}/handoff")
check(r.status_code == 200, "handoff sem ninguem elegivel NAO levanta excecao (200)")
bot3, at3, q3b, prh3, st3 = state(cEl3)
check(at3 is None, "sem elegivel: atendente_id continua NULL")
check(bot3 is False, "sem elegivel: bot desligado mesmo assim (saiu da BIA)")
check(q3b is not None, "sem elegivel: queued_at preenchido (continua na fila, so sem dono)")
check(prh3 is None, "sem elegivel: primeira_resposta_humana_at continua NULL")

# Restaura para o resto do arquivo (deterministico: ATENDENTES_ELEGIVEIS=1).
_db = SessionLocal()
_db.query(User).filter(User.id.in_([1, 2])).update(
    {User.is_active: True}, synchronize_session=False)
_db.commit()
_db.close()
os.environ["ATENDENTES_ELEGIVEIS"] = "1"

# ============ 18. CICLO COMPLETO DO BUG PRINCIPAL ============
print("18 — atribuida-mas-nao-respondida ate ser liberada apos atendida")

client.post("/webhook", json=inbound("wamid.N1", "5511900066641"))
cNovo = conv_id_of("5511900066641")
# inbound() usa timestamp fixo de 2023 (fora da janela de 24h do WhatsApp);
# abrimos a janela manualmente so aqui, onde a secao precisa exercitar
# POST /messages de verdade (send_message exige janela aberta p/ texto livre).
_db = SessionLocal()
_c = _db.query(Conversation).filter(Conversation.id == cNovo).first()
_c.last_customer_msg_at = _dt.datetime.now(_dt.timezone.utc)
_db.commit()
_db.close()

r = client.post(f"/api/conversations/{cNovo}/handoff")
check(r.status_code == 200, "18 — handoff inicial responde 200")
bot, at, q_h, prh, st = state(cNovo)
check(at is not None and prh is None and q_h is not None,
      "18 — ATRIBUIDA mas NAO RESPONDIDA: tem dono, continua elegivel para a fila")

print("18a — abrir a conversa (dono) NAO move o estado operacional")
r = client.get(f"/api/conversations/{cNovo}?opening=true")
check(r.status_code == 200, "GET (dono) responde 200")
check(state(cNovo) == (bot, at, q_h, prh, st),
      "abrir (dono) preserva bot/atendente/fila/prh/status")

print("18b — OUTRO usuario abrindo tambem NAO move")
as_user(_U2())
r = client.get(f"/api/conversations/{cNovo}?opening=true")
check(r.status_code == 200, "GET (outro usuario) responde 200")
as_user(_U1())
check(state(cNovo) == (bot, at, q_h, prh, st), "abrir (outro usuario) preserva o estado")

print("18c — resposta da BIA (autor_user_id=None) NAO tira da fila")
_db = SessionLocal()
_conv = _db.query(Conversation).filter(Conversation.id == cNovo).first()
record_outbound_message(
    _db, _conv, "resposta automatica da Bia", "text",
    {"messages": [{"id": "wamid.BIA1"}]}, autor_user_id=None,
)
_db.close()
check(state(cNovo) == (bot, at, q_h, prh, st),
      "outbound SEM autor_user_id (Bia/auto-resposta) nao move fila/atendimento")

print("18d — envio humano que FALHA na Meta tambem NAO tira da fila")
_db = SessionLocal()
_conv = _db.query(Conversation).filter(Conversation.id == cNovo).first()
record_outbound_message(
    _db, _conv, "tentativa que falha", "text",
    {"error": True, "summary": "simulado"}, autor_user_id=1,
)
_db.close()
check(state(cNovo) == (bot, at, q_h, prh, st),
      "envio humano que FALHOU nao marca primeira_resposta_humana_at")

print("18e — a PRIMEIRA resposta humana bem-sucedida move para MEUS")
_orig_send = wh.whatsapp.send_text_message
_ok_send_seq = {"n": 0}


async def _ok_send(*a, **k):
    # whatsapp_msg_id e UNIQUE em messages: cada chamada precisa de um wamid
    # diferente (18f manda uma SEGUNDA mensagem humana na mesma conversa).
    _ok_send_seq["n"] += 1
    return {"messages": [{"id": f"wamid.HUMANO{_ok_send_seq['n']}"}]}


wh.whatsapp.send_text_message = _ok_send
r = client.post(f"/api/conversations/{cNovo}/messages",
                 json={"content": "Oi, aqui e a Julia", "msg_type": "text"})
check(r.status_code == 200, f"primeiro envio humano responde 200 (got {r.status_code})")
bot2, at2, q2, prh2, st2 = state(cNovo)
check(prh2 is not None, "primeira resposta humana: primeira_resposta_humana_at preenchido")
check(q2 is None, "primeira resposta humana: sai da fila (queued_at=NULL)")
check(at2 == at, "primeira resposta humana: mantem o mesmo atendente")
check(bot2 is False, "primeira resposta humana: bot continua desligado")

print("18f — a SEGUNDA mensagem humana NAO reescreve o timestamp")
r2 = client.post(f"/api/conversations/{cNovo}/messages",
                  json={"content": "Segunda mensagem", "msg_type": "text"})
check(r2.status_code == 200, "segundo envio humano responde 200")
prh3b = state(cNovo)[3]
check(prh3b == prh2, "segunda mensagem humana NAO altera primeira_resposta_humana_at")
wh.whatsapp.send_text_message = _orig_send

print("18g — release APOS atendida: volta para o FIM da fila, prh limpo")
r = client.post(f"/api/conversations/{cNovo}/release")
check(r.status_code == 200, "release (pos-atendimento) responde 200")
bot4, at4, q4, prh4, st4 = state(cNovo)
check(at4 is None, "release apos atendida: atendente_id=NULL")
check(prh4 is None, "release apos atendida: primeira_resposta_humana_at volta a NULL")
check(q4 is not None, "release apos atendida: queued_at preenchido (fim da fila, de novo)")
check(bot4 is False, "release apos atendida: bot continua desligado (nao volta pra BIA)")

# ============ 19. MIGRATION m012 ============
print("19 — migration m012 num banco legado (pos-m008)")
LEGACY_DB_012 = SCRATCH / "conv_m012_legacy.db"
if LEGACY_DB_012.exists():
    LEGACY_DB_012.unlink()
legacy012 = create_engine(f"sqlite:///{LEGACY_DB_012.as_posix()}")
with legacy012.begin() as conn:
    # Schema PRE-m012 (pos-m008/m011: ja tem queued_at, ainda sem
    # primeira_resposta_humana_at) + tabela messages minima para o EXISTS do backfill.
    conn.execute(text(
        "CREATE TABLE conversations ("
        "id INTEGER PRIMARY KEY, lead_id INTEGER NOT NULL, whatsapp VARCHAR(30) NOT NULL, "
        "nome VARCHAR(200), status VARCHAR(20) NOT NULL DEFAULT 'aberta', "
        "atendente_id INTEGER, is_bot_active BOOLEAN NOT NULL DEFAULT 1, "
        "queued_at TIMESTAMP, created_at TIMESTAMP, updated_at TIMESTAMP)"
    ))
    conn.execute(text(
        "CREATE TABLE messages ("
        "id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL, "
        "direction VARCHAR(10) NOT NULL, content TEXT, created_at TIMESTAMP)"
    ))
    conn.execute(text(
        "INSERT INTO conversations (id, lead_id, whatsapp, status, atendente_id, is_bot_active, created_at) VALUES "
        "(1, 30, '551190020', 'aberta', 5, 0, '2026-01-01 10:00:00'),"    # A: candidata (bot off, tem outbound)
        "(2, 31, '551190021', 'aberta', 5, 0, '2026-01-01 10:00:00'),"    # B: dono, mas SEM outbound
        "(3, 32, '551190022', 'aberta', NULL, 1, '2026-01-01 10:00:00')," # C: ainda na BIA
        "(4, 33, '551190023', 'aberta', NULL, 0, '2026-01-01 10:00:00')," # D: fila, sem dono
        "(5, 34, '551190024', 'encerrada', 5, 0, '2026-01-01 10:00:00')" # E: encerrada, tem outbound
    ))
    conn.execute(text(
        "INSERT INTO messages (id, conversation_id, direction, content, created_at) VALUES "
        "(1, 1, 'outbound', 'oi', '2026-01-01 10:05:00'),"
        "(2, 2, 'inbound', 'oi', '2026-01-01 10:05:00'),"
        "(3, 5, 'outbound', 'tchau', '2026-01-01 10:05:00')"
    ))

spec012 = importlib.util.spec_from_file_location(
    "m012", ROOT / "migrations" / "m012_conversas_primeira_resposta_humana.py")
m012 = importlib.util.module_from_spec(spec012)
spec012.loader.exec_module(m012)

acoes012 = m012.run(engine=legacy012)
insp012 = inspect(legacy012)
cols012 = {c["name"] for c in insp012.get_columns("conversations")}
idx012 = {i["name"] for i in insp012.get_indexes("conversations")}
check("primeira_resposta_humana_at" in cols012, "m012: coluna primeira_resposta_humana_at criada")
check("ix_conversations_primeira_resposta_humana_at" in idx012, "m012: indice criado")
check("backfill-em-atendimento:1" in acoes012, f"m012: backfill marca SO a conversa 1 ({acoes012})")

with legacy012.begin() as conn:
    rows012 = dict(conn.execute(text(
        "SELECT id, primeira_resposta_humana_at FROM conversations")).all())
    total012 = conn.execute(text("SELECT COUNT(*) FROM conversations")).scalar()
check(rows012[1] == "2026-01-01 10:00:00",
      f"m012: conversa 1 (bot off + dono + outbound) recebe prh = conversations.created_at (got {rows012[1]!r})")
check(rows012[2] is None, "m012: conversa 2 (dono mas SEM outbound) NAO recebeu prh")
check(rows012[3] is None, "m012: conversa 3 (ainda na BIA) NAO recebeu prh")
check(rows012[4] is None, "m012: conversa 4 (fila, sem dono) NAO recebeu prh")
check(rows012[5] is None, "m012: conversa 5 (encerrada, mesmo com outbound) NAO recebeu prh")
check(total012 == 5, "m012: nenhuma linha perdida")

acoes012_2 = m012.run(engine=legacy012)
check("primeira_resposta_humana_at:already-present" in acoes012_2, "m012: idempotente na 2a execucao (coluna)")
check("backfill-em-atendimento:0" in acoes012_2, "m012: 2a execucao nao backfilla mais nada")
with legacy012.begin() as conn:
    row1_again = conn.execute(text(
        "SELECT primeira_resposta_humana_at FROM conversations WHERE id = 1")).scalar()
check(row1_again == rows012[1], "m012: 2a execucao NAO reescreve o valor ja gravado")

print()
if failures:
    print(f"FALHOU ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("TODOS OS CHECKS PASSARAM")
