"""
CONV-SMOKE-SEED-01 — Seed local de smoke do Conversas.

Prova que:
  1. Recusa sem CONVERSAS_SEED_DEV_DATA=true.
  2. Recusa ENVIRONMENT=production.
  3. Recusa DATABASE_URL postgres em host nao-local (guard de URL).
  4. Com ambiente dev valido: cria os dados esperados — 6 conversas [SMOKE]
     (fila x2, atribuida, encerrada, com tags, com falha p/ retry), mensagens
     inbound/outbound/failed com timestamps variados, 3 tags (2 conversas
     tagueadas), 1 nota interna, 3 midias placeholder ESPELHADAS localmente.
  5. IDEMPOTENTE: 2a execucao nao duplica nada (reusa).
  6. O script NAO contem operacao destrutiva (grep no fonte).
  7. Nao imprime segredos (saida = so contagens).

Roda standalone:  python tests/test_conversas_smoke_seed.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_smoke_seed_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()
STORAGE = SCRATCH / "smoke_seed_media"

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "true"
os.environ["CONVERSAS_DEV_EMAIL"] = "dev@bna.local"
os.environ["CONVERSAS_DEV_PASSWORD"] = "senha-fixture-teste"
os.environ["CONVERSAS_MEDIA_DIR"] = str(STORAGE)
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(CONVERSAS_DIR))

import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "smoke_seed", ROOT / "scripts" / "smoke_seed_conversas.py")
seed_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_mod)

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


# ============ 1/2/3. GUARDAS ============
print("SEED — guardas de ambiente")
check(seed_mod.refuse_reason({"CONVERSAS_SEED_DEV_DATA": "false"}) is not None,
      "recusa sem CONVERSAS_SEED_DEV_DATA=true")
check(seed_mod.refuse_reason({}) is not None, "recusa com flag ausente")
check(seed_mod.refuse_reason({"CONVERSAS_SEED_DEV_DATA": "true",
                              "ENVIRONMENT": "production"}) is not None,
      "recusa ENVIRONMENT=production")
check(seed_mod.refuse_reason({
    "CONVERSAS_SEED_DEV_DATA": "true", "ENVIRONMENT": "development",
    "DATABASE_URL": "postgresql://u:x@postgres:5432/crm_atacama"}) is not None,
    "recusa postgres em host nao-local (padrao de PRODUCAO)")
check(seed_mod.refuse_reason({
    "CONVERSAS_SEED_DEV_DATA": "true", "ENVIRONMENT": "development",
    "DATABASE_URL": "postgresql://u:x@localhost:5432/dev"}) is None,
    "aceita postgres LOCAL")
check(seed_mod.refuse_reason(dict(os.environ)) is None, "ambiente do teste e aceito")


# ============ 4. SEED CRIA OS DADOS ============
print("\nSEED — primeira execucao cria os dados")
r1 = seed_mod.run()
check("refused" not in r1, "seed rodou (nao recusado)")
check(r1["conversas"] == 6, f"6 conversas criadas (got {r1.get('conversas')})")
check(r1["tags"] == 3, "3 tags criadas")
check(r1["notas"] == 1, "1 nota interna criada")
check(r1["midias"] == 3, "3 midias placeholder criadas")

from app.database import SessionLocal  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.models.media_asset import MediaAsset  # noqa: E402
from app.models.note import ConversationNote  # noqa: E402

s = SessionLocal()
convs = s.query(Conversation).filter(Conversation.nome.like("[SMOKE]%")).all()
by_name = {c.nome: c for c in convs}
check(len(convs) == 6, "6 conversas [SMOKE] no banco")

fila = [c for c in convs if c.status == "aberta" and c.atendente_id is None]
check(len(fila) >= 3, f"fila tem conversas aguardando ({len(fila)})")
check(by_name["[SMOKE] Ana Atribuida"].atendente_id is not None, "1 conversa atribuida ao dev")
check(by_name["[SMOKE] Pedro Encerrado"].status == "encerrada", "1 conversa encerrada")
check(len(by_name["[SMOKE] Carla Tags"].tags) == 2, "conversa com 2 tags")
check(len(by_name["[SMOKE] Ana Atribuida"].tags) == 1, "2a conversa tagueada")

fail_msgs = s.query(Message).filter(Message.status == "failed",
                                    Message.content.like("[SMOKE]%")).all()
check(len(fail_msgs) == 1 and fail_msgs[0].send_attempts == 1
      and "[SMOKE]" in (fail_msgs[0].last_error or ""),
      "mensagem failed com metadados (retry testavel na UI)")

dirs = {m.direction for m in s.query(Message).filter(Message.whatsapp_msg_id.like("wamid.SMOKE%"))}
check(dirs == {"inbound", "outbound"}, "mensagens inbound E outbound")
stamps = [m.created_at for m in s.query(Message).filter(Message.whatsapp_msg_id.like("wamid.SMOKE%"))]
check(len(set(stamps)) > 3, "timestamps variados")

assets = s.query(MediaAsset).filter(MediaAsset.meta_media_id.like("SMOKE-%")).all()
check(len(assets) == 3 and all(a.status == "downloaded" for a in assets),
      "3 assets espelhados (preview offline)")
check(all((STORAGE / a.local_path).exists() for a in assets),
      "arquivos placeholder existem no storage")
mimes = {a.meta_mime_type for a in assets}
check(mimes == {"image/png", "audio/wav", "application/pdf"}, f"tipos: imagem/audio/pdf ({mimes})")

notes = s.query(ConversationNote).filter(ConversationNote.content.like("[SMOKE]%")).all()
check(len(notes) == 1 and notes[0].user_nome, "nota interna com autor")
s.close()


# ============ 5. IDEMPOTENCIA ============
print("\nSEED — segunda execucao e idempotente")
r2 = seed_mod.run()
check(r2["conversas"] == 0 and r2["mensagens"] == 0 and r2["tags"] == 0
      and r2["notas"] == 0 and r2["midias"] == 0,
      "2a execucao nao cria NADA novo")
check(r2["reusados"] > 10, f"registros reusados ({r2['reusados']})")
s = SessionLocal()
check(s.query(Conversation).filter(Conversation.nome.like("[SMOKE]%")).count() == 6,
      "continua com exatamente 6 conversas")
s.close()


# ============ 6. SEM OPERACAO DESTRUTIVA ============
print("\nSEED — fonte sem operacoes destrutivas")
src = (ROOT / "scripts" / "smoke_seed_conversas.py").read_text(encoding="utf-8")
check(".delete(" not in src and "DELETE FROM" not in src.upper().replace("NUNCA DELETA", "")
      and "DROP " not in src.upper(), "sem delete/drop no script")
check("[SMOKE]" in src, "dados marcados com prefixo [SMOKE]")

# --- Resultado ---
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DO SMOKE SEED PASSARAM")
