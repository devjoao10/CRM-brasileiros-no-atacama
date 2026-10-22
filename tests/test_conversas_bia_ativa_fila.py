"""
CONV-BIA-FILA-01 — a Bia PERMANECE ativa enquanto a conversa espera na fila humana.

Regra de negocio (D1/D2/D4 da rodada 2026-10-06): entrar ou continuar na fila NAO
desliga a Bia. Quem desliga e a vendedora, manualmente — botao "Desativar Bot"
(`PUT {"is_bot_active": false}`), "Assumir" (`/claim`) ou a primeira mensagem
humana. Handoff, `/assign`, `/release` e o `PUT` que so troca o atendente
PRESERVAM o `is_bot_active` como esta, e NUNCA religam uma Bia que a vendedora ja
desligou. Antes, todos eles gravavam `is_bot_active=False` no ponto unico
`aplicar_estado_humano`: a Bia calava no instante em que a conversa entrava na
fila, e o workflow n8n "Handoff Humano -> Julia" (handoff + handoff + assign)
desligava de novo no ultimo passo.

Como o flag deixou de separar o lado da Bia do lado humano, `queued_at` passou a
classificar o lado humano (`_inbox_predicates`):

    bia   = aberta AND bot ligado AND queued_at IS NULL
    fila  = aberta AND (bot desligado OR queued_at NOT NULL) AND prh IS NULL
    todos = aberta AND (bot desligado OR queued_at NOT NULL)
    meus  = inalterada (bot desligado + prh NOT NULL + dono = eu)

Prova que (tudo via HTTP real, TestClient sobre SQLite):
  1. Handoff por lead numa conversa com a Bia ligada: a Bia continua ligada,
     queued_at e carimbado, o dono e resolvido; a conversa aparece em fila e
     todos, NAO em bia; /counts acompanha (fila, aguardando_humano, bia, todos).
  2. Handoff repetido (por id e por lead) preserva queued_at, dono e is_bot_active.
  3. A sequencia do n8n (handoff + handoff + assign) deixa a Bia ligada, troca o
     dono e preserva queued_at.
  4. Release: com a Bia ligada continua ligada; com a Bia desligada (apos /claim)
     continua desligada. Em ambos a conversa volta ao FIM da fila.
  5. PUT so com atendente_id preserva o flag; PUT {"is_bot_active": false} desliga,
     preserva queued_at e dono e a conversa continua em fila; PUT
     {"is_bot_active": true} (D3) segue devolvendo tudo a Bia, inclusive o `bia`.
  6. Claim desliga a Bia (inalterado).
  7. A primeira mensagem humana numa conversa da fila com a Bia ligada desliga a
     Bia, preenche primeira_resposta_humana_at, tira de fila e poe em meus.
  8. Handoff/assign/release numa conversa cuja Bia ja foi desligada a mao: segue
     desligada (nunca religa).
  9. Triagem (Bia ligada, sem queued_at) aparece SO em bia.
 10. Contrato de `aplicar_estado_humano`: o padrao desliga (a Bia degradada do
     webhook depende disso); `desligar_bia=False` preserva e nunca religa.
 11. Invariante D4: bia e todos sao disjuntas e juntas cobrem toda conversa aberta.
 12. "Desativar Bot" (`PUT {"is_bot_active": false}`) nao depende de o DONO ATUAL estar ativo:
     um dono desativado nao pode travar o botao (200, Bia desligada, dono e queued_at
     preservados). So o atendente NOMEADO no payload e validado como ativo (404 se nao for).
     E o conversas.js so mostra o toast de sucesso e recarrega a conversa quando o PUT deu certo.

Roda standalone:  python tests/test_conversas_bia_ativa_fila.py
"""
import datetime as dt
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_bia_ativa_fila_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"  # sem rede: nada e encaminhado ao n8n
# Um UNICO elegivel deixa o dono do handoff deterministico (usuario 1) — mesma
# tecnica de test_conversas_operational_state.py.
os.environ["ATENDENTES_ELEGIVEIS"] = "1"

