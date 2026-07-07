"""
CONV-SMOKE-SEED-01 — Seed LOCAL de smoke para o QA manual do Conversas (C3).

Popula o banco de DEV com dados realistas prefixados [SMOKE] para o checklist
TECNOLOGIA_E_SISTEMAS/09_TESTES_QA/CONVERSAS_LOCAL_SMOKE_CHECKLIST.md:
fila, conversa atribuida, encerrada, tags, nota interna, mensagem com FALHA
(botao de retry) e midias placeholder JA espelhadas (imagem/audio/documento
abrem no chat sem Meta e sem internet).

GUARDAS (3 camadas — o script RECUSA rodar se qualquer uma falhar):
  1. CONVERSAS_SEED_DEV_DATA != true          -> recusa
  2. ENVIRONMENT == production                -> recusa
  3. DATABASE_URL nao-local (postgres em host != localhost/127.0.0.1) -> recusa

IDEMPOTENTE: reexecutar reusa os mesmos registros (conversas por numero
[fixo], tags por nome, mensagens por whatsapp_msg_id UNIQUE, nota por
conteudo, asset por message_id UNIQUE). NUNCA deleta nada.

Uso:
    set CONVERSAS_SEED_DEV_DATA=true   (+ CONVERSAS_DEV_EMAIL/PASSWORD)
    python scripts/smoke_seed_conversas.py
"""
import os
import pathlib
import struct
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

# `app.*` deve resolver para conversas/app (padrao migrations/testes)
_CONVERSAS_DIR = pathlib.Path(__file__).resolve().parent.parent / "conversas"
sys.path.insert(0, str(_CONVERSAS_DIR))


# ─── Guardas (avaliadas em TEMPO DE EXECUCAO, testaveis isoladamente) ────────

def refuse_reason(env: dict | None = None) -> str | None:
    """Retorna o motivo da recusa, ou None se o ambiente e seguro para o seed."""
    e = env if env is not None else os.environ
    if str(e.get("CONVERSAS_SEED_DEV_DATA", "")).lower() != "true":
        return "CONVERSAS_SEED_DEV_DATA nao esta 'true' — seed e DEV-ONLY"
    if str(e.get("ENVIRONMENT", "development")).lower() == "production":
        return "ENVIRONMENT=production — seed RECUSADO em producao"
    url = e.get("DATABASE_URL", "sqlite:///./conversas.db")
    if url.startswith("postgres"):
        host = (urlparse(url).hostname or "").lower()
        if host not in ("localhost", "127.0.0.1", ""):
            return f"DATABASE_URL aponta para host nao-local ('{host}') — seed RECUSADO"
    return None


# ─── Placeholders de midia (gerados em codigo; sem dados reais) ──────────────

def _png_bytes() -> bytes:
    """PNG 1x1 azul, valido (abre no navegador)."""
    import base64
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg"
        "+M9QDwADgQF/e5IkGQAAAABJRU5ErkJggg=="
    )


