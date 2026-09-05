"""
CONV-HOTFIX-R10-01 - o read-repair do responsavel nao derruba mais o inbox.

Fonte: bia-v2-r8-r10-inventory.md, secao 2.3 ("a cadeia de dano completa").

A CADEIA DE DANO, REPRODUZIDA LITERALMENTE
-------------------------------------------
  1. Alguem reatribui o lead PELO CRM (`PUT /api/leads/{id}/responsavel` - a
     `Tool Alterar Responsavel` do n8n faz exatamente isso, com um id fixo).
     Nenhuma nocao de que existe uma conversa em atendimento humano.
  2. A conversa correspondente esta em `WAITING_HUMAN`, onde a CHECK constraint
     de m015 exige `atendente_id = responsavel_id`. Agora os dois divergem.
  3. A notificacao de handoff NAO cura isso: o handoff V1 preserva o dono atual
     e nao toca a tabela `leads`. A divergencia fica de pe.
  4. O proximo `GET /api/conversations` roda `_repair_responsavel_cache`, que
     alinha `conversation.responsavel_id` ao lead SEM tocar `atendente_id`.
  5. O `db.commit()` viola a CHECK -> `IntegrityError`.
  6. O erro derrubava A LISTAGEM INTEIRA do inbox (nao so aquela conversa) e
     voltava A CADA CICLO DE POLLING enquanto a divergencia existisse.

O que este arquivo prova:

  1. A PRE-CONDICAO E REAL - o UPDATE que o read-repair emite de fato viola a
     CHECK neste banco. Sem isto o resto do teste provaria o vazio.
  2. LISTAGEM - `GET /api/conversations` responde 200, e nao 500.
  3. DETALHE - `GET /api/conversations/{id}?opening=true` (o outro call site)
     responde 200 tambem.
  4. O reparo INCOERENTE nao foi aplicado - o cache fica como estava, em vez de
     um valor que o banco recusa.
  5. ESCOPO ESTREITO - qualquer OUTRO `IntegrityError` continua subindo. Sem
     isto o "conserto" seria um `except IntegrityError: pass` disfarcado.
  6. O caminho FELIZ nao mudou - um reparo COERENTE continua sendo aplicado e
     commitado.

CONVERSAS_DIR presente de proposito (marcador de job do CI, ver
.github/workflows/test.yml, que separa os dois jobs por grep -l CONVERSAS_DIR).

Roda standalone:  python tests/test_conversas_repair_responsavel_hotfix.py
"""
import os
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_repair_responsavel_hotfix_test.db"
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
from sqlalchemy import text, update as sa_update  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

import app.main as main  # noqa: E402
from app.auth import get_current_user, User  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
from app.routers.conversations import _reparar_responsavel_tolerando_recusa  # noqa: E402

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

DONO = 7            # atendente E responsavel da conversa (coerente)
OUTRO = 8           # para quem o CRM reatribui o lead pelas costas
LEAD_HUMANO = 201   # conversa em atendimento humano
LEAD_TRIAGEM = 202  # conversa fora do escopo da CHECK (controle)
AGORA = datetime.now(timezone.utc)


def db_exec(sql, params=None):
    s = SessionLocal()
    try:
        s.execute(text(sql), params or {})
        s.commit()
    finally:
        s.close()


s = SessionLocal()
s.add_all([
    User(id=1, nome="Tester Local", email="tester@local", hashed_password="x", is_active=True),
    User(id=DONO, nome="Maria Vendas", email="maria@local", hashed_password="x", is_active=True),
    User(id=OUTRO, nome="Joao Backup", email="joao@local", hashed_password="x", is_active=True),
])

# Conversa COERENTE em WAITING_HUMAN: as sete invariantes de m015 satisfeitas.
conv_humana = Conversation(
    lead_id=LEAD_HUMANO, whatsapp="5511900077701", nome="Em Atendimento", status="aberta",
    unread_count=3, is_bot_active=False, atendente_id=DONO, responsavel_id=DONO,
    queued_at=AGORA, primeira_resposta_humana_at=None,
    state="WAITING_HUMAN", state_version=4,
)
# Controle: mesma divergencia, mas em AI_TRIAGE, onde a CHECK e inerte.
conv_triagem = Conversation(
    lead_id=LEAD_TRIAGEM, whatsapp="5511900077702", nome="Na Triagem", status="aberta",
    unread_count=0, is_bot_active=True, responsavel_id=DONO,
    state="AI_TRIAGE", state_version=2,
)
s.add_all([conv_humana, conv_triagem])
s.commit()
s.refresh(conv_humana)
s.refresh(conv_triagem)
CID_HUMANA, CID_TRIAGEM = conv_humana.id, conv_triagem.id
s.close()

# A tabela `leads` do CRM, no MESMO banco compartilhado.
db_exec("CREATE TABLE leads (id INTEGER PRIMARY KEY, nome VARCHAR(200), "
        "whatsapp VARCHAR(30), responsavel_id INTEGER)")
# PASSO 1 DA CADEIA: o CRM ja reatribuiu os dois leads para OUTRO.
db_exec(f"INSERT INTO leads (id, nome, whatsapp, responsavel_id) "
        f"VALUES ({LEAD_HUMANO}, 'Lead Humano', '5511900077701', {OUTRO})")
db_exec(f"INSERT INTO leads (id, nome, whatsapp, responsavel_id) "
        f"VALUES ({LEAD_TRIAGEM}, 'Lead Triagem', '5511900077702', {OUTRO})")


def responsavel_no_banco(cid):
    s = SessionLocal()
    try:
        return s.query(Conversation).filter(Conversation.id == cid).first().responsavel_id
    finally:
        s.close()