sys.path.insert(0, str(CONVERSAS_DIR))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import app.main as main  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.auth import get_current_user, User  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
from app.services import whatsapp  # noqa: E402
from app.services.atendimento import aplicar_estado_humano  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


Base.metadata.create_all(bind=engine)

# Tabela CRM-shaped `leads` no MESMO sqlite: a listagem consulta o responsavel dos
# leads vinculados; sem a tabela a consulta cairia no except (inofensivo, mas
# barulhento). Os leads ficam sem responsavel para a listagem nao "reparar" nada.
with engine.begin() as conn:
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS leads ("
        "id INTEGER PRIMARY KEY, nome VARCHAR(200), whatsapp VARCHAR(30), "
        "email VARCHAR(255), responsavel_id INTEGER)"
    ))
    conn.execute(text(
        "INSERT INTO leads (id, nome, whatsapp, email, responsavel_id) "
        "VALUES (7001, 'Lead Fila Bia', '5511900070001', 'l@x.com', NULL)"
    ))

# Usuarios reais na tabela compartilhada (assign e PUT validam is_active).
_db = SessionLocal()
for uid, nome, email in ((1, "Julia", "julia@local"), (2, "Joao", "joao@local")):
    if not _db.query(User).filter(User.id == uid).first():
        _db.add(User(id=uid, nome=nome, email=email,
                     hashed_password="x", role="user", is_active=True))
# Usuario 3: vendedora DESATIVADA depois de receber conversas (secao 12 — o dono atual inativo).
if not _db.query(User).filter(User.id == 3).first():
    _db.add(User(id=3, nome="Maria", email="maria@local",
                 hashed_password="x", role="USER", is_active=False))
_db.commit()
_db.close()


class _U1:
    id = 1
    nome = "Julia"
    email = "julia@local"
    role = "user"
    is_active = True


main.app.dependency_overrides[get_current_user] = lambda: _U1()
client = TestClient(main.app)


def nova_conversa(whatsapp_num, lead_id=0, **campos):
    """Cria uma conversa em TRIAGEM (Bia ligada, sem dono, sem fila) direto no banco."""
    db = SessionLocal()
    try:
        c = Conversation(lead_id=lead_id, whatsapp=whatsapp_num, nome=f"Cliente {whatsapp_num[-4:]}",
                         status="aberta", is_bot_active=True, **campos)
        db.add(c)
        db.commit()
        return c.id
    finally:
        db.close()


def estado(conv_id):
    """Le o estado operacional direto do banco (nao confia na serializacao)."""
    db = SessionLocal()
    try:
        c = db.query(Conversation).filter(Conversation.id == conv_id).first()
        return {
            "bot": c.is_bot_active,
            "atendente": c.atendente_id,
            "queued_at": c.queued_at,
            "prh": c.primeira_resposta_humana_at,
            "status": c.status,
        }
    finally:
        db.close()


ABAS = ("bia", "fila", "meus", "todos", "encerradas")


def ids_da_aba(aba):
    r = client.get(f"/api/conversations?inbox={aba}&limit=200")
    assert r.status_code == 200, (aba, r.status_code, r.text)
    return {c["id"] for c in r.json()["conversations"]}


def abas(conv_id):
    """Conjunto de abas do inbox em que a conversa aparece (para o usuario autenticado)."""
    return {aba for aba in ABAS if conv_id in ids_da_aba(aba)}


def counts():
    r = client.get("/api/conversations/counts")
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()


def post(path, **kw):
    return client.post(f"/api/conversations{path}", **kw)


def put(conv_id, body):
    return client.put(f"/api/conversations/{conv_id}", json=body)


# ============ 1. HANDOFF POR LEAD COM A BIA LIGADA ============
print("1 — handoff por lead: a Bia continua ligada, a conversa entra na fila")
c1 = nova_conversa("5511900070001", lead_id=7001)
e = estado(c1)
check(e["bot"] is True and e["queued_at"] is None and e["atendente"] is None,
      "pre-condicao: triagem (Bia ligada, sem dono, sem queued_at)")
check(abas(c1) == {"bia"}, "pre-condicao: a triagem aparece so em bia")
antes = counts()

