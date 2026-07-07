"""
CONV-03 — Audio send/receive no Conversas.

Prova que:
  1. Inbound de audio (webhook) persiste MediaAsset com metadados.
  2. Outbound por upload (multipart): sucesso -> Message 'sent' + wamid +
     MediaAsset 'downloaded' com espelho local do arquivo do operador.
  3. Falha no UPLOAD a Meta -> Message 'failed' (nunca falso-sent) + 502 seguro.
  4. Falha no SEND (upload ok) -> Message 'failed' + 502 seguro.
  5. Politica: MIME nao suportado -> 415 SEM persistir Message; oversize -> 413.
  6. Retry de midia failed re-faz upload+send a partir do espelho local
     (mesma linha, attempts incrementa).
  7. Simulado (dev sem credenciais) nao e falha, explicito.
  8. Nenhum segredo em respostas de erro.
  9. Frontend: render de audio usa id numerico + botao (grep estatico XSS-safe).

Meta 100% mockada. Roda standalone:  python tests/test_conversas_audio.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_audio_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()
STORAGE = SCRATCH / "media_audio_test"

SECRET_SENTINEL = "TOKEN_SECRETO_NAO_VAZAR_AUDIO"

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

OGG = b"OggS_FAKE_OPUS_AUDIO" * 50

_seq = {"n": 0}


def mock_upload(result):
    async def _up(content, mime, db=None):
        return result
    whatsapp.upload_media = _up


def mock_upload_ok():
    async def _up(content, mime, db=None):
        _seq["n"] += 1
        return {"id": f"UP-MEDIA-{_seq['n']}"}
    whatsapp.upload_media = _up


def mock_send(result):
    async def _send(*a, **k):
        return result
    whatsapp.send_media_message = _send


def mock_send_ok():
    async def _send(*a, **k):
        _seq["n"] += 1
        return {"messages": [{"id": f"wamid.AUD_{_seq['n']}"}]}
    whatsapp.send_media_message = _send


def make_conv(wpp):
    s = SessionLocal()
    c = Conversation(lead_id=1, whatsapp=wpp, nome="Cliente Audio", status="aberta")
    s.add(c)
    s.commit()
    s.refresh(c)
    cid = c.id
    s.close()
    return cid


def q_msg(conv_id, content=None):
    s = SessionLocal()
    try:
        q = s.query(Message).filter(Message.conversation_id == conv_id)
        if content:
            q = q.filter(Message.content == content)
        return q.all()
    finally:
        s.close()


def get_asset_of(msg_id):
    s = SessionLocal()
    try:
        return s.query(MediaAsset).filter(MediaAsset.message_id == msg_id).first()
    finally:
        s.close()


# ============ 1. INBOUND AUDIO ============
print("CONV-03 — inbound de audio persiste asset")
r = client.post("/webhook", json={"entry": [{"changes": [{"value": {
    "contacts": [{"profile": {"name": "Cliente Audio"}}],
    "messages": [{"from": "5511900033333", "id": "wamid.AUDIN", "type": "audio",
                  "timestamp": "1700000000",
                  "audio": {"id": "AUDIO-MID-1", "mime_type": "audio/ogg; codecs=opus", "sha256": "hashA"}}],
}}]}]})
check(r.status_code == 200, "webhook 200")
s = SessionLocal()
m_in = s.query(Message).filter(Message.whatsapp_msg_id == "wamid.AUDIN").first()
s.close()
check(m_in is not None and m_in.msg_type == "audio", "mensagem de audio persistida")
a_in = get_asset_of(m_in.id)
check(a_in is not None and a_in.meta_mime_type == "audio/ogg; codecs=opus", "asset de audio com mime")


# ============ 2. OUTBOUND SUCESSO ============
print("\nCONV-03 — outbound de audio (upload) com sucesso")
cid = make_conv("5511900044444")
mock_upload_ok()
mock_send_ok()
r2 = client.post(f"/api/conversations/{cid}/messages/media",
                 files={"file": ("nota_voz.ogg", OGG, "audio/ogg")},
                 data={"caption": ""})
check(r2.status_code == 200, f"envio de audio 200 (got {r2.status_code})")
body = r2.json()
check(body.get("status") == "sent" and body.get("whatsapp_msg_id", "").startswith("wamid.AUD"),
      "Message 'sent' + wamid")
check(body.get("msg_type") == "audio", "msg_type classificado como audio pelo MIME")
asset2 = get_asset_of(body["id"])
check(asset2 is not None and asset2.status == "downloaded", "asset outbound com espelho local")
check(asset2.meta_media_id and asset2.meta_media_id.startswith("UP-MEDIA"), "media_id do upload gravado")
check((STORAGE / asset2.local_path).read_bytes() == OGG, "arquivo do operador espelhado byte a byte")
check(asset2.filename == "nota_voz.ogg", "filename sanitizado persistido")
s = SessionLocal()
conv_after = s.query(Conversation).filter(Conversation.id == cid).first()
s.close()
check(conv_after.ultimo_msg == "[AUDIO]", "preview atualizado no sucesso")


# ============ 3. FALHA NO UPLOAD ============
print("\nCONV-03 — falha no upload NAO gera falso-sent")
mock_upload({"error": True, "status_code": 500, "summary": "HTTP 500: upload error (code 2)"})
r3 = client.post(f"/api/conversations/{cid}/messages/media",
                 files={"file": ("f.ogg", OGG, "audio/ogg")})
check(r3.status_code == 502, f"falha de upload -> 502 (got {r3.status_code})")
failed_msgs = [m for m in q_msg(cid) if m.status == "failed"]
check(len(failed_msgs) == 1, "Message persistida como 'failed'")
check(failed_msgs[0].last_error and "HTTP 500" in failed_msgs[0].last_error, "last_error seguro")
check(SECRET_SENTINEL not in r3.text, "sem segredo no erro de upload")
failed_asset = get_asset_of(failed_msgs[0].id)
check(failed_asset is not None and failed_asset.status == "downloaded",
      "espelho local preservado mesmo com falha (permite retry)")

# ============ 4. FALHA NO SEND (upload ok) ============
print("\nCONV-03 — falha no send apos upload ok")
mock_upload_ok()
mock_send({"error": True, "status_code": 400, "summary": "HTTP 400: send error (code 3)"})
r4 = client.post(f"/api/conversations/{cid}/messages/media",
                 files={"file": ("g.ogg", OGG, "audio/ogg")})
check(r4.status_code == 502, "falha de send -> 502")
sent_after = [m for m in q_msg(cid) if m.status == "sent" and m.msg_type == "audio"]
check(len(sent_after) == 1, "nenhum falso-sent adicional (so o sucesso do caso 2)")


# ============ 5. POLITICA 415/413 ============
print("\nCONV-03 — politica rejeita SEM persistir")
count_before = len(q_msg(cid))
r5 = client.post(f"/api/conversations/{cid}/messages/media",
                 files={"file": ("x.xyz", b"data", "application/x-evil")})
check(r5.status_code == 415, f"MIME nao suportado -> 415 (got {r5.status_code})")
r5b = client.post(f"/api/conversations/{cid}/messages/media",
                  files={"file": ("big.ogg", b"A" * (17 * 1024 * 1024), "audio/ogg")})
check(r5b.status_code == 413, f"audio de 17MB -> 413 (got {r5b.status_code})")
check(len(q_msg(cid)) == count_before, "rejeicao de politica NAO persistiu Message")


# ============ 6. RETRY DE MIDIA ============
print("\nCONV-03 — retry de midia failed re-envia do espelho local")
retry_id = failed_msgs[0].id
mock_upload_ok()
mock_send_ok()
r6 = client.post(f"/api/conversations/{cid}/messages/{retry_id}/retry")
check(r6.status_code == 200, f"retry de audio 200 (got {r6.status_code})")
s = SessionLocal()
m6 = s.query(Message).filter(Message.id == retry_id).first()
check(m6.status == "sent" and m6.send_attempts == 2, "failed -> sent na mesma linha, attempts=2")
a6 = s.query(MediaAsset).filter(MediaAsset.message_id == retry_id).first()
check(a6.meta_media_id.startswith("UP-MEDIA"), "novo media_id gravado no asset apos re-upload")
s.close()


# ============ 7. SIMULADO ============
print("\nCONV-03 — simulado (dev) explicito, nao e falha")
mock_upload({"simulated": True, "id": None})
r7 = client.post(f"/api/conversations/{cid}/messages/media",
                 files={"file": ("dev.ogg", OGG, "audio/ogg")})
check(r7.status_code == 200, "simulado responde 200")
check(r7.json().get("whatsapp_msg_id") is None, "simulado sem wamid (distinguivel)")


# ============ 8/9. FRONTEND ESTATICO ============
print("\nCONV-03 — frontend XSS-safe (grep estatico)")
js = (CONVERSAS_DIR / "static" / "js" / "conversas.js").read_text(encoding="utf-8")
check("window._playAudio(${Number(msg.media_asset.id)}" in js,
      "player usa id numerico coercido (sem dado bruto no onclick)")
check("sendMediaFile" in js and "FormData" in js, "upload multipart presente")
check("Authorization" in js, "upload envia Bearer manualmente")

# --- Resultado ---
main.app.dependency_overrides.clear()
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE AUDIO PASSARAM")
