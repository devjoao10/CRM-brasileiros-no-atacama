# -*- coding: utf-8 -*-
"""
PERF-PIPE-01 — board paginado por etapa.

Abrir um funil deixou de carregar todos os cards. O caminho novo e:
  GET /api/pipeline/board/{id}/meta                 -> esqueleto, ZERO cards
  GET /api/pipeline/board/{id}/stage/{etapa_id}     -> uma pagina de cards
  GET /api/pipeline/locate/{lead_id}                -> onde o lead esta

Este arquivo cobre o comportamento funcional E o guard de performance: com 300
cards numa etapa, a primeira pagina precisa continuar devolvendo 30 e o numero
de queries NAO pode crescer com a quantidade de cards.

ORDENACAO testada: (updated_at DESC, id DESC). O keyset exige desempate
deterministico — sem o `id`, cards com o mesmo timestamp duplicariam ou
sumiriam entre paginas.

Rodar:  python tests/test_pipeline_stage_pagination.py
   ou:  python -m pytest tests/test_pipeline_stage_pagination.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import os  # noqa: E402

pathlib.Path("scratch").mkdir(exist_ok=True)
os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": "sqlite:///./scratch/pipeline_pagination.db",
    "SEED_INITIAL_ADMIN": "true",
    "ADMIN_INITIAL_EMAIL": "admin@local.test",
    "ADMIN_INITIAL_PASSWORD": "LocalSmoke123!",
    "GEMINI_API_KEY": "",
})

N_GRANDE = 300          # etapa "cheia": prova paginacao e guard de performance
N_PEQUENA = 5           # etapa pequena: prova has_more=False
N_EMPATE = 90           # etapa com updated_at IDENTICO: prova o tie-breaker
TETO_QUERIES = 20       # constante: nao pode escalar com o nº de cards

_CACHE = {}


def _setup():
    """Banco descartavel + seed. Devolve (client, ctx) com ids uteis."""
    if _CACHE:
        return _CACHE["client"], _CACHE["ctx"]

    db_file = pathlib.Path("scratch/pipeline_pagination.db")
    if db_file.exists():
        try:
            db_file.unlink()
        except PermissionError:
            pass

    from datetime import datetime, timedelta, timezone
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import SessionLocal, Base, engine
    from app.models.lead import Lead
    from app.models.tag import Tag
    from app.models.pipeline import Funnel, FunnelEntry

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        tag = Tag(nome="Quente", cor="#f00")
        db.add(tag)
        funnel = Funnel(nome="Funil Paginado", etapas=[
            {"id": "cheia", "nome": "Etapa Cheia"},
            {"id": "pequena", "nome": "Etapa Pequena"},
            {"id": "vazia", "nome": "Etapa Vazia"},
            {"id": "empatada", "nome": "Etapa Empatada"},
        ])
        db.add(funnel)
        db.commit()
        db.refresh(funnel)

        agora = datetime.now(timezone.utc)
        leads = []
        for i in range(N_GRANDE + N_PEQUENA + N_EMPATE):
            leads.append(Lead(nome=f"Lead {i:04d}", email=f"l{i}@t.local",
                              whatsapp=f"+5551{i:09d}", destinos=["Atacama"]))
        db.add_all(leads)
        db.commit()

        todos = db.query(Lead).order_by(Lead.id).all()
        # um lead com nome/telefone distintos para os testes de busca
        alvo_busca = todos[7]
        alvo_busca.nome = "Mariana Buscavel"
        alvo_busca.whatsapp = "+5551987654321"
        alvo_busca.tags.append(tag)

        entries = []
        for i, lead in enumerate(todos[:N_GRANDE]):
            e = FunnelEntry(lead_id=lead.id, funnel_id=funnel.id,
                            etapa_id="cheia", posicao=i)
            # updated_at decrescente: entry i mais NOVO que i+1 ->
            # a ordem esperada da pagina 1 e exatamente todos[:limit]
            e.updated_at = agora - timedelta(minutes=i)
            entries.append(e)
        for i, lead in enumerate(todos[N_GRANDE:N_GRANDE + N_PEQUENA]):
            e = FunnelEntry(lead_id=lead.id, funnel_id=funnel.id,
                            etapa_id="pequena", posicao=i)
            e.updated_at = agora - timedelta(minutes=i)
            entries.append(e)
        # TODAS com o MESMO updated_at: sem desempate por id, o keyset
        # duplicaria ou perderia cards entre paginas.
        for i, lead in enumerate(todos[N_GRANDE + N_PEQUENA:]):
            e = FunnelEntry(lead_id=lead.id, funnel_id=funnel.id,
                            etapa_id="empatada", posicao=i)
            e.updated_at = agora
            entries.append(e)
        db.add_all(entries)
        db.commit()

        ctx = {
            "funnel_id": funnel.id,
            "lead_busca_id": alvo_busca.id,
            # lead bem no fim da etapa cheia: fora de qualquer primeira pagina
            "lead_fundo_id": todos[N_GRANDE - 1].id,
            "lead_topo_id": todos[0].id,
        }
    finally:
        db.close()

    client = TestClient(app)
    client.__enter__()
    r = client.post("/api/auth/login",
                    json={"email": "admin@local.test", "password": "LocalSmoke123!"})
    assert r.status_code == 200, f"login falhou: {r.status_code}"

    _CACHE["client"], _CACHE["ctx"] = client, ctx
    return client, ctx


def _stage_url(ctx, etapa="cheia", **params):
    base = f"/api/pipeline/board/{ctx['funnel_id']}/stage/{etapa}"
    if params:
        base += "?" + "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    return base


def _get(client, url):
    r = client.get(url)
    assert r.status_code == 200, f"{url} -> {r.status_code}: {r.text[:200]}"
    return r.json()


# ── META ──────────────────────────────────────────────────────────────────

def test_meta_traz_etapas_totais_e_nenhum_card():
    client, ctx = _setup()
    body = _get(client, f"/api/pipeline/board/{ctx['funnel_id']}/meta")

    assert [s["id"] for s in body["stages"]] == ["cheia", "pequena", "vazia", "empatada"], \
        "ordem das etapas precisa seguir funnel.etapas"
    totais = {s["id"]: s["total"] for s in body["stages"]}
    assert totais == {"cheia": N_GRANDE, "pequena": N_PEQUENA, "vazia": 0, "empatada": N_EMPATE}, \
        f"totais errados: {totais}"
    assert body["total_leads"] == N_GRANDE + N_PEQUENA + N_EMPATE

    # o ponto do PR: meta NAO pode conter cards
    bruto = str(body)
    assert "entry_id" not in bruto, "meta esta devolvendo cards — era so o esqueleto"
    for s in body["stages"]:
        assert "leads" not in s, "meta nao pode trazer a lista de cards"


# ── PAGINACAO ─────────────────────────────────────────────────────────────

def test_primeira_pagina_respeita_limit_e_traz_cursor():
    client, ctx = _setup()
    body = _get(client, _stage_url(ctx, limit=30))

    assert len(body["items"]) == 30, f"esperava 30 cards, veio {len(body['items'])}"
    assert body["total"] == N_GRANDE, f"total deve ser o da etapa: {body['total']}"
    assert body["has_more"] is True
    assert body["next_cursor"], "primeira pagina precisa devolver next_cursor"


def test_default_do_limit_e_30():
    client, ctx = _setup()
    body = _get(client, _stage_url(ctx))
    assert len(body["items"]) == 30, "o default de limit precisa ser 30"


def test_paginacao_nao_duplica_nem_perde_cards():
    client, ctx = _setup()
    vistos, cursor, paginas = [], None, 0

    while True:
        body = _get(client, _stage_url(ctx, limit=30, cursor=cursor))
        vistos.extend(i["entry_id"] for i in body["items"])
        paginas += 1
        if not body["has_more"]:
            break
        cursor = body["next_cursor"]
        assert paginas < 50, "loop infinito na paginacao"

    assert len(vistos) == N_GRANDE, \
        f"perdeu ou repetiu cards: {len(vistos)} de {N_GRANDE} em {paginas} paginas"
    assert len(set(vistos)) == len(vistos), "cursor devolveu entry_id DUPLICADO"
    assert paginas == 10, f"esperava 10 paginas de 30, veio {paginas}"


def test_paginacao_com_updated_at_empatado_nao_duplica():
    """Sem desempate por id, cards com o MESMO timestamp somem ou repetem."""
    client, ctx = _setup()
    vistos, cursor, paginas = [], None, 0
    while True:
        body = _get(client, _stage_url(ctx, etapa="empatada", limit=30, cursor=cursor))
        vistos.extend(i["entry_id"] for i in body["items"])
        paginas += 1
        if not body["has_more"]:
            break
        cursor = body["next_cursor"]
        assert paginas < 30, "loop infinito: cursor nao avanca com timestamps iguais"
    assert len(set(vistos)) == len(vistos), (
        "cursor DUPLICOU cards com updated_at igual "
        "(%d lidos, %d unicos)" % (len(vistos), len(set(vistos)))
    )
    assert len(vistos) == N_EMPATE, (
        "cursor PERDEU cards com updated_at igual: %d de %d" % (len(vistos), N_EMPATE)
    )


def test_ultima_pagina_tem_has_more_false():
    client, ctx = _setup()
    body = _get(client, _stage_url(ctx, etapa="pequena", limit=30))
    assert len(body["items"]) == N_PEQUENA
    assert body["has_more"] is False, "etapa menor que o limit nao tem proxima pagina"
    assert body["next_cursor"] is None


def test_ordenacao_e_estavel_entre_chamadas():
    client, ctx = _setup()
    a = [i["entry_id"] for i in _get(client, _stage_url(ctx, limit=30))["items"]]
    b = [i["entry_id"] for i in _get(client, _stage_url(ctx, limit=30))["items"]]
    assert a == b, "mesma consulta devolveu ordem diferente — ordenacao instavel"


# ── FILTROS ───────────────────────────────────────────────────────────────

def test_busca_por_nome():
    client, ctx = _setup()
    body = _get(client, _stage_url(ctx, q="Mariana"))
    assert body["total"] == 1, f"busca por nome deveria achar 1, veio {body['total']}"
    assert body["items"][0]["lead_id"] == ctx["lead_busca_id"]


def test_busca_por_telefone():
    client, ctx = _setup()
    body = _get(client, _stage_url(ctx, q="987654321"))
    assert body["total"] == 1, f"busca por telefone deveria achar 1, veio {body['total']}"
    assert body["items"][0]["lead_id"] == ctx["lead_busca_id"]


def test_busca_sem_resultado():
    client, ctx = _setup()
    body = _get(client, _stage_url(ctx, q="zzzznaoexistezzzz"))
    assert body["total"] == 0 and body["items"] == []
    assert body["has_more"] is False


def test_filtros_de_periodo():
    client, ctx = _setup()
    # o seed espaca as entries em minutos, entao TODAS caem dentro de "hoje"
    for periodo in ("hoje", "3d", "7d", "30d"):
        body = _get(client, _stage_url(ctx, periodo=periodo, limit=30))
        assert body["total"] == N_GRANDE, \
            f"periodo={periodo} deveria casar as {N_GRANDE} entries recentes, veio {body['total']}"
    # periodo desconhecido nao pode filtrar silenciosamente
    body = _get(client, _stage_url(ctx, periodo="seculo", limit=30))
    assert body["total"] == N_GRANDE


def test_filtro_de_uma_etapa_nao_afeta_a_outra():
    client, ctx = _setup()
    filtrada = _get(client, _stage_url(ctx, etapa="cheia", q="Mariana"))
    vizinha = _get(client, _stage_url(ctx, etapa="pequena"))
    assert filtrada["total"] == 1
    assert vizinha["total"] == N_PEQUENA, "o filtro da etapa A vazou para a etapa B"


# ── DEEP-LINK ─────────────────────────────────────────────────────────────

def test_locate_devolve_funil_e_etapa_sem_carregar_board():
    client, ctx = _setup()
    body = _get(client, f"/api/pipeline/locate/{ctx['lead_fundo_id']}")
    assert body["funnel_id"] == ctx["funnel_id"]
    assert body["etapa_id"] == "cheia"
    assert body["etapa_nome"] == "Etapa Cheia"
    assert body["entry_id"] > 0


def test_locate_404_para_lead_fora_de_funil():
    client, _ = _setup()
    r = client.get("/api/pipeline/locate/999999")
    assert r.status_code == 404


def test_card_alvo_fora_da_primeira_pagina_vem_em_target():
    client, ctx = _setup()
    alvo = ctx["lead_fundo_id"]

    # sem include_lead_id: o alvo NAO esta na primeira pagina
    normal = _get(client, _stage_url(ctx, limit=30))
    assert all(i["lead_id"] != alvo for i in normal["items"]), \
        "o seed deveria deixar este lead longe da primeira pagina"

    # com include_lead_id: continua 30 itens + o alvo em `target`
    body = _get(client, _stage_url(ctx, limit=30, include_lead_id=alvo))
    assert len(body["items"]) == 30, \
        f"include_lead_id nao pode inflar a pagina (veio {len(body['items'])})"
    assert body["target"] is not None, "o card alvo deveria vir em `target`"
    assert body["target"]["lead_id"] == alvo


def test_alvo_dentro_da_primeira_pagina_nao_duplica_em_target():
    client, ctx = _setup()
    body = _get(client, _stage_url(ctx, limit=30, include_lead_id=ctx["lead_topo_id"]))
    assert any(i["lead_id"] == ctx["lead_topo_id"] for i in body["items"])
    assert body["target"] is None, "alvo ja presente na pagina nao deve repetir em target"


# ── DRAG / MOVE ───────────────────────────────────────────────────────────

def test_mover_lead_coloca_no_topo_da_etapa_destino():
    client, ctx = _setup()
    origem = _get(client, _stage_url(ctx, etapa="cheia", limit=1))
    entry_id = origem["items"][0]["entry_id"]
    lead_id = origem["items"][0]["lead_id"]
    total_origem = origem["total"]
    total_destino = _get(client, _stage_url(ctx, etapa="vazia"))["total"]

    r = client.put(f"/api/pipeline/entries/{entry_id}/move",
                   json={"etapa_id": "vazia"})
    assert r.status_code == 200, f"move falhou: {r.status_code} {r.text[:200]}"

    destino = _get(client, _stage_url(ctx, etapa="vazia", limit=30))
    assert destino["total"] == total_destino + 1, "destino nao contabilizou o card"
    assert destino["items"][0]["lead_id"] == lead_id, \
        "lead movido precisa aparecer no TOPO da etapa destino"

    nova_origem = _get(client, _stage_url(ctx, etapa="cheia", limit=1))
    assert nova_origem["total"] == total_origem - 1, "origem nao decrementou"

    # devolve para nao contaminar os demais testes
    client.put(f"/api/pipeline/entries/{entry_id}/move", json={"etapa_id": "cheia"})


# ── GUARD DE PERFORMANCE ─────────────────────────────────────────────────

def test_pagina_nao_escala_com_o_numero_de_cards():
    """Com 300 cards na etapa, a pagina continua com 30 e as queries constantes."""
    from sqlalchemy import event
    from app.database import engine

    client, ctx = _setup()
    contador = []

    def _ouvir(conn, cursor, stmt, params, ctx_, many):
        contador.append(stmt)

    event.listen(engine, "before_cursor_execute", _ouvir)
    try:
        body = _get(client, _stage_url(ctx, limit=30))
    finally:
        event.remove(engine, "before_cursor_execute", _ouvir)

    assert len(body["items"]) == 30, (
        f"a etapa tem {N_GRANDE} cards mas a resposta trouxe {len(body['items'])} — "
        f"o limit sumiu e o board voltou a carregar tudo?"
    )
    assert len(contador) <= TETO_QUERIES, (
        f"{len(contador)} queries para 30 cards de uma etapa com {N_GRANDE} "
        f"(teto {TETO_QUERIES}) — N+1 por card de volta?"
    )
    # Contar itens da resposta NAO basta: da para buscar a etapa inteira do
    # banco e fatiar em Python que o corpo continua com 30. O corte tem que
    # estar no SQL.
    sel = [q for q in contador
           if "funnel_entries" in q.lower()
           and q.lower().lstrip().startswith("select")
           and "count(" not in q.lower()]
    assert sel, "nenhum SELECT em funnel_entries foi capturado"
    assert any("limit" in q.lower() for q in sel), (
        "o SELECT da etapa saiu SEM LIMIT: o banco devolveu a etapa inteira e o "
        "corte virou fatia em Python. A paginacao precisa acontecer no SQL."
    )


def test_meta_tambem_nao_escala_com_cards():
    from sqlalchemy import event
    from app.database import engine

    client, ctx = _setup()
    contador = []

    def _ouvir(conn, cursor, stmt, params, ctx_, many):
        contador.append(stmt)

    event.listen(engine, "before_cursor_execute", _ouvir)
    try:
        _get(client, f"/api/pipeline/board/{ctx['funnel_id']}/meta")
    finally:
        event.remove(engine, "before_cursor_execute", _ouvir)

    assert len(contador) <= TETO_QUERIES, (
        f"meta usou {len(contador)} queries (teto {TETO_QUERIES}) — "
        f"virou um COUNT por etapa em vez de um GROUP BY?"
    )


# ── COMPATIBILIDADE ──────────────────────────────────────────────────────

def test_board_antigo_continua_funcionando():
    client, ctx = _setup()
    body = _get(client, f"/api/pipeline/board/{ctx['funnel_id']}")
    assert "stages" in body and "funnel" in body and "total_leads" in body
    cards = sum(len(s["leads"]) for s in body["stages"])
    assert cards == N_GRANDE + N_PEQUENA + N_EMPATE, "o endpoint antigo mudou de contrato"
    card = body["stages"][0]["leads"][0]
    for campo in ("entry_id", "lead_id", "nome", "tags", "etapa_id", "entry_updated_at"):
        assert campo in card, f"campo '{campo}' sumiu do board antigo"


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