r = post("/by-lead/7001/handoff")
check(r.status_code == 200, f"handoff por lead responde 200 (got {r.status_code})")
e1 = estado(c1)
check(e1["bot"] is True, "handoff: is_bot_active continua True (a Bia segue respondendo na fila)")
check(e1["queued_at"] is not None, "handoff: queued_at preenchido")
check(e1["atendente"] == 1, "handoff: atendente resolvido (ATENDENTES_ELEGIVEIS=1)")
check(e1["prh"] is None, "handoff: primeira_resposta_humana_at continua NULL (atribuir != atender)")
corpo = r.json()
check(corpo.get("is_bot_active") is True and corpo.get("queued_at") is not None,
      "handoff: a resposta da API ja expoe Bia ligada + queued_at")
check(abas(c1) == {"fila", "todos"}, f"handoff: aparece em fila e todos, NAO em bia (got {sorted(abas(c1))})")
depois = counts()
check(depois["fila"] == antes["fila"] + 1, "/counts: fila +1")
check(depois["aguardando_humano"] == antes["aguardando_humano"] + 1, "/counts: aguardando_humano +1")
check(depois["todos"] == antes["todos"] + 1, "/counts: todos +1")
check(depois["bia"] == antes["bia"] - 1, "/counts: bia -1 (saiu da triagem, nao esta mais em bia)")
check(depois["meus"] == antes["meus"], "/counts: meus inalterado")

# ============ 2. HANDOFF REPETIDO ============
print("2 — handoff repetido preserva queued_at, dono e is_bot_active")
time.sleep(0.05)
r_id = post(f"/{c1}/handoff")
r_lead = post("/by-lead/7001/handoff")
check(r_id.status_code == 200 and r_lead.status_code == 200, "handoff repetido (por id e por lead) responde 200")
e = estado(c1)
check(e["queued_at"] == e1["queued_at"], "retry do handoff NAO altera queued_at (posicao na fila intacta)")
check(e["atendente"] == e1["atendente"], "retry do handoff NAO troca o dono")
check(e["bot"] is True, "retry do handoff mantem a Bia ligada")

# ============ 3. SEQUENCIA DO N8N: handoff + handoff + assign ============
print("3 — workflow n8n 'Handoff Humano -> Julia': handoff, handoff e assign deixam a Bia ligada")
c3 = nova_conversa("5511900070003", lead_id=7003)
post("/by-lead/7003/handoff")
post("/by-lead/7003/handoff")
e3 = estado(c3)
check(e3["bot"] is True and e3["atendente"] == 1, "pre-condicao: na fila com a Bia ligada e dono 1")
r = post(f"/{c3}/assign", json={"user_id": 2})
check(r.status_code == 200, f"assign responde 200 (got {r.status_code})")
e = estado(c3)
check(e["bot"] is True, "assign: a Bia continua ligada (atribuir nao e assumir)")
check(e["atendente"] == 2, "assign: dono trocado para o destino")
check(e["queued_at"] == e3["queued_at"], "assign: queued_at preservado (nao vai para o fim da fila)")
check(e["prh"] is None, "assign: primeira_resposta_humana_at continua NULL")
check(abas(c3) == {"fila", "todos"}, "assign: a conversa continua em fila e todos, nao em bia")

# ============ 4. RELEASE ============
print("4 — release devolve ao fim da fila SEM mexer na Bia")
c4a = nova_conversa("5511900070004")
post(f"/{c4a}/handoff")
q_antigo = estado(c4a)["queued_at"]
time.sleep(0.05)
r = post(f"/{c4a}/release")
check(r.status_code == 200, f"release (Bia ligada) responde 200 (got {r.status_code})")
e = estado(c4a)
check(e["bot"] is True, "release com a Bia ligada: continua ligada")
check(e["atendente"] is None, "release: atendente_id=NULL")
check(e["queued_at"] is not None and e["queued_at"] != q_antigo,
      "release: queued_at NOVO (volta para o fim da fila)")
check(e["prh"] is None, "release: primeira_resposta_humana_at=NULL")
check(abas(c4a) == {"fila", "todos"}, "release com a Bia ligada: a conversa fica em fila e todos")

