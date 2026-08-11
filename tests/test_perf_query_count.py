# -*- coding: utf-8 -*-
"""
PERF-N1 — guard anti N+1 nas listagens criticas.

As respostas leem relacionamentos lazy (`lead.tags` no card do kanban,
`tags`/`responsavel`/`funnel_entries` no LeadResponse). Sem eager loading isso
vira UMA query por item:

    board com 3000 cards -> 3003 queries, ~4s
    /api/leads com 100   ->  103 queries

Este teste conta as queries realmente executadas e falha se o total voltar a
crescer com o numero de itens. O criterio e proposital: um teto CONSTANTE,
independente de quantos leads existem. Se alguem remover o eager loading, a
contagem passa do teto e o teste aponta exatamente onde.

NAO mede tempo (frágil em CI) — mede numero de queries, que e deterministico.

Rodar:  python tests/test_perf_query_count.py
   ou:  python -m pytest tests/test_perf_query_count.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import os  # noqa: E402

pathlib.Path("scratch").mkdir(exist_ok=True)
os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": "sqlite:///./scratch/perf_query_count.db",
    "SEED_INITIAL_ADMIN": "true",
    "ADMIN_INITIAL_EMAIL": "admin@local.test",
    "ADMIN_INITIAL_PASSWORD": "LocalSmoke123!",
    "GEMINI_API_KEY": "",
})

N_LEADS = 120          # > que o limit default, para o N+1 aparecer
N_ENTRIES = 120
LIMITE_LEADS = 15      # teto constante; sem eager loading passaria de 100
LIMITE_BOARD = 15      # idem; sem eager loading seria ~N_ENTRIES + 3

_READY = False


def _setup():
    """Cria o banco descartavel, semeia e devolve (client, funnel_id)."""
    global _READY
    db_file = pathlib.Path("scratch/perf_query_count.db")
    if not _READY and db_file.exists():
        try:
            db_file.unlink()
        except PermissionError:
            pass
    _READY = True

    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import SessionLocal, Base, engine
    from app.models.lead import Lead
    from app.models.tag import Tag
    from app.models.pipeline import Funnel, FunnelEntry

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(Lead).count() == 0:
            tags = [Tag(nome=f"T{i}", cor="#fff") for i in range(3)]
            db.add_all(tags)
            db.add_all([Lead(nome=f"Lead {i}", email=f"l{i}@t.local",
                             destinos=["Atacama"]) for i in range(N_LEADS)])
            db.commit()
            for lead in db.query(Lead).all():          # tags em todos: pior caso
                lead.tags.append(tags[lead.id % 3])
            funnel = Funnel(nome="Funil Perf",
                            etapas=[{"id": "e1", "nome": "Etapa 1"}])
            db.add(funnel)
            db.commit()
            db.refresh(funnel)
            ids = [row[0] for row in db.query(Lead.id).limit(N_ENTRIES).all()]
            db.add_all([FunnelEntry(lead_id=lid, funnel_id=funnel.id,
                                    etapa_id="e1", posicao=i)
                        for i, lid in enumerate(ids)])
            db.commit()
        fid = db.query(Funnel).first().id
    finally:
        db.close()

    client = TestClient(app)
    client.__enter__()          # dispara o lifespan (seed do admin)
    client.post("/api/auth/login",
                json={"email": "admin@local.test", "password": "LocalSmoke123!"})
    return client, fid


def _contar_queries(client, url):
    """Executa a request contando as queries emitidas pelo engine."""
    from sqlalchemy import event
    from app.database import engine

    contador = []

    def _ouvir(conn, cursor, stmt, params, ctx, many):
        contador.append(stmt)

    event.listen(engine, "before_cursor_execute", _ouvir)
    try:
        resp = client.get(url)
    finally:
        event.remove(engine, "before_cursor_execute", _ouvir)
    assert resp.status_code == 200, f"{url} -> {resp.status_code}: {resp.text[:200]}"
    return len(contador), resp.json()


# ── 1. /api/leads nao pode escalar com o numero de leads ──────────────────

def test_listagem_de_leads_tem_query_count_constante():
    client, _ = _setup()
    n50, body50 = _contar_queries(client, "/api/leads?limit=50")
    n100, body100 = _contar_queries(client, "/api/leads?limit=100")

    assert len(body50["leads"]) == 50, "esperava 50 leads no corpo"
    assert len(body100["leads"]) == 100, "esperava 100 leads no corpo"
    assert n50 <= LIMITE_LEADS, (
        f"N+1 em /api/leads: {n50} queries para 50 leads (teto {LIMITE_LEADS}). "
        f"Faltou eager loading de tags/responsavel/funnel_entries?"
    )
    assert n100 <= LIMITE_LEADS, (
        f"N+1 em /api/leads: {n100} queries para 100 leads (teto {LIMITE_LEADS})."
    )
    # o sinal mais forte: dobrar os itens NAO pode aumentar as queries
    assert n100 == n50, (
        f"query count varia com o numero de itens ({n50} -> {n100}): "
        f"ainda ha carga lazy por lead."
    )


# ── 2. board do kanban nao pode escalar com o numero de cards ─────────────

def test_board_do_kanban_tem_query_count_constante():
    client, fid = _setup()
    n, body = _contar_queries(client, f"/api/pipeline/board/{fid}")

    cards = sum(len(s["leads"]) for s in body["stages"])
    assert cards == N_ENTRIES, f"esperava {N_ENTRIES} cards, veio {cards}"
    assert n <= LIMITE_BOARD, (
        f"N+1 no board: {n} queries para {cards} cards (teto {LIMITE_BOARD}). "
        f"Faltou selectinload(Lead.tags) em get_kanban_board?"
    )


# ── 3. o payload nao pode mudar por causa do eager loading ───────────────

def test_payload_preserva_tags_e_funis():
    client, fid = _setup()
    _, body = _contar_queries(client, "/api/leads?limit=5")
    lead = body["leads"][0]
    for campo in ("id", "nome", "tags", "funis", "responsavel_nome"):
        assert campo in lead, f"campo '{campo}' sumiu do LeadResponse"
    assert lead["tags"], "tags vieram vazias — o eager loading quebrou o payload"

    _, board = _contar_queries(client, f"/api/pipeline/board/{fid}")
    card = board["stages"][0]["leads"][0]
    for campo in ("entry_id", "lead_id", "nome", "tags", "etapa_id"):
        assert campo in card, f"campo '{campo}' sumiu do LeadCardResponse"
    assert card["tags"], "tags do card vieram vazias"


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    failures = 0
    for fn in ALL_TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {fn.__name__}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(ALL_TESTS) - failures}/{len(ALL_TESTS)} testes OK")
    sys.exit(1 if failures else 0)
