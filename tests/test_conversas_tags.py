"""
CONV-05 — Tags do Conversas.

Prova que:
  1. CRUD: criar, listar, editar, deletar (delete limpa links).
  2. Duplicata de nome -> 409; cor invalida -> 422 (validacao server-side —
     cor vai para style attr no frontend).
  3. Aplicar/remover tag em conversa; aplicar 2x e idempotente (PK composta).
  4. Filtro GET /api/conversations?tag_id= retorna so as marcadas.
  5. Tags aparecem no ConversationResponse (lista e detail).
  6. Sem autenticacao -> 401.
  7. Frontend (grep): nome via escapeHtml/textContent; cor revalidada
     (safeTagColor) antes do style; ids numericos coercidos.
  8. Migration m005 idempotente em banco antigo.

Roda standalone:  python tests/test_conversas_tags.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_tags_test.db"
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

s = SessionLocal()
c1 = Conversation(lead_id=1, whatsapp="5511900066661", nome="Conv Tag A", status="aberta")
c2 = Conversation(lead_id=2, whatsapp="5511900066662", nome="Conv Tag B", status="aberta")
s.add_all([c1, c2])
s.commit()
s.refresh(c1)
s.refresh(c2)
CID1, CID2 = c1.id, c2.id
s.close()


# ============ 1/2. CRUD + VALIDACOES ============
print("CONV-05 — CRUD e validacoes")
r = client.post("/api/tags", json={"nome": "Urgente", "cor": "#EF4444"})
check(r.status_code == 201, f"criar tag 201 (got {r.status_code})")
TAG1 = r.json()["id"]
r2 = client.post("/api/tags", json={"nome": "Orcamento", "cor": "#22C55E"})
TAG2 = r2.json()["id"]

check(client.post("/api/tags", json={"nome": "Urgente", "cor": "#000000"}).status_code == 409,
      "nome duplicado -> 409")
check(client.post("/api/tags", json={"nome": "Hack", "cor": "red;}</style><script>"}).status_code == 422,
      "cor invalida -> 422 (nunca chega ao style)")
check(client.post("/api/tags", json={"nome": "Hack2", "cor": "#GGGGGG"}).status_code == 422,
      "hex invalido -> 422")

lst = client.get("/api/tags").json()
check(len(lst) == 2 and lst[0]["nome"] in ("Orcamento", "Urgente"), "listagem com 2 tags")

r_up = client.put(f"/api/tags/{TAG1}", json={"nome": "Urgentissimo", "cor": "#DC2626"})
check(r_up.status_code == 200 and r_up.json()["nome"] == "Urgentissimo", "editar tag")
check(client.put(f"/api/tags/{TAG1}", json={"nome": "Orcamento", "cor": "#111111"}).status_code == 409,
      "editar para nome de outra -> 409")


# ============ 3. APLICAR/REMOVER ============
print("\nCONV-05 — aplicar/remover em conversa")
r3 = client.post(f"/api/conversations/{CID1}/tags/{TAG1}")
check(r3.status_code == 200 and len(r3.json()) == 1, "aplicar tag")
r3b = client.post(f"/api/conversations/{CID1}/tags/{TAG1}")
check(r3b.status_code == 200 and len(r3b.json()) == 1, "aplicar 2x = idempotente (sem duplicar)")
client.post(f"/api/conversations/{CID1}/tags/{TAG2}")
check(len(client.get(f"/api/conversations/{CID1}").json()["tags"]) == 2,
      "detail expoe as 2 tags")
r3c = client.delete(f"/api/conversations/{CID1}/tags/{TAG2}")
check(r3c.status_code == 200 and len(r3c.json()) == 1, "remover tag")
check(client.post(f"/api/conversations/{CID1}/tags/99999").status_code == 404, "tag inexistente 404")
check(client.post(f"/api/conversations/99999/tags/{TAG1}").status_code == 404, "conversa inexistente 404")


# ============ 4. FILTRO ============
print("\nCONV-05 — filtro por tag na listagem")
data = client.get(f"/api/conversations?tag_id={TAG1}").json()
check(data["total"] == 1 and data["conversations"][0]["id"] == CID1,
      "filtro retorna so a conversa marcada")
check(data["conversations"][0]["tags"][0]["nome"] == "Urgentissimo",
      "tags presentes na resposta da lista")
data2 = client.get(f"/api/conversations?tag_id={TAG2}").json()
check(data2["total"] == 0, "tag sem conversas -> lista vazia")


# ============ 1b. DELETE LIMPA LINKS ============
print("\nCONV-05 — delete de tag limpa links")
check(client.delete(f"/api/tags/{TAG1}").status_code == 204, "delete 204")
check(len(client.get(f"/api/conversations/{CID1}").json()["tags"]) == 0,
      "conversa ficou sem a tag deletada")


# ============ 6. AUTH ============
print("\nCONV-05 — autenticacao obrigatoria")
main.app.dependency_overrides.clear()
check(client.get("/api/tags").status_code == 401, "GET /api/tags sem auth -> 401")
check(client.post(f"/api/conversations/{CID1}/tags/{TAG2}").status_code == 401,
      "aplicar sem auth -> 401")
main.app.dependency_overrides[get_current_user] = lambda: _DummyUser()


# ============ 7. FRONTEND ESTATICO ============
print("\nCONV-05 — frontend XSS-safe (grep)")
js = (CONVERSAS_DIR / "static" / "js" / "conversas.js").read_text(encoding="utf-8")
check("escapeHtml(t.nome)" in js, "nome da tag via escapeHtml no chip")
check("safeTagColor" in js and "/^#[0-9A-Fa-f]{6}$/" in js, "cor revalidada no cliente antes do style")
check("o1.textContent = t.nome" in js, "options de tag via textContent")
check("window._removeTag(${Number(t.id)})" in js, "remocao usa id numerico coercido")


# ============ 8. m005 IDEMPOTENTE ============
print("\nm005 — migration idempotente em banco antigo")
import importlib.util  # noqa: E402
from sqlalchemy import create_engine as _ce, inspect as _inspect, text as _text  # noqa: E402

OLD_DB = SCRATCH / "conv_m005_old.db"
if OLD_DB.exists():
    OLD_DB.unlink()
old_engine = _ce(f"sqlite:///{OLD_DB.as_posix()}")
with old_engine.begin() as conn:  # banco antigo: tem conversations, nao tem tags
    conn.execute(_text("CREATE TABLE conversations (id INTEGER PRIMARY KEY, lead_id INTEGER)"))

spec = importlib.util.spec_from_file_location("m005", ROOT / "migrations" / "m005_conversas_tags.py")
m005 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m005)
acts1 = m005.run(old_engine)
check(sorted(acts1) == ["conversation_tag_links:created", "conversation_tags:created"],
      f"1a execucao cria as 2 tabelas ({acts1})")
acts2 = m005.run(old_engine)
check(all("already-present" in a for a in acts2), f"2a execucao e no-op ({acts2})")

# --- Resultado ---
main.app.dependency_overrides.clear()
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE TAGS PASSARAM")