c4b = nova_conversa("5511900070014")
post(f"/{c4b}/handoff")
r = post(f"/{c4b}/claim")
check(estado(c4b)["bot"] is False, "pre-condicao: o claim desligou a Bia")
r = post(f"/{c4b}/release")
check(r.status_code == 200, f"release (Bia desligada) responde 200 (got {r.status_code})")
e = estado(c4b)
check(e["bot"] is False, "release com a Bia desligada (apos /claim): continua desligada (nunca religa)")
check(e["atendente"] is None and e["queued_at"] is not None, "release: sem dono e na fila")
check(abas(c4b) == {"fila", "todos"}, "release com a Bia desligada: a conversa fica em fila e todos")

# ============ 5. PUT ============
print("5 — PUT: so trocar o atendente preserva a Bia; 'Desativar Bot' desliga; religar reseta")
c5 = nova_conversa("5511900070005")
post(f"/{c5}/handoff")
e5 = estado(c5)
check(e5["bot"] is True and e5["atendente"] == 1, "pre-condicao: na fila com a Bia ligada e dono 1")

r = put(c5, {"atendente_id": 2})
check(r.status_code == 200, f"PUT so com atendente_id responde 200 (got {r.status_code})")
e = estado(c5)
check(e["bot"] is True, "PUT so com atendente_id: o flag is_bot_active e preservado (continua ligado)")
check(e["atendente"] == 2, "PUT so com atendente_id: dono trocado")
check(e["queued_at"] == e5["queued_at"], "PUT so com atendente_id: queued_at preservado")
check(abas(c5) == {"fila", "todos"}, "PUT so com atendente_id: continua em fila e todos")

r = put(c5, {"is_bot_active": False})
check(r.status_code == 200, f"PUT is_bot_active=false responde 200 (got {r.status_code})")
e = estado(c5)
check(e["bot"] is False, "PUT is_bot_active=false ('Desativar Bot'): desliga a Bia")
check(e["atendente"] == 2, "PUT is_bot_active=false: preserva o dono")
check(e["queued_at"] == e5["queued_at"], "PUT is_bot_active=false: preserva queued_at")
check(abas(c5) == {"fila", "todos"}, "PUT is_bot_active=false: a conversa continua em fila e todos")

r = put(c5, {"is_bot_active": True})
check(r.status_code == 200, f"PUT is_bot_active=true responde 200 (got {r.status_code})")
e = estado(c5)
check(e["bot"] is True and e["atendente"] is None and e["queued_at"] is None and e["prh"] is None,
      "PUT is_bot_active=true (D3): religar devolve TUDO a Bia (sem dono, sem fila, sem 1a resposta)")
check(abas(c5) == {"bia"}, "religar: a conversa volta a aparecer so em bia")

# ============ 6. CLAIM ============
print("6 — claim desliga a Bia (inalterado)")
c6 = nova_conversa("5511900070006")
post(f"/{c6}/handoff")
e6 = estado(c6)
check(e6["bot"] is True, "pre-condicao: a Bia segue ligada apos o handoff")
r = post(f"/{c6}/claim")
check(r.status_code == 200, f"claim responde 200 (got {r.status_code})")
e = estado(c6)
check(e["bot"] is False, "claim: is_bot_active=False (assumir desliga a Bia)")
check(e["atendente"] == 1, "claim: atendente_id = usuario autenticado")
check(e["queued_at"] == e6["queued_at"], "claim: queued_at preservado")
check(abas(c6) == {"fila", "todos"}, "claim: a conversa continua em fila e todos")

# ============ 7. PRIMEIRA MENSAGEM HUMANA ============
print("7 — a primeira mensagem humana desliga a Bia e move a conversa da fila para meus")
_seq = {"n": 0}


async def _envio_ok(*a, **k):
    # whatsapp_msg_id e UNIQUE em messages: cada chamada devolve um wamid novo.
    _seq["n"] += 1
    return {"messages": [{"id": f"wamid.BIAFILA{_seq['n']}"}]}