# ===========================================================================
# 1. A PRE-CONDICAO E REAL - o UPDATE do read-repair viola mesmo a CHECK
# ===========================================================================
print("CONV-HOTFIX-R10-01 - a pre-condicao (sem ela o teste seria vazio)")

s = SessionLocal()
_erro = None
try:
    s.execute(
        sa_update(Conversation)
        .where(Conversation.id == CID_HUMANA)
        .values(responsavel_id=OUTRO, responsavel_nome="Joao Backup")
    )
    s.commit()
except IntegrityError as exc:
    _erro = exc
    s.rollback()
finally:
    s.close()

check(
    _erro is not None and "ck_conversations_estado_humano" in str(getattr(_erro, "orig", _erro)),
    f"1. o UPDATE que `_repair_responsavel_cache` emite VIOLA a CHECK m015 neste "
    f"banco - a cadeia de dano e mecanica, nao hipotetica [{type(_erro).__name__}]",
)
check(
    responsavel_no_banco(CID_HUMANA) == DONO,
    "1b. e o rollback deixou o cache como estava (pre-condicao limpa)",
)

# ===========================================================================
# 2-4. AS DUAS ROTAS DE LEITURA SOBREVIVEM
# ===========================================================================
print("\nCONV-HOTFIX-R10-01 - as rotas de LEITURA nao devolvem mais 500")

r_list = client.get("/api/conversations")
check(
    r_list.status_code == 200,
    f"2. GET /api/conversations -> 200 (era 500: o IntegrityError do reparo "
    f"derrubava a listagem INTEIRA). Obteve {r_list.status_code}",
)
_ids = [c["id"] for c in r_list.json()["conversations"]]
check(
    CID_HUMANA in _ids and CID_TRIAGEM in _ids,
    f"2b. e as DUAS conversas continuam na pagina - o reparo recusado nao remove "
    f"nem esconde linha nenhuma (ids={_ids})",
)

r_det = client.get(f"/api/conversations/{CID_HUMANA}")
check(
    r_det.status_code == 200,
    f"3. GET /api/conversations/{{id}} com opening=true (o DEFAULT, e o outro call "
    f"site do reparo) -> 200. Obteve {r_det.status_code}",
)

check(
    responsavel_no_banco(CID_HUMANA) == DONO,
    f"4. o reparo INCOERENTE nao foi aplicado: o cache segue com o dono coerente "
    f"({DONO}), nunca com o valor que o banco recusa (obteve "
    f"{responsavel_no_banco(CID_HUMANA)})",
)

# Repetir a listagem: o bug voltava A CADA CICLO DE POLLING.
check(
    all(client.get("/api/conversations").status_code == 200 for _ in range(3)),
    "4b. e o polling seguinte tambem responde 200 - o defeito reaparecia a cada "
    "refresh enquanto a divergencia existisse",
)

# ===========================================================================
# 5. O CAMINHO FELIZ NAO MUDOU - reparo COERENTE continua sendo aplicado
# ===========================================================================
print("\nCONV-HOTFIX-R10-01 - o reparo legitimo continua funcionando")

check(
    responsavel_no_banco(CID_TRIAGEM) == OUTRO,
    f"5. na conversa AI_TRIAGE (onde a CHECK e inerte) o read-repair FOI aplicado "
    f"e commitado normalmente: cache alinhado ao CRM ({OUTRO}) - o hotfix nao "
    f"desligou o reparo, so parou de derrubar a rota (obteve "
    f"{responsavel_no_banco(CID_TRIAGEM)})",
)

# ===========================================================================
# 6. ESCOPO ESTREITO - outro IntegrityError NAO e engolido
# ===========================================================================
print("\nCONV-HOTFIX-R10-01 - o escopo do except e estreito")

# `whatsapp` tem UNIQUE: um UPDATE que colide com outra linha levanta
# IntegrityError por um motivo que NAO e a CHECK de m015.
s = SessionLocal()
_conv = s.query(Conversation).filter(Conversation.id == CID_TRIAGEM).first()
_subiu = False
try:
    s.execute(
        sa_update(Conversation)
        .where(Conversation.id == CID_TRIAGEM)
        .values(whatsapp="5511900077701")
    )
    s.commit()
except IntegrityError as _exc:
    _subiu = "ck_conversations_estado_humano" not in str(getattr(_exc, "orig", _exc))
    s.rollback()
finally:
    s.close()

check(
    _subiu,
    "6. pre-condicao: existe IntegrityError DIFERENTE da CHECK m015 neste banco "
    "(UNIQUE de `whatsapp`)",
)

# E o guarda o deixa subir: mesma colisao, agora atraves do wrapper.
s = SessionLocal()
_conv = s.query(Conversation).filter(Conversation.id == CID_TRIAGEM).first()


def _repair_que_colide(db, conversation, lead_resp):
    db.execute(
        sa_update(Conversation)
        .where(Conversation.id == conversation.id)
        .values(whatsapp="5511900077701")
    )
    return True


_conv_mod = sys.modules["app.routers.conversations"]
_original = _conv_mod._repair_responsavel_cache
_conv_mod._repair_responsavel_cache = _repair_que_colide
_propagou = False
try:
    _reparar_responsavel_tolerando_recusa(s, _conv, {"responsavel_id": OUTRO})
except IntegrityError:
    _propagou = True
    s.rollback()
finally:
    _conv_mod._repair_responsavel_cache = _original
    s.close()

check(
    _propagou,
    "6b. o guarda DEIXA SUBIR um IntegrityError que nao e a CHECK m015 - o hotfix "
    "nao virou um `except IntegrityError: pass` disfarcado",
)

print()
if failures:
    print(f"FALHAS: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("TODOS OS CHECKS PASSARAM")
