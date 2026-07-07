"""
CONV-HOTFIX-POSTDEPLOY-01 — filtros do Conversas + responsavel do lead (CRM).

Dois bugs de producao pos-deploy do merge fdb6859:
  BUG 1 (filtros/fila): o codigo ANTIGO aceitava qualquer valor no PUT de
    status, entao podem existir linhas LEGADAS com status='aguardando'
    persistido — elas nao batiam em NENHUMA aba nem em ?queue=fila.
  BUG 2 (responsavel): responsavel_id/nome da conversa sao snapshot gravado
    SO no auto-link/PUT; o list/detail nunca consultavam o lead no CRM ->
    tudo exibia 'Agente IA'.

O hotfix usa o padrao SQL-direto na base COMPARTILHADA (crm.py). Este teste
cria a tabela CRM-shaped 'leads' NO MESMO sqlite (users ja existe via
app.auth.User) e prova o comportamento REAL, sem mock.

Prova que:
  0. DEV ISOLADO (sem tabela leads): list e detail nao quebram, cache exibido.
  1. Lead com responsavel "X" + conversa vinculada -> list E detail exibem
     responsavel_nome="X" (nunca 'Agente IA'), com read-repair do cache.
  2. Lead SEM responsavel -> responsavel_nome None (frontend exibe 'Agente IA').
  3. Conversa SEM lead com atribuicao local -> exibe a atribuicao local
     (enriquecimento nao toca conversas nao vinculadas).
  4. Mudanca do responsavel no CRM reflete na proxima carga (read-repair
     persistido no cache, SEM bumpar updated_at — ordenacao preservada).
  5. Linha LEGADA status='aguardando' inserida direto -> aparece em
     ?queue=fila, na aba Abertas (status=aberta) e pode ser assumida (claim).
  6. abertas/encerradas/todas e ?queue=em_atendimento intactos (regressao).
  7. Filtro por responsavel_id volta a ser util apos o read-repair.
  8. Nenhum segredo nas respostas.

Roda standalone:  python tests/test_conversas_hotfix_filters_resp.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_hotfix_filters_resp_test.db"
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
from sqlalchemy import text  # noqa: E402

import app.main as main  # noqa: E402
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


class _DummyUser:
    id = 1
    email = "tester@local"
    is_admin = True


main.app.dependency_overrides[get_current_user] = lambda: _DummyUser()
client = TestClient(main.app)


def db_exec(sql, params=None):
    s = SessionLocal()
    try:
        result = s.execute(text(sql), params or {})
        s.commit()
        try:
            return result.fetchall()
        except Exception:
            return None
    finally:
        s.close()


# usuarios na tabela COMPARTILHADA users (ja criada via app.auth.User)
s = SessionLocal()
s.add_all([
    User(id=1, nome="Tester Local", email="tester@local", hashed_password="x", is_active=True),
    User(id=7, nome="Maria Vendas", email="maria@local", hashed_password="x", is_active=True),
    User(id=8, nome="Joao Backup", email="joao@local", hashed_password="x", is_active=True),
    User(id=9, nome="Ex Funcionario", email="ex@local", hashed_password="x", is_active=False),
])

LEAD_COM_RESP = 101   # responsavel = Maria (7)
LEAD_SEM_RESP = 102   # sem responsavel
LEAD_USER_INATIVO = 103  # responsavel inativo (9)

conv_com_resp = Conversation(lead_id=LEAD_COM_RESP, whatsapp="5511900088801", nome="Com Resp", status="aberta")
conv_sem_resp = Conversation(lead_id=LEAD_SEM_RESP, whatsapp="5511900088802", nome="Sem Resp", status="aberta")
conv_free = Conversation(lead_id=0, whatsapp="5511900088803", nome="Sem Lead", status="aberta")
conv_fechada = Conversation(lead_id=0, whatsapp="5511900088804", nome="Fechada", status="encerrada")
conv_inativo = Conversation(lead_id=LEAD_USER_INATIVO, whatsapp="5511900088805", nome="Resp Inativo", status="aberta")
s.add_all([conv_com_resp, conv_sem_resp, conv_free, conv_fechada, conv_inativo])
s.commit()
for c in (conv_com_resp, conv_sem_resp, conv_free, conv_fechada, conv_inativo):
    s.refresh(c)
CID_COM, CID_SEM, CID_FREE, CID_FECH, CID_INAT = (
    conv_com_resp.id, conv_sem_resp.id, conv_free.id, conv_fechada.id, conv_inativo.id
)
s.close()


def list_convs(qs=""):
    r = client.get(f"/api/conversations{qs}")
    assert r.status_code == 200, f"GET /api/conversations{qs} -> {r.status_code}"
    return r.json()


def by_id(data, cid):
    return next((c for c in data["conversations"] if c["id"] == cid), None)


# ============ 0. DEV ISOLADO (SEM tabela leads) ============
print("HOTFIX — dev isolado (tabela leads ausente) nao quebra")
r0 = client.get("/api/conversations")
check(r0.status_code == 200, "list sem CRM -> 200 (segue com o cache)")
r0d = client.get(f"/api/conversations/{CID_COM}")
check(r0d.status_code == 200, "detail sem CRM -> 200 (segue com o cache)")
check(r0d.json()["responsavel_nome"] is None, "cache NULL exibido como esta (frontend mostra 'Agente IA')")

# ============ cria a tabela CRM-shaped no MESMO banco ============
db_exec("CREATE TABLE leads (id INTEGER PRIMARY KEY, nome VARCHAR(200), "
        "whatsapp VARCHAR(30), email VARCHAR(255), responsavel_id INTEGER, "
        "created_at TIMESTAMP)")
db_exec(f"INSERT INTO leads (id, nome, whatsapp, responsavel_id) "
        f"VALUES ({LEAD_COM_RESP}, 'Lead Com Resp', '5511900088801', 7)")
db_exec(f"INSERT INTO leads (id, nome, whatsapp, responsavel_id) "
        f"VALUES ({LEAD_SEM_RESP}, 'Lead Sem Resp', '5511900088802', NULL)")
db_exec(f"INSERT INTO leads (id, nome, whatsapp, responsavel_id) "
        f"VALUES ({LEAD_USER_INATIVO}, 'Lead Resp Inativo', '5511900088805', 9)")

# ============ 1. RESPONSAVEL DO LEAD NO LIST E NO DETAIL ============
print("\nHOTFIX — conversa vinculada exibe o responsavel do lead (CRM)")
d1 = list_convs()
check(by_id(d1, CID_COM)["responsavel_nome"] == "Maria Vendas",
      "list: conversa vinculada exibe responsavel do lead ('Maria Vendas')")
check(by_id(d1, CID_COM)["responsavel_id"] == 7, "list: responsavel_id espelhado do lead")
r1d = client.get(f"/api/conversations/{CID_COM}")
check(r1d.json()["responsavel_nome"] == "Maria Vendas", "detail: idem")
rows = db_exec(f"SELECT responsavel_id, responsavel_nome FROM conversations WHERE id = {CID_COM}")
check(rows[0].responsavel_id == 7 and rows[0].responsavel_nome == "Maria Vendas",
      "cache da conversa REPARADO no banco (read-repair persistido)")

# ============ 2. LEAD SEM RESPONSAVEL ============
print("\nHOTFIX — lead sem responsavel")
check(by_id(d1, CID_SEM)["responsavel_nome"] is None,
      "lead sem responsavel -> responsavel_nome None (frontend exibe 'Agente IA')")
check(by_id(d1, CID_INAT)["responsavel_nome"] is None,
      "responsavel INATIVO no CRM -> sem nome (nao exibe usuario desativado)")

# ============ 3. CONVERSA SEM LEAD = ATRIBUICAO LOCAL ============
print("\nHOTFIX — conversa sem lead preserva atribuicao local")
r3 = client.put(f"/api/conversations/{CID_FREE}/responsavel?responsavel_id=8")
check(r3.status_code == 200, "PUT responsavel local em conversa sem lead -> 200")
d3 = list_convs()
check(by_id(d3, CID_FREE)["responsavel_nome"] == "Joao Backup",
      "conversa SEM lead exibe a atribuicao local (enriquecimento nao toca)")

# ============ 4. MUDANCA NO CRM REFLETE (READ-REPAIR) ============
print("\nHOTFIX — mudanca do responsavel no CRM reflete na proxima carga")
upd_before = db_exec(f"SELECT updated_at FROM conversations WHERE id = {CID_COM}")[0].updated_at
db_exec(f"UPDATE leads SET responsavel_id = 8 WHERE id = {LEAD_COM_RESP}")
d4 = list_convs()
check(by_id(d4, CID_COM)["responsavel_nome"] == "Joao Backup",
      "list reflete o novo responsavel do CRM ('Joao Backup')")
rows4 = db_exec(f"SELECT responsavel_nome, updated_at FROM conversations WHERE id = {CID_COM}")
check(rows4[0].responsavel_nome == "Joao Backup", "cache re-reparado no banco")
check(rows4[0].updated_at == upd_before,
      "read-repair NAO bumpa updated_at (ordenacao da lista preservada)")

# ============ 5. LINHA LEGADA status='aguardando' ============
print("\nHOTFIX — linha legada status='aguardando' (codigo antigo sem whitelist)")
db_exec("INSERT INTO conversations (lead_id, whatsapp, nome, status, unread_count, "
        "is_bot_active, created_at, updated_at) VALUES (0, '5511900088806', 'Legada', "
        "'aguardando', 0, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
CID_LEG = db_exec("SELECT id FROM conversations WHERE nome = 'Legada'")[0].id

fila = list_convs("?queue=fila")
check(by_id(fila, CID_LEG) is not None, "legada aparece em ?queue=fila")
abertas = list_convs("?status=aberta")
check(by_id(abertas, CID_LEG) is not None, "legada aparece no filtro status=aberta (aba Abertas)")
todas = list_convs()
check(by_id(todas, CID_LEG) is not None, "legada aparece em Todas")
r5c = client.post(f"/api/conversations/{CID_LEG}/claim")
check(r5c.status_code == 200, "legada pode ser ASSUMIDA (claim nao responde 409)")
fila5 = list_convs("?queue=fila")
check(by_id(fila5, CID_LEG) is None, "apos claim, legada sai da fila")
em_at5 = list_convs("?queue=em_atendimento")
check(by_id(em_at5, CID_LEG) is not None, "apos claim, legada conta como em_atendimento")

# ============ 6. REGRESSAO DOS DEMAIS FILTROS ============
print("\nHOTFIX — regressao: abertas/encerradas/todas/em_atendimento")
abertas6 = list_convs("?status=aberta")
check(by_id(abertas6, CID_FECH) is None, "status=aberta NAO inclui encerrada")
encerradas = list_convs("?status=encerrada")
check(by_id(encerradas, CID_FECH) is not None and encerradas["total"] == 1,
      "status=encerrada retorna apenas a encerrada")
todas6 = list_convs()
check(todas6["total"] == 6, f"todas retorna 6 conversas (got {todas6['total']})")
check(client.get("/api/conversations?queue=banana").status_code == 422, "queue invalida -> 422")
check(client.put(f"/api/conversations/{CID_COM}", json={"status": "aguardando"}).status_code == 422,
      "PUT status='aguardando' segue REJEITADO (whitelist CONV-06 intacta)")

# ============ 7. FILTRO POR RESPONSAVEL VOLTA A SER UTIL ============
print("\nHOTFIX — filtro por responsavel_id apos read-repair")
d7 = list_convs("?responsavel_id=8")
ids7 = {c["id"] for c in d7["conversations"]}
check(CID_COM in ids7 and CID_FREE in ids7,
      "responsavel_id=8 encontra a vinculada reparada E a atribuicao local")

# ============ 8. SEM SEGREDO NAS RESPOSTAS ============
print("\nHOTFIX — nenhum segredo nas respostas")
body = client.get("/api/conversations").text + client.get(f"/api/conversations/{CID_COM}").text
check("test-secret-key" not in body and "hashed_password" not in body,
      "respostas sem SECRET_KEY e sem hash de senha")

# --- Resultado ---
main.app.dependency_overrides.clear()
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DO HOTFIX PASSARAM")
