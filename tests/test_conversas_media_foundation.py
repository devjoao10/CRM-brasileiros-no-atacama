"""
CONV-01 — Fundacao de midia do Conversas (media_assets + politica + webhook).

Prova que:
  1. Politica MIME/tamanho (media_policy) classifica/valida conforme a Meta.
  2. Inbound de IMAGEM persiste MediaAsset (mime/sha256/media_id) com status
     'referenced', SEM alterar o contrato Message.media_url (= media_id).
  3. Inbound de DOCUMENT persiste filename.
  4. Mensagem de texto NAO cria asset.
  5. Idempotencia do webhook: reenvio do mesmo msg_id nao duplica asset.
  6. Falha na criacao do asset NAO perde a mensagem inbound.
  7. MessageResponse expoe media_asset (aditivo; None em texto).
  8. Migration m004 e idempotente em banco antigo (2a execucao = no-op).

Meta API mockada; nenhuma credencial real. Roda standalone (processo isolado):

    python tests/test_conversas_media_foundation.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_media_test.db"
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
import app.routers.webhook as wh  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.models.conversation import Message  # noqa: E402
from app.models.media_asset import MediaAsset  # noqa: E402
from app.schemas.conversation import MessageResponse  # noqa: E402
from app.services import media_policy  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


# --- Setup ---
Base.metadata.create_all(bind=engine)
client = TestClient(main.app)


async def _noop(*a, **k):
    return None


async def _noop_false(*a, **k):
    return False


wh.whatsapp.mark_as_read = _noop
wh.whatsapp.send_text_message = _noop  # sem auto-reply real
wh.crm_service.auto_link_conversation = _noop_false


def q(model, **filters):
    s = SessionLocal()
    try:
        query = s.query(model)
        for k, v in filters.items():
            query = query.filter(getattr(model, k) == v)
        return query.all()
    finally:
        s.close()


def inbound_payload(msg_id, msg_type, media_obj, sender="5511900011111"):
    return {"entry": [{"changes": [{"value": {
        "contacts": [{"profile": {"name": "Cliente Media"}}],
        "messages": [{"from": sender, "id": msg_id, "type": msg_type,
                      "timestamp": "1700000000", msg_type: media_obj}],
    }}]}]}


# ============ 1. POLITICA MIME/TAMANHO ============
print("CONV-01 — media_policy")
check(media_policy.classify_mime("image/jpeg") == "image", "classify image/jpeg -> image")
check(media_policy.classify_mime("audio/ogg; codecs=opus") == "audio", "classify com parametros -> audio")
check(media_policy.classify_mime("application/pdf") == "document", "classify pdf -> document")
check(media_policy.classify_mime("application/x-evil") is None, "MIME desconhecido -> None")
check(media_policy.classify_mime(None) is None, "None -> None")
check(media_policy.is_allowed("image", "image/png") is True, "png permitido em image")
check(media_policy.is_allowed("image", "application/pdf") is False, "pdf negado em image")
check(media_policy.max_size_for("video") == 16 * 1024 * 1024, "limite video 16MB")

ok, why = media_policy.validate("image", "image/jpeg", 4 * 1024 * 1024)
check(ok and why is None, "validate imagem 4MB ok")
ok, why = media_policy.validate("image", "image/jpeg", 6 * 1024 * 1024)
check(not ok and "limite" in (why or ""), "validate imagem 6MB estoura limite (motivo seguro)")
ok, why = media_policy.validate("image", "image/gif", None)
check(not ok, "gif rejeitado (Meta nao aceita)")
ok, why = media_policy.validate("banana", "image/jpeg", 10)
check(not ok, "kind desconhecido rejeitado")
ok, why = media_policy.validate("audio", "audio/mpeg", None)
check(ok, "size None pula checagem de tamanho (inbound)")


# ============ 2. INBOUND IMAGEM -> ASSET ============
print("\nCONV-01 — inbound de imagem persiste MediaAsset")
r = client.post("/webhook", json=inbound_payload("wamid.MEDIA1", "image", {
    "id": "MEDIAID-123", "mime_type": "image/jpeg", "sha256": "abc123hash",
    "caption": "foto do produto",
}))
check(r.status_code == 200, "webhook 200")

msgs = q(Message, whatsapp_msg_id="wamid.MEDIA1")
check(len(msgs) == 1, "mensagem inbound persistida")
if msgs:
    m = msgs[0]
    check(m.media_url == "MEDIAID-123", "compat: media_url continua recebendo o media_id")
    check(m.content == "foto do produto", "caption em content (inalterado)")
    assets = q(MediaAsset, message_id=m.id)
    check(len(assets) == 1, "MediaAsset criado (1:1)")
    if assets:
        a = assets[0]
        check(a.meta_media_id == "MEDIAID-123", "meta_media_id persistido")
        check(a.meta_mime_type == "image/jpeg", "meta_mime_type persistido")
        check(a.meta_sha256 == "abc123hash", "meta_sha256 persistido")
        check(a.status == "referenced", "status inicial 'referenced'")
        check(a.local_path is None and a.downloaded_at is None, "espelho local vazio (CONV-02)")


# ============ 3. INBOUND DOCUMENT -> filename ============
print("\nCONV-01 — inbound de documento persiste filename")
client.post("/webhook", json=inbound_payload("wamid.MEDIA2", "document", {
    "id": "MEDIAID-456", "mime_type": "application/pdf", "sha256": "def456",
    "filename": "contrato_atacama.pdf",
}))
docs = q(Message, whatsapp_msg_id="wamid.MEDIA2")
check(len(docs) == 1, "documento persistido")
if docs:
    a2 = q(MediaAsset, message_id=docs[0].id)
    check(a2 and a2[0].filename == "contrato_atacama.pdf", "filename persistido")


# ============ 4. TEXTO NAO CRIA ASSET ============
print("\nCONV-01 — texto nao cria asset")
client.post("/webhook", json={"entry": [{"changes": [{"value": {
    "contacts": [{"profile": {"name": "Cliente Media"}}],
    "messages": [{"from": "5511900011111", "id": "wamid.TXT1", "type": "text",
                  "timestamp": "1700000001", "text": {"body": "so texto"}}],
}}]}]})
txt = q(Message, whatsapp_msg_id="wamid.TXT1")
check(len(txt) == 1 and len(q(MediaAsset, message_id=txt[0].id)) == 0,
      "mensagem de texto sem MediaAsset")


# ============ 5. IDEMPOTENCIA ============
print("\nCONV-01 — reenvio do webhook nao duplica asset")
client.post("/webhook", json=inbound_payload("wamid.MEDIA1", "image", {
    "id": "MEDIAID-123", "mime_type": "image/jpeg", "sha256": "abc123hash",
}))
check(len(q(Message, whatsapp_msg_id="wamid.MEDIA1")) == 1, "mensagem nao duplicada")
check(len(q(MediaAsset, meta_media_id="MEDIAID-123")) == 1, "asset nao duplicado")


# ============ 6. FALHA NO ASSET NAO PERDE A MENSAGEM ============
print("\nCONV-01 — falha na criacao do asset nao perde a mensagem")
_orig_add = wh.MediaAsset


class _BoomAsset:
    def __init__(self, *a, **k):
        raise RuntimeError("boom no asset")


wh.MediaAsset = _BoomAsset
r6 = client.post("/webhook", json=inbound_payload("wamid.MEDIA3", "image", {
    "id": "MEDIAID-789", "mime_type": "image/png", "sha256": "x",
}))
wh.MediaAsset = _orig_add
check(r6.status_code == 200, "webhook segue 200 mesmo com falha no asset")
m3 = q(Message, whatsapp_msg_id="wamid.MEDIA3")
check(len(m3) == 1, "mensagem inbound PRESERVADA apesar da falha no asset")
check(m3 and len(q(MediaAsset, message_id=m3[0].id)) == 0, "asset ausente (falhou) sem efeito colateral")


# ============ 7. SCHEMA EXPOE media_asset ============
print("\nCONV-01 — MessageResponse expoe media_asset")
s = SessionLocal()
m_img = s.query(Message).filter(Message.whatsapp_msg_id == "wamid.MEDIA1").first()
resp = MessageResponse.model_validate(m_img)
check(resp.media_asset is not None and resp.media_asset.meta_mime_type == "image/jpeg",
      "media_asset presente no schema da mensagem de midia")
m_txt = s.query(Message).filter(Message.whatsapp_msg_id == "wamid.TXT1").first()
check(MessageResponse.model_validate(m_txt).media_asset is None,
      "media_asset None em mensagem de texto")
s.close()


# ============ 8. m004 IDEMPOTENTE ============
print("\nm004 — migration idempotente em banco antigo")
import importlib.util  # noqa: E402
from sqlalchemy import create_engine as _ce, inspect as _inspect, text as _text  # noqa: E402

OLD_DB = SCRATCH / "conv_m004_old.db"
if OLD_DB.exists():
    OLD_DB.unlink()
old_engine = _ce(f"sqlite:///{OLD_DB.as_posix()}")
with old_engine.begin() as conn:  # banco ANTIGO: tem messages, nao tem media_assets
    conn.execute(_text(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INTEGER, "
        "direction VARCHAR(10), content TEXT, msg_type VARCHAR(20), media_url TEXT, "
        "whatsapp_msg_id VARCHAR(100), status VARCHAR(20), created_at TIMESTAMP, "
        "last_error TEXT, send_attempts INTEGER, last_attempt_at TIMESTAMP)"
    ))

spec = importlib.util.spec_from_file_location(
    "m004", ROOT / "migrations" / "m004_conversas_media_assets.py")
m004 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m004)

acts1 = m004.run(old_engine)
check(acts1 == ["media_assets:created"], f"1a execucao cria a tabela ({acts1})")
acts2 = m004.run(old_engine)
check(acts2 == ["media_assets:already-present"], f"2a execucao e no-op ({acts2})")
check("media_assets" in _inspect(old_engine).get_table_names(), "tabela presente apos m004")


# --- Resultado ---
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DA FUNDACAO DE MIDIA PASSARAM")