def _wav_bytes() -> bytes:
    """WAV de ~0.4s de tom baixo, valido (toca no <audio>)."""
    rate, n = 8000, 3200
    samples = b"".join(
        struct.pack("<h", int(3000 * ((i // 40) % 2 * 2 - 1))) for i in range(n)
    )
    hdr = b"RIFF" + struct.pack("<I", 36 + len(samples)) + b"WAVEfmt " + \
        struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + \
        b"data" + struct.pack("<I", len(samples))
    return hdr + samples


def _pdf_bytes() -> bytes:
    """PDF minimo valido de 1 pagina."""
    return (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
            b"xref\n0 4\n0000000000 65535 f \ntrailer<</Size 4/Root 1 0 R>>\n%%EOF\n")


# ─── Seed ────────────────────────────────────────────────────────────────────

def run() -> dict:
    reason = refuse_reason()
    if reason:
        print(f"[SMOKE-SEED] RECUSADO: {reason}")
        return {"refused": reason}

    from app.database import SessionLocal, engine, Base
    from app.auth import User
    from app.models.conversation import Conversation, Message
    from app.models.media_asset import MediaAsset
    from app.models.tag import ConversationTag
    from app.models.note import ConversationNote
    from app.services import media_storage
    from app.seed import seed_dev_user

    Base.metadata.create_all(bind=engine)
    seed_dev_user()  # garante o usuario dev (idempotente; exige DEV_EMAIL/PASSWORD)

    db = SessionLocal()
    now = datetime.now(timezone.utc)
    counts = {"conversas": 0, "mensagens": 0, "tags": 0, "notas": 0, "midias": 0, "reusados": 0}

    dev_email = os.getenv("CONVERSAS_DEV_EMAIL", "")
    dev_user = db.query(User).filter(User.email == dev_email).first() \
        or db.query(User).filter(User.is_active == True).first()  # noqa: E712
    if not dev_user:
        db.close()
        print("[SMOKE-SEED] RECUSADO: nenhum usuario ativo (defina CONVERSAS_DEV_EMAIL/PASSWORD)")
        return {"refused": "sem usuario dev"}

    def conv(wpp, nome, status="aberta", atendente=None, ultimo=None, wait_min=0, unread=0):
        c = db.query(Conversation).filter(Conversation.whatsapp == wpp).first()
        if c:
            counts["reusados"] += 1
            return c
        c = Conversation(
            lead_id=0, whatsapp=wpp, nome=nome, status=status,
            atendente_id=atendente, ultimo_msg=ultimo, unread_count=unread,
            last_customer_msg_at=now - timedelta(minutes=wait_min),
        )
        db.add(c)
        db.commit()
        db.refresh(c)
        counts["conversas"] += 1
        return c

    def msg(c, wamid, direction, content, status, minutes_ago, **extra):
        m = db.query(Message).filter(Message.whatsapp_msg_id == wamid).first()
        if m:
            counts["reusados"] += 1
            return m
        m = Message(
            conversation_id=c.id, direction=direction, content=content,
            msg_type=extra.pop("msg_type", "text"), whatsapp_msg_id=wamid,
            status=status, created_at=now - timedelta(minutes=minutes_ago), **extra,
        )
        db.add(m)
        db.commit()
        db.refresh(m)
        counts["mensagens"] += 1
        return m

    def tag(nome, cor):
        t = db.query(ConversationTag).filter(ConversationTag.nome == nome).first()
        if t:
            counts["reusados"] += 1
            return t
        t = ConversationTag(nome=nome, cor=cor)
        db.add(t)
        db.commit()
        db.refresh(t)
        counts["tags"] += 1
        return t

    def media(m, mime, filename, content):
        a = db.query(MediaAsset).filter(MediaAsset.message_id == m.id).first()
        if a:
            counts["reusados"] += 1
            return a
        a = MediaAsset(message_id=m.id, meta_media_id=f"SMOKE-{m.id}",
                       meta_mime_type=mime, filename=filename, status="referenced")
        db.add(a)
        db.commit()
        db.refresh(a)
        media_storage.store_bytes(a, content, mime, db)  # espelho local -> preview offline
        counts["midias"] += 1
        return a

    # 1) Fila — aberta, sem atendente, esperando ha pouco
    c1 = conv("5511911110001", "[SMOKE] Maria Fila", ultimo="Ola, quero informacoes", wait_min=5, unread=2)
    msg(c1, "wamid.SMOKE.C1.1", "inbound", "Ola, quero informacoes sobre o passeio", "received", 6)
    m_img = msg(c1, "wamid.SMOKE.C1.2", "inbound", "foto do comprovante", "received", 5, msg_type="image", media_url=f"SMOKE-IMG")
    m_aud = msg(c1, "wamid.SMOKE.C1.3", "inbound", "[AUDIO]", "received", 4, msg_type="audio")
    m_doc = msg(c1, "wamid.SMOKE.C1.4", "inbound", "[DOCUMENT]", "received", 3, msg_type="document")
    media(m_img, "image/png", "comprovante.png", _png_bytes())
    media(m_aud, "audio/wav", "nota_de_voz.wav", _wav_bytes())
    media(m_doc, "application/pdf", "roteiro_atacama.pdf", _pdf_bytes())

    # 2) Fila antiga — testa ordenacao (mais antiga primeiro)
    c2 = conv("5511911110002", "[SMOKE] Joao Esperando", ultimo="Alguem me atende?", wait_min=180, unread=1)
    msg(c2, "wamid.SMOKE.C2.1", "inbound", "Alguem me atende?", "received", 180)

    # 3) Minha — atribuida ao usuario dev (aba Minhas + Liberar)
    c3 = conv("5511911110003", "[SMOKE] Ana Atribuida", atendente=dev_user.id, ultimo="Perfeito, obrigado!")
    msg(c3, "wamid.SMOKE.C3.1", "inbound", "Podemos fechar para março?", "received", 60)
    msg(c3, "wamid.SMOKE.C3.2", "outbound", "Claro! Vou preparar o orçamento.", "sent", 55)

    # 4) Encerrada
    c4 = conv("5511911110004", "[SMOKE] Pedro Encerrado", status="encerrada", ultimo="Atendimento encerrado. Obrigado!")
    msg(c4, "wamid.SMOKE.C4.1", "inbound", "Era so isso, obrigado!", "received", 300)
    msg(c4, "wamid.SMOKE.C4.2", "outbound", "Atendimento encerrado. Obrigado!", "sent", 299)

    # 5) Com tags (2 tags aplicadas)
    c5 = conv("5511911110005", "[SMOKE] Carla Tags", ultimo="Quero o pacote VIP", unread=1)
    msg(c5, "wamid.SMOKE.C5.1", "inbound", "Quero o pacote VIP", "received", 30)
    t1 = tag("[SMOKE] Urgente", "#EF4444")
    t2 = tag("[SMOKE] Orcamento", "#22C55E")
    t3 = tag("[SMOKE] VIP", "#8B5CF6")
    for t in (t1, t3):
        if t not in c5.tags:
            c5.tags.append(t)
    if t2 not in c3.tags:
        c3.tags.append(t2)
    db.commit()

    # 6) Falha de envio — botao ⟳ de retry visivel
    c6 = conv("5511911110006", "[SMOKE] Rafa Retry", ultimo="mensagem que falhou", unread=0)
    msg(c6, "wamid.SMOKE.C6.1", "inbound", "Pode me mandar o link?", "received", 20)
    m_fail = db.query(Message).filter(
        Message.conversation_id == c6.id, Message.status == "failed"
    ).first()
    if not m_fail:
        m_fail = Message(
            conversation_id=c6.id, direction="outbound",
            content="[SMOKE] esta mensagem falhou — clique em ⟳ para reenviar",
            msg_type="text", status="failed",
            last_error="[SMOKE] HTTP 400: falha simulada para o QA de retry",
            send_attempts=1, last_attempt_at=now - timedelta(minutes=15),
            created_at=now - timedelta(minutes=15),
        )
        db.add(m_fail)
        db.commit()
        counts["mensagens"] += 1
    else:
        counts["reusados"] += 1

    # Nota interna na conversa atribuida
    note = db.query(ConversationNote).filter(
        ConversationNote.conversation_id == c3.id,
        ConversationNote.content.like("[SMOKE]%"),
    ).first()
    if not note:
        db.add(ConversationNote(
            conversation_id=c3.id, user_id=dev_user.id,
            user_nome=dev_user.nome,
            content="[SMOKE] Cliente prefere contato de manha. NAO enviar ao WhatsApp.",
        ))
        db.commit()
        counts["notas"] += 1
    else:
        counts["reusados"] += 1

    db.close()
    print(f"[SMOKE-SEED] OK — criados: {counts['conversas']} conversas, "
          f"{counts['mensagens']} mensagens, {counts['tags']} tags, "
          f"{counts['notas']} notas, {counts['midias']} midias "
          f"(reusados: {counts['reusados']})")
    return counts


if __name__ == "__main__":
    result = run()
    sys.exit(1 if result.get("refused") else 0)