_envio_original = whatsapp.send_text_message
whatsapp.send_text_message = _envio_ok
try:
    # janela de 24h aberta: o texto livre exige last_customer_msg_at recente
    c7 = nova_conversa("5511900070007",
                       last_customer_msg_at=dt.datetime.now(dt.timezone.utc))
    post(f"/{c7}/handoff")
    e7 = estado(c7)
    check(e7["bot"] is True and e7["prh"] is None and e7["atendente"] == 1,
          "pre-condicao: na fila com a Bia ligada, dono 1, ninguem respondeu")
    check(abas(c7) == {"fila", "todos"}, "pre-condicao: a conversa esta em fila e todos")

    r = post(f"/{c7}/messages", json={"content": "Oi, aqui e a Julia", "msg_type": "text"})
    check(r.status_code == 200, f"primeira mensagem humana responde 200 (got {r.status_code})")
    e = estado(c7)
    check(e["bot"] is False, "primeira mensagem humana: a Bia e desligada (a vendedora assumiu)")
    check(e["prh"] is not None, "primeira mensagem humana: primeira_resposta_humana_at preenchido")
    check(e["queued_at"] == e7["queued_at"], "primeira mensagem humana: queued_at preservado (HOTFIX-01)")
    check(e["atendente"] == 1, "primeira mensagem humana: o dono continua o mesmo")
    check(abas(c7) == {"meus", "todos"},
          f"primeira mensagem humana: sai de fila e entra em meus (got {sorted(abas(c7))})")
finally:
    whatsapp.send_text_message = _envio_original

# ============ 8. BIA JA DESLIGADA A MAO ============
print("8 — handoff, assign e release NUNCA religam uma Bia que a vendedora desligou")
c8 = nova_conversa("5511900070008")
post(f"/{c8}/handoff")
check(estado(c8)["bot"] is True, "pre-condicao: a Bia segue ligada apos o handoff")
put(c8, {"is_bot_active": False})
e8 = estado(c8)
check(e8["bot"] is False, "pre-condicao: a vendedora desligou a Bia ('Desativar Bot')")

post(f"/{c8}/handoff")
e = estado(c8)
check(e["bot"] is False, "handoff apos o desligamento manual: a Bia continua desligada")
check(e["queued_at"] == e8["queued_at"] and e["atendente"] == e8["atendente"],
      "handoff apos o desligamento manual: fila e dono intactos")
post(f"/{c8}/assign", json={"user_id": 2})
check(estado(c8)["bot"] is False, "assign apos o desligamento manual: a Bia continua desligada")
post(f"/{c8}/release")
check(estado(c8)["bot"] is False, "release apos o desligamento manual: a Bia continua desligada")

# ============ 9. TRIAGEM ============
print("9 — triagem (Bia ligada, sem queued_at) aparece SO em bia")
c9 = nova_conversa("5511900070009")
e = estado(c9)
check(e["bot"] is True and e["queued_at"] is None, "pre-condicao: Bia ligada e sem queued_at")
check(abas(c9) == {"bia"}, f"triagem aparece so em bia (got {sorted(abas(c9))})")

# ============ 10. CONTRATO DO PONTO UNICO ============
print("10 — contrato de aplicar_estado_humano: o padrao desliga, desligar_bia=False preserva")
conv = Conversation(lead_id=0, whatsapp="5511900070010", is_bot_active=True)
aplicar_estado_humano(conv, 2, keep_queue_position=True)
check(conv.is_bot_active is False,
      "padrao (desligar_bia=True): desliga a Bia — vendedora assumindo e a falha degradada do webhook")
check(conv.atendente_id == 2 and conv.queued_at is not None, "padrao: dono definido e conversa na fila")

conv = Conversation(lead_id=0, whatsapp="5511900070011", is_bot_active=True)
aplicar_estado_humano(conv, 2, keep_queue_position=True, desligar_bia=False)
check(conv.is_bot_active is True, "desligar_bia=False: preserva a Bia ligada")
check(conv.atendente_id == 2 and conv.queued_at is not None,
      "desligar_bia=False: dono e queued_at gravados normalmente")

conv = Conversation(lead_id=0, whatsapp="5511900070012", is_bot_active=False)
aplicar_estado_humano(conv, 2, keep_queue_position=True, desligar_bia=False)
check(conv.is_bot_active is False, "desligar_bia=False: NUNCA religa uma Bia desligada")

