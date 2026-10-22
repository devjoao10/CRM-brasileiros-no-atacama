"""
WP R13 — regressao de scripts/cleanup_conversation_events.py.

Prova:
  (a) dry run nunca deleta nenhuma linha, independente da idade.
  (b) --apply --confirm deleta SO linhas estritamente antes do cutoff.
  (c) linhas no/depois do cutoff permanecem intocadas.
  (d) --apply SEM --confirm nao deleta nada.

Roda standalone:  python tests/test_cleanup_conversation_events.py
"""
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "cleanup_conversation_events_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"
# Fixo ANTES do primeiro import de app.config/scripts.cleanup_conversation_events
# (constantes de modulo sao lidas uma unica vez, na importacao) — deixa o
# teste (d) deterministico sem depender do ambiente de quem roda a suite.
os.environ["CONVERSATION_EVENTS_RETENTION_DAYS"] = "30"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(CONVERSAS_DIR))

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models.evento import ConversationEvent  # noqa: E402

from scripts.cleanup_conversation_events import (  # noqa: E402
    limpar_eventos_antigos,
    main as cleanup_main,
)

Base.metadata.create_all(bind=engine)
db = SessionLocal()

# Fixado no FUTURO distante de proposito: linhas datadas em relacao a este
# `AGORA` continuam no futuro mesmo do ponto de vista do relogio REAL usado
# por `main()` (que nao aceita `now` injetado) — garante que os checks (a)/
# (b)/(c) nunca fiquem "elegiveis por acidente" quando o teste (d) chamar
# `cleanup_main`, que calcula o cutoff com `datetime.now()` de verdade.
AGORA = datetime(2099, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
RETENTION_DIAS = 30
CUTOFF = AGORA - timedelta(days=RETENTION_DIAS)


def _criar_evento(event_id: str, created_at: datetime, event_type: str = "MESSAGE_RECEIVED") -> int:
    ev = ConversationEvent(
        event_id=event_id,
        event_type=event_type,
        conversation_id=1,
        created_at=created_at,
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev.id


def _existe(evento_id: int) -> bool:
    return db.query(ConversationEvent).filter_by(id=evento_id).first() is not None


# --- (a) dry run nunca deleta, mesmo linha MUITO antiga ---------------------
id_antigo_a = _criar_evento("a-dry-run-antigo", AGORA - timedelta(days=1000))
resultado_dry = limpar_eventos_antigos(db, retention_days=RETENTION_DIAS, apply_changes=False, now=AGORA)
check(resultado_dry.aplicado is False, "a. dry run reporta aplicado=False")
check(resultado_dry.elegivel_total == 1, f"a. dry run conta a linha antiga como elegivel (veio {resultado_dry.elegivel_total})")
check(resultado_dry.deletado_total == 0, "a. dry run reporta deletado_total=0")
check(_existe(id_antigo_a), "a. dry run NAO deleta a linha antiga, independente da idade")

# --- (b)/(c) apply real: deleta SO estritamente antes do cutoff -------------
id_antes = _criar_evento("b-antes-cutoff", CUTOFF - timedelta(seconds=1))
id_no_cutoff = _criar_evento("c-no-cutoff", CUTOFF)
id_depois = _criar_evento("c-depois-cutoff", CUTOFF + timedelta(seconds=1))

# batch_size=1 de proposito: forca multiplos lotes (2 elegiveis: id_antigo_a
# do check (a), que segue elegivel, + id_antes) para exercitar o loop.
# total_antes/total_depois: COUNT(*) da tabela INTEIRA (sem filtro de cutoff),
# medido antes e depois do apply — prova que deletado_total bate com o que o
# banco REALMENTE reporta como deletado, e nao so com o que foi TENTADO.
total_antes = db.query(func.count(ConversationEvent.id)).scalar()
resultado_apply = limpar_eventos_antigos(
    db, retention_days=RETENTION_DIAS, apply_changes=True, batch_size=1, now=AGORA
)
total_depois = db.query(func.count(ConversationEvent.id)).scalar()
check(resultado_apply.aplicado is True, "b. apply reporta aplicado=True")
check(
    resultado_apply.deletado_total == 2,
    f"b. deletado_total bate com o esperado (2), veio {resultado_apply.deletado_total}",
)
check(
    resultado_apply.deletado_total == (total_antes - total_depois),
    "b. deletado_total bate com o COUNT(*) real da tabela (antes - depois), "
    f"esperado {total_antes - total_depois}, veio {resultado_apply.deletado_total}",
)
check(not _existe(id_antigo_a), "b. linha antiga (do check a) foi deletada no apply")
check(not _existe(id_antes), "b. linha estritamente ANTES do cutoff foi deletada")
check(_existe(id_no_cutoff), "c. linha EXATAMENTE no cutoff permanece intocada")
check(_existe(id_depois), "c. linha DEPOIS do cutoff permanece intocada")

# --- (d) --apply sem --confirm nao deleta nada ------------------------------
# `main()` usa datetime.now() real (sem override) — cria a linha relativa ao
# relogio REAL, elegivel sob a retencao real de 30 dias (env acima), para
# provar que mesmo uma linha elegivel sobrevive sem --confirm.
id_sem_confirm = _criar_evento("d-sem-confirm", datetime.now(timezone.utc) - timedelta(days=60))
codigo_saida = cleanup_main(["--apply"])
check(codigo_saida == 1, f"d. --apply sem --confirm devolve codigo de recusa (veio {codigo_saida})")
check(_existe(id_sem_confirm), "d. --apply sem --confirm NAO deleta nada")

db.close()

if failures:
    print(f"\n{len(failures)} FALHA(S):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("\nOK: todos os checks de cleanup_conversation_events passaram")
