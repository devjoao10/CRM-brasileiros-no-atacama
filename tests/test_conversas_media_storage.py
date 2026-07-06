"""
CONV-02 — Storage de midia + preview autenticado do Conversas.

Prova que:
  1. Download da Meta (mockada) espelha o binario e transiciona
     referenced -> downloaded (local_path/size/downloaded_at preenchidos).
  2. Falha do provider persiste last_error SEGURO e status 'failed'.
  3. media_id expirado (400/404) -> status 'expired' + HTTP 410 no fetch.
  4. Politica MIME/tamanho bloqueia download fora da regra.
  5. GET /api/media/{id} autenticado serve o binario correto.
  6. Sem autenticacao -> 401.
  7. Path traversal em local_path corrompido -> 404 (leitura confinada).
  8. Asset inexistente -> 404; nao baixado -> 409.
  9. upload_media (fundacao outbound) retorna media_id com Meta mockada.
 10. Nenhum segredo em respostas de erro.

Meta 100% mockada; storage em scratch/. Roda standalone:

    python tests/test_conversas_media_storage.py
"""
import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_media_storage_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()
STORAGE = SCRATCH / "media_storage_test"

SECRET_SENTINEL = "TOKEN_SECRETO_NAO_VAZAR_789"

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
from app.services import whatsapp, media_storage  # noqa: E402

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

JPEG_BYTES = b"\xff\xd8\xff\xe0FAKEJPEGDATA" * 10


def make_asset(media_id="MID-1", mime="image/jpeg", msg_type="image"):
    s = SessionLocal()
    conv = Conversation(lead_id=1, whatsapp="5511900022222", nome="Cliente Storage", status="aberta")
    s.add(conv)
    s.commit()
    s.refresh(conv)
    m = Message(conversation_id=conv.id, direction="inbound", content="[MIDIA]",
                msg_type=msg_type, media_url=media_id, status="received")
    s.add(m)
    s.commit()
    s.refresh(m)
    a = MediaAsset(message_id=m.id, meta_media_id=media_id, meta_mime_type=mime, status="referenced")
    s.add(a)
    s.commit()
    s.refresh(a)
    aid = a.id
    s.close()
    return aid


def get_asset(aid):
    s = SessionLocal()
    try:
        return s.query(MediaAsset).filter(MediaAsset.id == aid).first()
    finally:
        s.close()


def mock_meta(url_info=None, content=None):
    async def _get_media_url(media_id, db=None):
        return url_info

    async def _download(url, db=None):
        return content

    whatsapp.get_media_url = _get_media_url
    whatsapp.download_media_content = _download
    # media_storage importou o MODULO whatsapp — patch no modulo e suficiente


# ============ 1. DOWNLOAD OK ============
print("CONV-02 — download com sucesso")
aid1 = make_asset("MID-OK")
mock_meta({"url": "https://cdn.meta/fake", "mime_type": "image/jpeg", "file_size": len(JPEG_BYTES)},
          {"content": JPEG_BYTES})
r = client.post(f"/api/media/{aid1}/fetch")
check(r.status_code == 200, f"fetch responde 200 (got {r.status_code})")
a1 = get_asset(aid1)
check(a1.status == "downloaded", "status referenced -> downloaded")
check(a1.local_path == f"asset_{aid1}.jpg", "local_path gerado server-side (asset_<id>.jpg)")
check(a1.local_size_bytes == len(JPEG_BYTES), "local_size_bytes correto")
check(a1.downloaded_at is not None, "downloaded_at preenchido")
check((STORAGE / a1.local_path).exists(), "arquivo existe no storage dir")

# fetch idempotente
r_again = client.post(f"/api/media/{aid1}/fetch")
check(r_again.status_code == 200, "fetch idempotente quando ja baixada")


# ============ 2. FALHA DO PROVIDER ============
print("\nCONV-02 — falha do provider persiste erro seguro")
aid2 = make_asset("MID-FAIL")
mock_meta({"error": True, "status_code": 500, "summary": "HTTP 500: Internal error (code 1)"}, None)
r2 = client.post(f"/api/media/{aid2}/fetch")
check(r2.status_code == 502, f"fetch falho responde 502 (got {r2.status_code})")
a2 = get_asset(aid2)
check(a2.status == "failed", "status 'failed'")
check(a2.last_error and "HTTP 500" in a2.last_error, "last_error com resumo seguro")
check(SECRET_SENTINEL not in r2.text, "sem segredo no corpo do erro")


# ============ 3. MEDIA_ID EXPIRADO ============
print("\nCONV-02 — media_id expirado -> 'expired' + 410")
aid3 = make_asset("MID-EXP")
mock_meta({"error": True, "status_code": 404, "summary": "HTTP 404: media not found"}, None)
r3 = client.post(f"/api/media/{aid3}/fetch")
check(r3.status_code == 410, f"fetch de expirada responde 410 (got {r3.status_code})")
check(get_asset(aid3).status == "expired", "status 'expired'")


