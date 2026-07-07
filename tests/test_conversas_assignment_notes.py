"""
CONV-07 — Atribuicao dirigida + notas internas do Conversas.

Prova que:
  1. Assign dirigido atribui a QUALQUER usuario ativo (handoff/reassign ok);
     usuario inexistente/inativo -> 404; encerrada -> 409.
  2. Filtro ?atendente_id= (0 = sem atendente) funciona.
  3. Nota interna: criada com autor do TOKEN (nunca do request), listada,
     conteudo/autor exibidos escapados (grep).
  4. INVARIANTE: criar/listar notas dispara ZERO chamadas ao provider
     WhatsApp (contador em todas as funcoes send_*).
  5. Delete: so o autor (403 para outro usuario); 404 inexistente.
  6. 401 sem auth.
  7. m006 idempotente em banco antigo.

Roda standalone:  python tests/test_conversas_assignment_notes.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_assign_notes_test.db"
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
from app.auth import get_current_user, User  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.services import whatsapp  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


Base.metadata.create_all(bind=engine)

# usuarios REAIS no banco (assign valida existencia)
s = SessionLocal()
u1 = User(nome="Atendente Um", email="a1@local", hashed_password="x", is_active=True)
u2 = User(nome="Atendente Dois", email="a2@local", hashed_password="x", is_active=True)
u3 = User(nome="Inativo", email="a3@local", hashed_password="x", is_active=False)
s.add_all([u1, u2, u3])
s.commit()
s.refresh(u1)
s.refresh(u2)
s.refresh(u3)
U1, U2, U3 = u1.id, u2.id, u3.id
conv = Conversation(lead_id=1, whatsapp="5511900088888", nome="Cliente Equipe", status="aberta")
conv2 = Conversation(lead_id=2, whatsapp="5511900088889", nome="Cliente Dois", status="aberta")
s.add_all([conv, conv2])
s.commit()
s.refresh(conv)
s.refresh(conv2)
CID, CID2 = conv.id, conv2.id
s.close()


class _FakeUser:
    def __init__(self, uid, nome):
        self.id = uid
        self.nome = nome
        self.email = f"{nome}@local"
        self.is_admin = False


def as_user(uid, nome):
    main.app.dependency_overrides[get_current_user] = lambda: _FakeUser(uid, nome)


as_user(U1, "Atendente Um")
client = TestClient(main.app)

# INVARIANTE: contador de chamadas ao provider
provider_calls = {"n": 0}


def _count(*a, **k):
    provider_calls["n"] += 1

    async def _r():
        return None
    return _r()


for fn in ("send_text_message", "send_media_message", "send_template_message",
           "upload_media", "mark_as_read"):
    setattr(whatsapp, fn, _count)


# ============ 1. ASSIGN DIRIGIDO ============
print("CONV-07 — atribuicao dirigida (handoff)")
r = client.post(f"/api/conversations/{CID}/assign", json={"user_id": U2})
check(r.status_code == 200 and r.json()["atendente_id"] == U2, "assign para outro usuario")
r2 = client.post(f"/api/conversations/{CID}/assign", json={"user_id": U1})
check(r2.status_code == 200 and r2.json()["atendente_id"] == U1, "reassign (handoff) permitido")
check(client.post(f"/api/conversations/{CID}/assign", json={"user_id": 99999}).status_code == 404,
      "usuario inexistente -> 404")
check(client.post(f"/api/conversations/{CID}/assign", json={"user_id": U3}).status_code == 404,
      "usuario INATIVO -> 404")
client.put(f"/api/conversations/{CID2}", json={"status": "encerrada"})
check(client.post(f"/api/conversations/{CID2}/assign", json={"user_id": U1}).status_code == 409,
      "assign em encerrada -> 409")
client.put(f"/api/conversations/{CID2}", json={"status": "aberta"})


# ============ 2. FILTRO ============
print("\nCONV-07 — filtro por atendente")
data = client.get(f"/api/conversations?atendente_id={U1}").json()
check(data["total"] == 1 and data["conversations"][0]["id"] == CID, "filtro por atendente")
data0 = client.get("/api/conversations?atendente_id=0").json()
check(data0["total"] == 1 and data0["conversations"][0]["id"] == CID2, "atendente_id=0 = sem atendente")


# ============ 3/4. NOTAS + INVARIANTE ============
print("\nCONV-07 — notas internas (nunca vao ao WhatsApp)")
calls_before = provider_calls["n"]
rn = client.post(f"/api/conversations/{CID}/notes",
                 json={"content": "Cliente pediu <b>desconto</b>; combinar com gerencia."})
check(rn.status_code == 201, f"criar nota 201 (got {rn.status_code})")
note = rn.json()
check(note["user_id"] == U1 and note["user_nome"] == "Atendente Um",
      "autor vem do TOKEN (id+nome snapshot)")
lst = client.get(f"/api/conversations/{CID}/notes").json()
check(len(lst) == 1 and "<b>desconto</b>" in lst[0]["content"],
      "nota listada (conteudo bruto no banco; escape no render)")
check(provider_calls["n"] == calls_before,
      "ZERO chamadas ao provider WhatsApp ao criar/listar notas")
s = SessionLocal()
check(s.query(Message).filter(Message.conversation_id == CID).count() == 0,
      "nota NAO cria Message (nao entra no fluxo outbound)")
s.close()


# ============ 5. DELETE (SO AUTOR) ============
print("\nCONV-07 — delete restrito ao autor")
NOTE_ID = note["id"]
as_user(U2, "Atendente Dois")
check(client.delete(f"/api/conversations/{CID}/notes/{NOTE_ID}").status_code == 403,
      "outro usuario -> 403")
as_user(U1, "Atendente Um")
check(client.delete(f"/api/conversations/{CID}/notes/{NOTE_ID}").status_code == 204,
      "autor deleta -> 204")
check(client.delete(f"/api/conversations/{CID}/notes/{NOTE_ID}").status_code == 404,
      "nota ja removida -> 404")


# ============ 6. AUTH ============
print("\nCONV-07 — auth")
main.app.dependency_overrides.clear()
check(client.get(f"/api/conversations/{CID}/notes").status_code == 401, "notas sem auth -> 401")
check(client.post(f"/api/conversations/{CID}/assign", json={"user_id": U1}).status_code == 401,
      "assign sem auth -> 401")
as_user(U1, "Atendente Um")


# ============ FRONTEND (grep) ============
print("\nCONV-07 — frontend (grep)")
js = (CONVERSAS_DIR / "static" / "js" / "conversas.js").read_text(encoding="utf-8")
html = (CONVERSAS_DIR / "templates" / "conversas.html").read_text(encoding="utf-8")
check("escapeHtml(n.content)" in js and "escapeHtml(n.user_nome" in js,
      "nota renderizada com conteudo E autor escapados")
check("window._deleteNote(${Number(n.id)})" in js, "delete usa id numerico coercido")
check("c.atendente_id === me.id" in js, "aba Minhas filtra pelo usuario logado")
check('id="selectAtendente"' in html and 'id="btnAddNote"' in html,
      "select de atendente + notas no painel")
check("NUNCA são enviadas ao WhatsApp" in html, "aviso explicito na UI")


# ============ 7. m006 ============
print("\nm006 — migration idempotente")
import importlib.util  # noqa: E402
from sqlalchemy import create_engine as _ce, text as _text  # noqa: E402

OLD_DB = SCRATCH / "conv_m006_old.db"
if OLD_DB.exists():
    OLD_DB.unlink()
old_engine = _ce(f"sqlite:///{OLD_DB.as_posix()}")
with old_engine.begin() as conn:
    conn.execute(_text("CREATE TABLE conversations (id INTEGER PRIMARY KEY, lead_id INTEGER)"))
spec = importlib.util.spec_from_file_location("m006", ROOT / "migrations" / "m006_conversas_notes.py")
m006 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m006)
check(m006.run(old_engine) == ["conversation_notes:created"], "1a execucao cria")
check(m006.run(old_engine) == ["conversation_notes:already-present"], "2a execucao no-op")

# --- Resultado ---
main.app.dependency_overrides.clear()
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE ATRIBUICAO/NOTAS PASSARAM")
