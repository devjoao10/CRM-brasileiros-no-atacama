"""
CONV-TAGS-SYNC-01 — Sync de tags do lead (CRM) <-> Conversas.

O sync usa o padrao SQL-direto na base COMPARTILHADA (mesmo padrao do
crm.py). Este teste cria as tabelas do CRM (tags/lead_tags) NO MESMO
sqlite e prova o sync REAL, sem mock.

Prova que:
  0. DEV ISOLADO (antes de existirem as tabelas CRM): abrir conversa e
     aplicar tag funcionam 100% local, sem excecao.
  1. CRM -> Conversas: abrir conversa vinculada espelha as tags do lead
     (tag local criada por NOME, cor copiada).
  2. Espelho EXATO: tag removida do lead no CRM some da conversa no
     proximo GET; tag nova no CRM aparece.
  3. Conversas -> CRM: aplicar tag em conversa vinculada grava em
     lead_tags (criando a tag no CRM por nome se faltar); remover apaga o
     vinculo. Idempotente.
  4. Conversa SEM lead: aplicar/remover NAO toca as tabelas do CRM.
  5. Cor invalida vinda do CRM e SANITIZADA (vai para style attr).
  6. Guard do CONV-BF-UI-02: .conv-filters com flex-wrap (abas visiveis).

Roda standalone:  python tests/test_conversas_tags_sync.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_tags_sync_test.db"
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
from app.auth import get_current_user  # noqa: E402
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

LEAD_ID = 42
s = SessionLocal()
conv_linked = Conversation(lead_id=LEAD_ID, whatsapp="5511900099991", nome="Lead Vinculado", status="aberta")
conv_free = Conversation(lead_id=0, whatsapp="5511900099992", nome="Sem Lead", status="aberta")
s.add_all([conv_linked, conv_free])
s.commit()
s.refresh(conv_linked)
s.refresh(conv_free)
CID_L, CID_F = conv_linked.id, conv_free.id
s.close()


def crm_exec(sql, params=None):
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


# ============ 0. DEV ISOLADO (SEM tabelas do CRM) ============
print("SYNC — dev isolado (tabelas CRM ausentes) nao quebra")
r0 = client.get(f"/api/conversations/{CID_L}")
check(r0.status_code == 200, "abrir conversa vinculada sem CRM -> 200 (espelho no-op)")
r0b = client.post("/api/tags", json={"nome": "LocalOnly", "cor": "#111111"})
TAG_LOCAL = r0b.json()["id"]
r0c = client.post(f"/api/conversations/{CID_L}/tags/{TAG_LOCAL}")
check(r0c.status_code == 200 and len(r0c.json()) == 1,
      "aplicar tag sem CRM funciona local (replicacao so loga)")

# ============ cria as tabelas do CRM no MESMO banco ============
crm_exec("CREATE TABLE tags (id INTEGER PRIMARY KEY, nome VARCHAR(100) UNIQUE NOT NULL, "
         "cor VARCHAR(7) NOT NULL DEFAULT '#2B6CB0', created_at TIMESTAMP)")
crm_exec("CREATE TABLE lead_tags (lead_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, "
         "PRIMARY KEY (lead_id, tag_id))")

# lead 42 tem a tag VIP no CRM (cor valida) e a tag Sujo (cor INVALIDA)
crm_exec("INSERT INTO tags (nome, cor, created_at) VALUES ('VIP', '#EF4444', CURRENT_TIMESTAMP)")
crm_exec("INSERT INTO tags (nome, cor, created_at) VALUES ('Sujo', 'red;}</style>', CURRENT_TIMESTAMP)")
crm_exec(f"INSERT INTO lead_tags (lead_id, tag_id) VALUES ({LEAD_ID}, 1)")
crm_exec(f"INSERT INTO lead_tags (lead_id, tag_id) VALUES ({LEAD_ID}, 2)")


# ============ 1. CRM -> CONVERSAS (espelho ao abrir) ============
print("\nSYNC — CRM -> Conversas ao abrir a conversa")
r1 = client.get(f"/api/conversations/{CID_L}")
tags1 = {t["nome"]: t["cor"] for t in r1.json()["tags"]}
check("VIP" in tags1, "tag do lead no CRM aparece na conversa")
check(tags1.get("VIP") == "#EF4444", "cor copiada do CRM")
check("Sujo" in tags1 and tags1["Sujo"] == "#3B82F6",
      "cor INVALIDA do CRM sanitizada para default (nunca chega ao style)")
check("LocalOnly" not in tags1,
      "espelho EXATO: tag local previa (nao presente no lead) foi substituida pelo conjunto do CRM")

# idempotencia do espelho
r1b = client.get(f"/api/conversations/{CID_L}")
check(len(r1b.json()["tags"]) == 2, "2o GET nao duplica tags")


# ============ 2. ESPELHO EXATO (mudancas no CRM refletem) ============
print("\nSYNC — mudancas no CRM refletem no proximo GET")
crm_exec(f"DELETE FROM lead_tags WHERE lead_id = {LEAD_ID} AND tag_id = 2")  # remove Sujo
crm_exec("INSERT INTO tags (nome, cor, created_at) VALUES ('Novo', '#22C55E', CURRENT_TIMESTAMP)")
crm_exec(f"INSERT INTO lead_tags (lead_id, tag_id) VALUES ({LEAD_ID}, 3)")
r2 = client.get(f"/api/conversations/{CID_L}")
nomes2 = {t["nome"] for t in r2.json()["tags"]}
check(nomes2 == {"VIP", "Novo"}, f"conjunto espelhado exato (got {nomes2})")


# ============ 3. CONVERSAS -> CRM (aplicar/remover replica) ============
print("\nSYNC — Conversas -> CRM")
r3 = client.post("/api/tags", json={"nome": "Orcamento", "cor": "#F59E0B"})
TAG_ORC = r3.json()["id"]
client.post(f"/api/conversations/{CID_L}/tags/{TAG_ORC}")
rows = crm_exec(
    "SELECT t.nome FROM tags t JOIN lead_tags lt ON lt.tag_id = t.id "
    f"WHERE lt.lead_id = {LEAD_ID} AND t.nome = 'Orcamento'")
check(len(rows) == 1, "aplicar no Conversas criou a tag no CRM por nome e vinculou ao lead")

# aplicar 2x = idempotente nos dois lados
client.post(f"/api/conversations/{CID_L}/tags/{TAG_ORC}")
rows_dup = crm_exec(f"SELECT COUNT(*) AS c FROM lead_tags WHERE lead_id = {LEAD_ID}")
check(rows_dup[0].c == 3, "aplicar 2x nao duplica vinculo no CRM")

# remover replica
client.delete(f"/api/conversations/{CID_L}/tags/{TAG_ORC}")
rows_after = crm_exec(
    "SELECT t.nome FROM tags t JOIN lead_tags lt ON lt.tag_id = t.id "
    f"WHERE lt.lead_id = {LEAD_ID} AND t.nome = 'Orcamento'")
check(len(rows_after) == 0, "remover no Conversas removeu o vinculo no CRM")


# ============ 4. CONVERSA SEM LEAD = LOCAL PURO ============
print("\nSYNC — conversa sem lead nao toca o CRM")
crm_count_before = crm_exec("SELECT COUNT(*) AS c FROM lead_tags")[0].c
client.post(f"/api/conversations/{CID_F}/tags/{TAG_ORC}")
r4 = client.get(f"/api/conversations/{CID_F}")
check(any(t["nome"] == "Orcamento" for t in r4.json()["tags"]),
      "tag aplicada localmente na conversa sem lead")
crm_count_after = crm_exec("SELECT COUNT(*) AS c FROM lead_tags")[0].c
check(crm_count_after == crm_count_before, "lead_tags do CRM INTOCADO para conversa sem lead")


# ============ 6. GUARD DO BUGFIX DE UI ============
print("\nCONV-BF-UI-03 — guard estatico das abas (linha rolavel)")
css = (CONVERSAS_DIR / "static" / "css" / "conversas.css").read_text(encoding="utf-8")
js_ui = (CONVERSAS_DIR / "static" / "js" / "conversas.js").read_text(encoding="utf-8")
check("flex-wrap: nowrap" in css and "overflow-x: auto" in css,
      ".conv-filters em UMA linha com overflow-x (abas alcancaveis por scroll)")
check("flex: 0 0 auto" in css, "abas nao encolhem (flex 0 0 auto)")
check("initTabsDragScroll" in js_ui and "dragged" in js_ui,
      "drag-to-scroll escopado presente (arrasto nao dispara troca de aba)")

# --- Resultado ---
main.app.dependency_overrides.clear()
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE TAG SYNC PASSARAM")