# ============ 4. POLITICA BLOQUEIA ============
print("\nCONV-02 — politica MIME/tamanho bloqueia download")
aid4 = make_asset("MID-BIG")
mock_meta({"url": "https://cdn.meta/fake", "mime_type": "image/jpeg",
           "file_size": 50 * 1024 * 1024}, {"content": JPEG_BYTES})
r4 = client.post(f"/api/media/{aid4}/fetch")
check(r4.status_code == 502 and get_asset(aid4).status == "failed",
      "imagem de 50MB bloqueada pela politica")
check("politica" in (get_asset(aid4).last_error or ""), "motivo da politica no last_error")

aid4b = make_asset("MID-GIF", mime="image/gif")
mock_meta({"url": "https://cdn.meta/fake", "mime_type": "image/gif", "file_size": 100},
          {"content": b"GIF89a"})
r4b = client.post(f"/api/media/{aid4b}/fetch")
check(r4b.status_code == 502 and get_asset(aid4b).status == "failed", "MIME nao aceito bloqueado")


# ============ 5. SERVE AUTENTICADO ============
print("\nCONV-02 — GET serve o binario correto")
r5 = client.get(f"/api/media/{aid1}")
check(r5.status_code == 200, "GET 200 para asset baixado")
check(r5.content == JPEG_BYTES, "conteudo identico ao baixado")
check(r5.headers.get("content-type", "").startswith("image/jpeg"), "content-type do asset")


# ============ 6. SEM AUTH -> 401 ============
print("\nCONV-02 — sem autenticacao rejeitado")
main.app.dependency_overrides.clear()  # remove o override de auth
r6 = client.get(f"/api/media/{aid1}")
check(r6.status_code == 401, f"GET sem auth -> 401 (got {r6.status_code})")
r6b = client.post(f"/api/media/{aid1}/fetch")
check(r6b.status_code == 401, f"fetch sem auth -> 401 (got {r6b.status_code})")
main.app.dependency_overrides[get_current_user] = lambda: _DummyUser()


# ============ 7. PATH TRAVERSAL ============
print("\nCONV-02 — path traversal bloqueado")
outside = SCRATCH / "fora_do_storage.txt"
outside.write_text("conteudo proibido")
s = SessionLocal()
a_evil = s.query(MediaAsset).filter(MediaAsset.id == aid1).first()
a_evil.local_path = "../fora_do_storage.txt"  # corrompido: escapa do storage dir
s.commit()
s.close()
r7 = client.get(f"/api/media/{aid1}")
check(r7.status_code == 404, f"traversal -> 404 (got {r7.status_code})")
check(b"proibido" not in r7.content, "conteudo externo NAO servido")
# restaura
s = SessionLocal()
a_fix = s.query(MediaAsset).filter(MediaAsset.id == aid1).first()
a_fix.local_path = f"asset_{aid1}.jpg"
s.commit()
s.close()


# ============ 8. 404 / 409 ============
print("\nCONV-02 — asset inexistente e nao-baixado")
check(client.get("/api/media/99999").status_code == 404, "asset inexistente -> 404")
aid8 = make_asset("MID-REF")
check(client.get(f"/api/media/{aid8}").status_code == 409, "asset 'referenced' -> 409 no GET")


# ============ 9. UPLOAD FOUNDATION ============
print("\nCONV-02 — upload_media (fundacao outbound)")
import httpx  # noqa: E402


class _FakeUploadResp:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"id": "UPLOADED-MEDIA-1"}


class _FakeAsyncClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _FakeUploadResp()


_orig = httpx.AsyncClient
httpx.AsyncClient = _FakeAsyncClient
up = asyncio.run(whatsapp.upload_media(b"12345", "image/jpeg"))
httpx.AsyncClient = _orig
check(isinstance(up, dict) and up.get("id") == "UPLOADED-MEDIA-1", "upload retorna media_id")

# send_media_message por media_id monta payload {'id': ...} (sem link)
sent_payload = {}


class _FakeSendResp:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"messages": [{"id": "wamid.MEDIA_SEND"}]}


class _FakeSendClient(_FakeAsyncClient):
    async def post(self, url, json=None, headers=None, **k):
        sent_payload.update(json or {})
        return _FakeSendResp()


httpx.AsyncClient = _FakeSendClient
res = asyncio.run(whatsapp.send_media_message("5511999", "image", media_id="UPLOADED-MEDIA-1"))
httpx.AsyncClient = _orig
check(res and "messages" in res, "send por media_id retorna sucesso")
check(sent_payload.get("image", {}).get("id") == "UPLOADED-MEDIA-1",
      "payload usa {'id': media_id} (nao link)")


# --- Resultado ---
main.app.dependency_overrides.clear()
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE STORAGE DE MIDIA PASSARAM")
