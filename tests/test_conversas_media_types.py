"""
CONV-04 — Imagem/video/documento send & receive no Conversas.

Prova que (sobre o backend generico do CONV-03):
  1. Envio de IMAGEM com caption -> 'sent', msg_type image, caption no content,
     asset espelhado.
  2. Envio de DOCUMENTO (pdf) -> filename sanitizado persistido.
  3. Envio de VIDEO mp4 -> aceito pela politica.
  4. MIME nao suportado -> 415 sem persistir; imagem oversize (6MB) -> 413.
  5. Falha do provider em imagem -> 'failed' + 502 (retry-compatible, sem falso-sent).
  6. Inbound de documento com filename -> asset com filename (render usa escapado).
  7. Frontend (grep estatico): render por tipo usa ids numericos coercidos e
     filename via escapeHtml (texto E data-attribute); accept do input ampliado.

Meta mockada. Roda standalone:  python tests/test_conversas_media_types.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_mediatypes_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()
STORAGE = SCRATCH / "media_types_test"

SECRET_SENTINEL = "TOKEN_SECRETO_NAO_VAZAR_TYPES"

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_ACCESS_TOKEN"] = SECRET_SENTINEL
os.environ["META_PHONE_NUMBER_ID"] = "0000000000"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"
os.environ["CONVERSAS_MEDIA_DIR"] = str(STORAGE)

sys.path.insert(0, str(CONVERSAS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
import app.routers.webhook as wh  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.models.media_asset import MediaAsset  # noqa: E402
from app.services import whatsapp  # noqa: E402

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


async def _noop(*a, **k):
    return None


async def _noop_false(*a, **k):
    return False


wh.whatsapp.mark_as_read = _noop
wh.whatsapp.send_text_message = _noop
wh.crm_service.auto_link_conversation = _noop_false

_seq = {"n": 0}


def mock_ok():
    async def _up(content, mime, db=None):
        _seq["n"] += 1
        return {"id": f"UPT-{_seq['n']}"}

    async def _send(*a, **k):
        _seq["n"] += 1
        return {"messages": [{"id": f"wamid.TYPE_{_seq['n']}"}]}

    whatsapp.upload_media = _up
    whatsapp.send_media_message = _send


def mock_fail_send():
    async def _up(content, mime, db=None):
        _seq["n"] += 1
        return {"id": f"UPT-{_seq['n']}"}

    async def _send(*a, **k):
        return {"error": True, "status_code": 400, "summary": "HTTP 400: media send error"}

    whatsapp.upload_media = _up
    whatsapp.send_media_message = _send


s = SessionLocal()
conv = Conversation(lead_id=1, whatsapp="5511900055555", nome="Cliente Tipos", status="aberta")
s.add(conv)
s.commit()
s.refresh(conv)
CID = conv.id
s.close()

JPEG = b"\xff\xd8\xff\xe0IMG" * 100
PDF = b"%PDF-1.4 FAKE" * 100
MP4 = b"\x00\x00\x00\x18ftypmp42" * 100


def asset_of(msg_id):
    s = SessionLocal()
    try:
        return s.query(MediaAsset).filter(MediaAsset.message_id == msg_id).first()
    finally:
        s.close()


# ============ 1. IMAGEM COM CAPTION ============
print("CONV-04 — imagem com caption")
mock_ok()
r1 = client.post(f"/api/conversations/{CID}/messages/media",
                 files={"file": ("foto_visita.jpg", JPEG, "image/jpeg")},
                 data={"caption": "Foto da visita ao deserto"})
check(r1.status_code == 200, f"imagem 200 (got {r1.status_code})")
b1 = r1.json()
check(b1["msg_type"] == "image" and b1["status"] == "sent", "msg_type image + sent")
check(b1["content"] == "Foto da visita ao deserto", "caption persistida no content")
a1 = asset_of(b1["id"])
check(a1.status == "downloaded" and a1.filename == "foto_visita.jpg", "asset espelhado + filename")

# ============ 2. DOCUMENTO PDF ============
print("\nCONV-04 — documento pdf")
r2 = client.post(f"/api/conversations/{CID}/messages/media",
                 files={"file": ("contrato ..\\..\\hack.pdf", PDF, "application/pdf")})
check(r2.status_code == 200, "pdf 200")
b2 = r2.json()
check(b2["msg_type"] == "document", "classificado como document")
a2 = asset_of(b2["id"])
check(a2.filename == "hack.pdf", f"filename SANITIZADO (sem path) (got {a2.filename!r})")

# ============ 3. VIDEO MP4 ============
print("\nCONV-04 — video mp4")
r3 = client.post(f"/api/conversations/{CID}/messages/media",
                 files={"file": ("clip.mp4", MP4, "video/mp4")})
check(r3.status_code == 200 and r3.json()["msg_type"] == "video", "video aceito e classificado")

# ============ 4. POLITICA ============
print("\nCONV-04 — politica 415/413")
s = SessionLocal()
count_before = s.query(Message).filter(Message.conversation_id == CID).count()
s.close()
check(client.post(f"/api/conversations/{CID}/messages/media",
                  files={"file": ("x.exe", b"MZ", "application/x-msdownload")}).status_code == 415,
      "exe -> 415")
check(client.post(f"/api/conversations/{CID}/messages/media",
                  files={"file": ("big.jpg", b"A" * (6 * 1024 * 1024), "image/jpeg")}).status_code == 413,
      "imagem 6MB -> 413")
s = SessionLocal()
check(s.query(Message).filter(Message.conversation_id == CID).count() == count_before,
      "rejeicoes nao persistiram Message")
s.close()

# ============ 5. FALHA DO PROVIDER ============
print("\nCONV-04 — falha do provider em imagem")
mock_fail_send()
r5 = client.post(f"/api/conversations/{CID}/messages/media",
                 files={"file": ("f2.jpg", JPEG, "image/jpeg")})
check(r5.status_code == 502, "falha -> 502")
check(SECRET_SENTINEL not in r5.text, "sem segredo no erro")
s = SessionLocal()
failed = s.query(Message).filter(Message.conversation_id == CID, Message.status == "failed").all()
check(len(failed) == 1 and failed[0].msg_type == "image", "imagem 'failed' (retry-compatible)")
s.close()

# ============ 6. INBOUND DOCUMENT ============
print("\nCONV-04 — inbound de documento com filename")
client.post("/webhook", json={"entry": [{"changes": [{"value": {
    "contacts": [{"profile": {"name": "Cliente Tipos"}}],
    "messages": [{"from": "5511900055555", "id": "wamid.DOCIN", "type": "document",
                  "timestamp": "1700000000",
                  "document": {"id": "DOC-MID", "mime_type": "application/pdf",
                               "sha256": "h", "filename": "<b>nota</b>.pdf"}}],
}}]}]})
s = SessionLocal()
m6 = s.query(Message).filter(Message.whatsapp_msg_id == "wamid.DOCIN").first()
s.close()
a6 = asset_of(m6.id)
check(a6 is not None and a6.filename == "<b>nota</b>.pdf",
      "filename bruto persistido (escape acontece no render)")

# ============ 7. FRONTEND ESTATICO ============
print("\nCONV-04 — frontend XSS-safe (grep estatico)")
js = (CONVERSAS_DIR / "static" / "js" / "conversas.js").read_text(encoding="utf-8")
html = (CONVERSAS_DIR / "templates" / "conversas.html").read_text(encoding="utf-8")
check("window._showInlineMedia(${Number(msg.media_asset.id)}, 'image'" in js,
      "imagem: id numerico coercido")
check("window._showInlineMedia(${Number(msg.media_asset.id)}, 'video'" in js,
      "video: id numerico coercido")
check("escapeHtml(msg.media_asset.filename" in js, "filename via escapeHtml no render")
check('data-fn="${fn}"' in js, "data-fn usa o filename JA escapado")
check("image/jpeg" in html and "application/pdf" in html, "accept do input ampliado")
import re  # noqa: E402
raw_fn = re.search(r'onclick="[^"]*filename', js)
check(raw_fn is None, "filename NUNCA interpolado cru em onclick")

# --- Resultado ---
main.app.dependency_overrides.clear()
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE TIPOS DE MIDIA PASSARAM")
