# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-WF2 — regressao: corrida em add_lead_to_funnel virava 500.

`POST /api/pipeline/funnels/{funnel_id}/leads` fazia SELECT-entao-INSERT sem
protecao contra concorrencia: entre o SELECT que verifica se a FunnelEntry ja
existe e o commit do INSERT cabe outra requisicao inserindo a MESMA
(lead_id, funnel_id). O indice unico uq_funnel_entries_lead_funnel
(app/models/pipeline.py, aplicado em producao pela migration m011) barra a
segunda no banco, mas sem tratar o IntegrityError o erro subia cru e virava
500 — nao o 409 que o workflow n8n do formulario do site espera
(neverError: true, decide pelo corpo da resposta).

Este arquivo prova:
  1. Sequencial: adicionar o mesmo lead duas vezes -> 201 depois 409 (o
     contrato documentado do caminho check-then-act, sem tocar concorrencia).
  2. Concorrente: DUAS threads, DUAS sessoes de banco, mesma
     (lead_id, funnel_id) -> nunca 500, sempre {201, 409}, e SO UMA
     FunnelEntry sobrevive. O 409 da corrida e byte-a-byte igual ao 409
     sequencial — quem chama a API nao consegue distinguir qual ramo
     respondeu (checagem previa ou IntegrityError traduzido).

SQLite serializa escritas (lock de arquivo) e pode nao expor a corrida de
forma confiavel — o mesmo arquivo roda tambem contra POSTGRESQL DE VERDADE
apontando DATABASE_URL para o container de auditoria:

  DATABASE_URL=postgresql+psycopg2://bna_test:bna_test_2026@127.0.0.1:55432/bna_app_audit \
    python tests/test_pipeline_funnel_race.py

Ao contrario do resto da suite, este arquivo LE DATABASE_URL do ambiente e so
cai para o SQLite descartavel de sempre quando nada foi definido — e o que
permite a segunda execucao acima sem editar o arquivo. Todas as linhas
criadas (lead, funil, entries, historico) sao apagadas ao final, em qualquer
backend.