# ============ 11. INVARIANTE D4 ============
print("11 — bia e todos sao disjuntas e juntas cobrem toda conversa aberta")
_db = SessionLocal()
abertas = {c.id for c in _db.query(Conversation).filter(
    Conversation.status.in_(("aberta", "aguardando"))).all()}
_db.close()
ids_bia, ids_todos = ids_da_aba("bia"), ids_da_aba("todos")
check(ids_bia.isdisjoint(ids_todos), "nenhuma conversa aparece em bia E em todos")
check(ids_bia | ids_todos == abertas, "bia + todos cobrem todas as conversas abertas")

# ============ 12. 'DESATIVAR BOT' COM O DONO ATUAL DESATIVADO ============
print("12 — 'Desativar Bot' nao falha porque o dono atual foi desativado")
# O dono (usuario 3) foi desativado depois de receber a conversa. O botao so desliga a Bia: nao
# nomeia atendente nenhum, entao nao ha ninguem a validar.
c12 = nova_conversa("5511900070015", atendente_id=3, queued_at=dt.datetime.now(dt.timezone.utc))
e12 = estado(c12)
check(e12["bot"] is True and e12["atendente"] == 3 and e12["queued_at"] is not None,
      "pre-condicao: na fila com a Bia ligada e dono 3 (usuario inativo)")
r = put(c12, {"is_bot_active": False})
check(r.status_code == 200, f"PUT is_bot_active=false com dono inativo responde 200 (got {r.status_code})")
e = estado(c12)
check(e["bot"] is False, "dono inativo: a Bia foi desligada")
check(e["atendente"] == 3, "dono inativo: o dono continua o mesmo (nada foi reatribuido)")
check(e["queued_at"] == e12["queued_at"], "dono inativo: queued_at preservado")
check(abas(c12) == {"fila", "todos"}, f"dono inativo: a conversa continua em fila e todos (got {sorted(abas(c12))})")

# A validacao continua valendo quando o payload NOMEIA o atendente.
r = put(c12, {"atendente_id": 3})
check(r.status_code == 404, f"nomear um atendente inativo no payload -> 404 (got {r.status_code})")
r = put(c12, {"is_bot_active": False, "atendente_id": 3})
check(r.status_code == 404, f"desligar a Bia nomeando um atendente inativo -> 404 (got {r.status_code})")
check(estado(c12)["atendente"] == 3, "o 404 nao altera a conversa")
r = put(c12, {"atendente_id": 2})
check(r.status_code == 200 and estado(c12)["atendente"] == 2,
      f"nomear um atendente ativo continua funcionando (got {r.status_code})")

# JS do botao (guard estatico, no estilo da suite): antes, o toast de sucesso saia mesmo com 404/500.
js = (CONVERSAS_DIR / "static" / "js" / "conversas.js").read_text(encoding="utf-8")
b_ini = js.find("document.getElementById('btnToggleBot').addEventListener(")
b_fim = js.find("// Mobile back", b_ini)
botao = js[b_ini:b_fim]
check(b_ini != -1 and b_fim > b_ini, "handler do botao 'Desativar Bot' localizado no conversas.js")
check("const resp = await Auth.apiRequest(" in botao and "resp && resp.ok" in botao,
      "botao: le a resposta do PUT e testa resp.ok")
check(0 <= botao.find("resp && resp.ok") < botao.find("showToast(newValue ? 'Bot ativado' : 'Bot desativado')"),
      "botao: o toast de sucesso so aparece DEPOIS de checar resp.ok")
check(0 <= botao.find("resp && resp.ok") < botao.find("loadChat("),
      "botao: a conversa so e recarregada (UI atualizada) no sucesso")
check("else {" in botao and "e.detail" in botao and botao.count("showToast(") == 2,
      "botao: no erro mostra o detail do backend (um toast de sucesso e um de erro)")

main.app.dependency_overrides.clear()

print()
if failures:
    print(f"FALHOU ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("TODOS OS CHECKS PASSARAM")