Rodar:  python tests/test_pipeline_funnel_race.py
"""
import os
import pathlib
import sys
import threading
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)

sys.path.insert(0, str(ROOT))

# DATABASE_URL: respeita o ambiente (permite apontar para o Postgres real de
# auditoria); so cai para SQLite descartavel quando nada foi definido.
_DB_FILE = SCRATCH / "pipeline_funnel_race_test.db"
if "DATABASE_URL" not in os.environ:
    if _DB_FILE.exists():
        _DB_FILE.unlink()
    os.environ["DATABASE_URL"] = f"sqlite:///{_DB_FILE.as_posix()}"
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("SEED_INITIAL_ADMIN", "false")
# Sem isto, um DEFAULT_FUNNEL_ID que por acaso esteja no ambiente do
# desenvolvedor tornaria a escolha do funil nao-deterministica.
os.environ.pop("DEFAULT_FUNNEL_ID", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.models.lead import Lead  # noqa: E402
from app.models.pipeline import Funnel, FunnelEntry, LeadHistory  # noqa: E402
from app.models.user import UserRole  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover
    pass

falhas = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        falhas.append(msg)


Base.metadata.create_all(bind=engine)


class _Usuario:
    id = 1
    email = "race@local"
    nome = "Teste de corrida"
    role = UserRole.ADMIN
    is_active = True


main.app.dependency_overrides[get_current_user] = lambda: _Usuario()
client = TestClient(main.app)

# suffix unico por execucao — o nome do Funnel e UNIQUE, e este arquivo pode
# rodar mais de uma vez contra o MESMO Postgres compartilhado de auditoria.
_RUN = uuid.uuid4().hex[:8]
_criados = {"leads": [], "funnels": []}


def _criar_funil(db, nome):
    """Devolve o ID (int), nao o objeto ORM: o mesmo `db` cria mais de uma
    linha nesta suite, e cada commit subsequente EXPIRA os objetos anteriores
    da sessao — o id puro sobrevive ao proximo commit e ao db.close()."""
    f = Funnel(nome=f"{nome} {_RUN}", etapas=[{"id": "novo", "nome": "Novo"}], is_active=True)
    db.add(f)
    db.commit()
    db.refresh(f)
    fid = f.id
    _criados["funnels"].append(fid)
    return fid


def _criar_lead(db, nome):
    lead = Lead(nome=f"{nome} {_RUN}")
    db.add(lead)
    db.commit()
    db.refresh(lead)
    lid = lead.id
    _criados["leads"].append(lid)
    return lid


def _limpar():
    """Apaga so as linhas criadas por esta execucao (banco pode ser compartilhado)."""
    db = SessionLocal()
    try:
        if _criados["leads"]:
            db.query(FunnelEntry).filter(FunnelEntry.lead_id.in_(_criados["leads"])) \
                .delete(synchronize_session=False)
            db.query(LeadHistory).filter(LeadHistory.lead_id.in_(_criados["leads"])) \
                .delete(synchronize_session=False)
            db.query(Lead).filter(Lead.id.in_(_criados["leads"])).delete(synchronize_session=False)
        if _criados["funnels"]:
            db.query(Funnel).filter(Funnel.id.in_(_criados["funnels"])).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


try:
    # ─── 1. Sequencial: contrato documentado (409 do caminho check-then-act) ───
    print(f"1) sequencial (DATABASE_URL={os.environ['DATABASE_URL'].split('@')[-1]}): "
          "adicionar o mesmo lead 2x -> 201 depois 409")
    db = SessionLocal()
    funil_seq = _criar_funil(db, "Race Seq")
    lead_seq = _criar_lead(db, "Lead Seq")
    db.close()

    r1 = client.post(f"/api/pipeline/funnels/{funil_seq}/leads",
                      json={"lead_id": lead_seq, "etapa_id": "novo"})
    check(r1.status_code == 201, f"1a adicao -> 201 (obteve {r1.status_code}: {r1.text})")

    r2 = client.post(f"/api/pipeline/funnels/{funil_seq}/leads",
                      json={"lead_id": lead_seq, "etapa_id": "novo"})
    check(r2.status_code == 409, f"2a adicao (mesmo lead) -> 409, nunca 500 (obteve {r2.status_code})")
    SEQUENTIAL_409_BODY = r2.text
    check(r2.json() == {"detail": "Lead já está neste funil"},
          f"corpo do 409 sequencial e o contrato documentado (obteve {r2.json()})")

    with SessionLocal() as db:
        count_seq = db.query(FunnelEntry).filter(
            FunnelEntry.lead_id == lead_seq, FunnelEntry.funnel_id == funil_seq,
        ).count()
    check(count_seq == 1, f"so 1 FunnelEntry sobrevive ao caso sequencial (tem {count_seq})")

    # ─── 2. Concorrente: DUAS threads, DUAS sessoes, mesma (lead_id, funnel_id) ───
    print("\n2) concorrente: 2 threads/2 sessoes, mesma (lead_id, funnel_id)")
    db = SessionLocal()
    funil_par = _criar_funil(db, "Race Par")
    lead_par = _criar_lead(db, "Lead Par")
    db.close()

    barreira = threading.Barrier(2)
    resultados = [None, None]
    erros = [None, None]

    def _tentar(i):
        try:
            barreira.wait(timeout=10)  # as duas threads disparam o POST juntas
            resultados[i] = client.post(
                f"/api/pipeline/funnels/{funil_par}/leads",
                json={"lead_id": lead_par, "etapa_id": "novo"},
            )
        except Exception as exc:  # noqa: BLE001 — captura para reportar, nao mascarar
            erros[i] = exc

    t1 = threading.Thread(target=_tentar, args=(0,), daemon=True)
    t2 = threading.Thread(target=_tentar, args=(1,), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)

    check(erros == [None, None], f"nenhuma excecao nas threads (obteve {erros})")
    check(resultados[0] is not None and resultados[1] is not None,
          "as duas threads terminaram dentro do timeout")

    codigos = sorted(r.status_code for r in resultados if r is not None)
    print(f"     respostas: {codigos}")
    check(500 not in codigos, f"nenhuma resposta 500 (obteve {codigos})")
    check(codigos == [201, 409], f"respostas sao exatamente [201, 409] (obteve {codigos})")

    perdedora = next((r for r in resultados if r is not None and r.status_code == 409), None)
    if perdedora is not None:
        check(perdedora.text == SEQUENTIAL_409_BODY,
              "409 da corrida e byte-a-byte igual ao 409 sequencial "
              "(quem chama nao distingue qual ramo respondeu)")

    with SessionLocal() as db:
        count_par = db.query(FunnelEntry).filter(
            FunnelEntry.lead_id == lead_par, FunnelEntry.funnel_id == funil_par,
        ).count()
    check(count_par == 1, f"so 1 FunnelEntry sobrevive a corrida (tem {count_par})")

finally:
    main.app.dependency_overrides.clear()
    _limpar()

if falhas:
    print(f"\n{len(falhas)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DA CORRIDA DO FUNIL PASSARAM")
