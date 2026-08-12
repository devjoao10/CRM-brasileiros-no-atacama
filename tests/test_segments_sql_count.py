# -*- coding: utf-8 -*-
"""
SEG-SQL-01 — contagem de segmentacoes resolvida no banco.

Antes: os filtros com `campo_chave` eram aplicados em Python. _count_segment_leads
carregava TODO Lead que passasse nos demais filtros e varria o dict
campos_personalizados. Com 19 mil leads e 2 segmentos desse tipo, GET
/api/segments hidratava 22.167 objetos ORM (medido) e a tela parecia travada.

Depois: predicado EXISTS sobre os pares chave/valor do JSON, no banco.

O ORACULO deste arquivo e o algoritmo ANTIGO reimplementado em Python
(_oraculo_campo_personalizado / _oraculo). Cada filtro e resolvido pelos dois
caminhos e os CONJUNTOS DE IDs sao comparados — nao so o total. Um teste que
so conferisse `lead_count` passaria com um predicado que contasse certo por
acaso.

A semente e adversarial de proposito: chave com espaco/caixa diferente, valor
com % e _ (curinga de LIKE), numero, booleano, null, chave ausente, dict
vazio, e campos_personalizados legado que NAO e objeto.

DIVERGENCIAS CONHECIDAS E DELIBERADAS: ver os tres testes
test_divergencia_* no fim. Estao fixadas em teste para ninguem "consertar"
sem querer, e estao reportadas no PR.

Rodar:  python tests/test_segments_sql_count.py
   ou:  python -m pytest tests/test_segments_sql_count.py
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

pathlib.Path("scratch").mkdir(exist_ok=True)
os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": "sqlite:///./scratch/segments_sql_count.db",
    "SEED_INITIAL_ADMIN": "true",
    "ADMIN_INITIAL_EMAIL": "admin@local.test",
    "ADMIN_INITIAL_PASSWORD": "LocalSmoke123!",
    "GEMINI_API_KEY": "",
})

from sqlalchemy import event, select, func  # noqa: E402

FONTE = pathlib.Path("app/routers/segments.py").read_text(encoding="utf-8")
_C = {}


# ─────────────────────────────────────────────────────────────────────────
# Semente adversarial
# ─────────────────────────────────────────────────────────────────────────

# (nome, campos_personalizados, destinos, status, resp?, tag?, chegada)
SEMENTE = [
    ("plain",        {"origem": "Instagram"},              ["Atacama"], "venda",         1, 1, "2026-03-01"),
    ("chave-caixa",  {"Origem": "instagram"},              ["Uyuni"],   "em_negociacao", 1, 0, "2026-04-01"),
    ("chave-espaco", {"  origem  ": "INSTAGRAM"},          ["Atacama"], "venda",         0, 1, "2026-05-01"),
    ("substring",    {"origem": "meta/instagram ads"},     ["Atacama"], "perda",         0, 0, "2026-06-01"),
    ("valor-vazio",  {"origem": ""},                       ["Uyuni"],   "venda",         1, 0, "2026-07-01"),
    ("numero",       {"origem": 25},                       ["Atacama"], "venda",         0, 1, "2026-08-01"),
    ("bool",         {"origem": True},                     ["Santiago"], "venda",        1, 0, "2026-09-01"),
    ("null",         {"origem": None},                     ["Atacama"], "venda",         0, 0, "2026-10-01"),
    ("outra-chave",  {"canal": "Instagram"},               ["Uyuni"],   "venda",         1, 1, "2026-11-01"),
    ("dict-vazio",   {},                                   ["Atacama"], "venda",         0, 0, "2026-12-01"),
    ("pct",          {"origem": "100% organico"},          ["Atacama"], "venda",         1, 0, "2026-01-15"),
    ("underscore",   {"origem": "a_b"},                    ["Uyuni"],   "venda",         0, 1, "2026-02-15"),
    ("lista",        {"origem": ["a", "b"]},               ["Atacama"], "venda",         1, 0, "2026-03-15"),
    ("acento",       {"origem": "Indicacao"},              ["Santiago"], "perda",        0, 0, "2026-04-15"),
    ("dup-chave",    {"Origem": "facebook", "origem  ": "instagram"}, ["Atacama"], "venda", 1, 1, "2026-05-15"),
]

# campos_personalizados legado que nao e objeto JSON — jsonb_each_text estoura.
# Ficam num destino/status proprios para nao entrarem nos demais filtros: o
# LeadResponse nao valida campos_personalizados nao-dict (ver relatorio do PR,
# achado pre-existente), entao preview/leads com eles no conjunto quebraria por
# um motivo que nao tem nada a ver com a contagem.
LEGADOS = [("legado-lista", ["a", "b"]), ("legado-texto", "sou string")]

FILTROS = {
    "campo-so-chave":     {"campo_chave": "origem"},
    "campo-chave-valor":  {"campo_chave": "origem", "campo_valor": "instagram"},
    "campo-chave-caixa":  {"campo_chave": " ORIGEM ", "campo_valor": "INSTA"},
    "campo-pct":          {"campo_chave": "origem", "campo_valor": "100%"},
    "campo-underscore":   {"campo_chave": "origem", "campo_valor": "a_b"},
    "campo-numero":       {"campo_chave": "origem", "campo_valor": "25"},
    "campo-bool":         {"campo_chave": "origem", "campo_valor": "true"},
    "campo-inexistente":  {"campo_chave": "nao_existe"},
    "campo-zero-result":  {"campo_chave": "origem", "campo_valor": "zzzzz"},
    "destino":            {"destino": "Atacama"},
    "status":             {"status_venda": "venda"},
    "responsavel":        {"responsavel_id": 1},
    "responsavel-ia":     {"responsavel_id": 0},
    "tags":               {"tag_ids": [1], "tag_mode": "any"},
    "datas":              {"data_chegada_de": "2026-03-01", "data_chegada_ate": "2026-08-01"},
    "comb-destino-campo": {"destino": "Atacama", "campo_chave": "origem", "campo_valor": "insta"},
    "comb-tudo":          {"destino": "Atacama", "status_venda": "venda", "responsavel_id": 1,
                           "campo_chave": "origem"},
    "comb-tag-campo":     {"tag_ids": [1], "campo_chave": "origem", "campo_valor": "insta"},
}


def _setup():
    if _C:
        return _C["client"], _C["dados"]

    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import SessionLocal, Base, engine
    from app.models.lead import Lead
    from app.models.tag import Tag, lead_tags
    from app.models.user import User
    from datetime import date

    db_file = pathlib.Path("scratch/segments_sql_count.db")
    if db_file.exists():
        try:
            db_file.unlink()
        except PermissionError:
            pass

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        u = User(nome="Resp", email="resp@local.test", hashed_password="x")
        # 2o responsavel so para os leads legados: assim eles ficam fora de
        # TODOS os filtros da comparacao (inclusive responsavel_id=0) e nunca
        # chegam ao preview, que nao os serializa (achado pre-existente).
        u2 = User(nome="Resp2", email="resp2@local.test", hashed_password="x")
        db.add_all([u, u2])
        t = Tag(nome="VIP", cor="#fff")
        db.add(t)
        db.commit()

        dados = []
        for nome, cp, dest, status, resp, tag, chegada in SEMENTE:
            l = Lead(nome=nome, campos_personalizados=cp, destinos=dest,
                     status_venda=status, is_active=True,
                     responsavel_id=u.id if resp else None,
                     data_chegada=date.fromisoformat(chegada))
            db.add(l)
            dados.append((l, tag))
        for nome, cp in LEGADOS:
            l = Lead(nome=nome, campos_personalizados=cp, destinos=["Legado"],
                     status_venda="legado", is_active=True, responsavel_id=u2.id,
                     data_chegada=date(2026, 12, 20))
            db.add(l)
            dados.append((l, 0))
        db.commit()

        marcados = [(l.id, t.id) for l, tag in dados if tag]
        if marcados:
            db.execute(lead_tags.insert(),
                       [{"lead_id": lid, "tag_id": tid} for lid, tid in marcados])
            db.commit()

        # snapshot em dict puro: o oraculo nao pode depender do ORM
        snapshot = []
        for l, tag in dados:
            snapshot.append({
                "id": l.id, "nome": l.nome, "cp": l.campos_personalizados,
                "destinos": l.destinos, "status": l.status_venda,
                "responsavel_id": l.responsavel_id, "tag": bool(tag),
                "chegada": l.data_chegada.isoformat(),
            })
        # o responsavel_id real do usuario semeado
        resp_id = u.id
    finally:
        db.close()

    client = TestClient(app)
    client.__enter__()
    r = client.post("/api/auth/login",
                    json={"email": "admin@local.test", "password": "LocalSmoke123!"})
    assert r.status_code == 200, f"login: {r.status_code} {r.text}"
    _C["client"], _C["dados"] = client, {"leads": snapshot, "resp_id": resp_id}
    return client, _C["dados"]


# ─────────────────────────────────────────────────────────────────────────
# Oraculo: o algoritmo ANTIGO, reimplementado
# ─────────────────────────────────────────────────────────────────────────

def _oraculo_campo_personalizado(cp, chave, valor):
    """Copia fiel do bloco Python que existia em _count_segment_leads."""
    if not isinstance(cp, dict):
        # o codigo antigo fazia `cp = lead.campos_personalizados or {}` e
        # iterava; para nao-dict o resultado pratico era nao casar
        return False
    chave_lower = (chave or "").strip().lower()
    valor_lower = (valor or "").strip().lower()
    match_key = next((k for k in cp if k.strip().lower() == chave_lower), None)
    if match_key is None:
        return False
    return not valor_lower or valor_lower in str(cp[match_key]).lower()


def _oraculo(leads, filtros, resp_id):
    """Conjunto de ids esperado, calculado em Python puro."""
    out = set()
    for l in leads:
        if "destino" in filtros and filtros["destino"] not in (l["destinos"] or []):
            continue
        if "status_venda" in filtros and l["status"] != filtros["status_venda"]:
            continue
        if "responsavel_id" in filtros:
            alvo = filtros["responsavel_id"]
            esperado = None if alvo == 0 else resp_id
            if l["responsavel_id"] != esperado:
                continue
        if "tag_ids" in filtros and not l["tag"]:
            continue
        if "data_chegada_de" in filtros and l["chegada"] < filtros["data_chegada_de"]:
            continue
        if "data_chegada_ate" in filtros and l["chegada"] > filtros["data_chegada_ate"]:
            continue
        if "campo_chave" in filtros:
            if not _oraculo_campo_personalizado(
                    l["cp"], filtros["campo_chave"], filtros.get("campo_valor")):
                continue
        out.add(l["id"])
    return out


def _sem_chave_duplicada(ids, leads):
    """
    Tira da comparacao o UNICO lead cuja divergencia e conhecida: o que tem
    "Origem" e "origem  " (normalizam igual) com valores diferentes. O oraculo
    parava na primeira chave; o EXISTS aceita qualquer uma. Coberto a parte em
    test_divergencia_chave_duplicada_casa_qualquer_uma.
    """
    dup = {l["id"] for l in leads if l["nome"] == "dup-chave"}
    return set(ids) - dup


# ─────────────────────────────────────────────────────────────────────────
# 1. Equivalencia semantica: conjuntos de ids, nao so totais
# ─────────────────────────────────────────────────────────────────────────

def _ids_do_preview(client, filtros):
    r = client.post("/api/segments/preview?limit=500", json=filtros)
    assert r.status_code == 200, f"preview {filtros}: {r.status_code} {r.text}"
    body = r.json()
    return {l["id"] for l in body["leads"]}, body["total"]


def test_conjuntos_batem_com_o_algoritmo_antigo():
    client, d = _setup()
    problemas = []
    for nome, filtros in FILTROS.items():
        esperado = _sem_chave_duplicada(
            _oraculo(d["leads"], filtros, d["resp_id"]), d["leads"])
        obtido = _sem_chave_duplicada(_ids_do_preview(client, filtros)[0], d["leads"])
        if obtido != esperado:
            problemas.append(
                f"{nome}: esperado {sorted(esperado)}, obtido {sorted(obtido)}; "
                f"sobrando {sorted(obtido - esperado)}, "
                f"faltando {sorted(esperado - obtido)}"
            )
    assert not problemas, (
        f"conjuntos divergiram em {len(problemas)}/{len(FILTROS)} filtros:\n  "
        + "\n  ".join(problemas))


def test_count_bate_com_o_conjunto_em_todos_os_filtros():
    """lead_count precisa ser exatamente |conjunto| — inclusive com tags."""
    from app.database import SessionLocal
    from app.routers.segments import _count_segment_leads
    client, d = _setup()
    db = SessionLocal()
    try:
        problemas = []
        for nome, filtros in FILTROS.items():
            ids, _ = _ids_do_preview(client, filtros)
            n = _count_segment_leads(filtros, db)
            if n != len(ids):
                problemas.append(f"{nome}: count={n} mas o conjunto tem {len(ids)}")
        assert not problemas, "count != conjunto:\n  " + "\n  ".join(problemas)
    finally:
        db.close()


def test_tags_nao_multiplicam_a_contagem():
    """
    Guarda de COUNT DISTINCT: se alguem trocar a subquery de tags por JOIN,
    um lead com 2 tags passa a contar 2x. A semente tem lead com tag.
    """
    from app.database import SessionLocal
    from app.models.lead import Lead
    from app.models.tag import Tag, lead_tags
    from app.routers.segments import _count_segment_leads
    client, d = _setup()
    db = SessionLocal()
    try:
        t2 = db.query(Tag).filter(Tag.nome == "VIP2").first()
        if not t2:
            t2 = Tag(nome="VIP2", cor="#000")
            db.add(t2)
            db.commit()
        com_tag = [l["id"] for l in d["leads"] if l["tag"]]
        ja = {r[0] for r in db.query(lead_tags.c.lead_id)
              .filter(lead_tags.c.tag_id == t2.id).all()}
        novos = [i for i in com_tag if i not in ja]
        if novos:
            db.execute(lead_tags.insert(),
                       [{"lead_id": i, "tag_id": t2.id} for i in novos])
            db.commit()
        t1 = db.query(Tag).filter(Tag.nome == "VIP").first()
        n = _count_segment_leads({"tag_ids": [t1.id, t2.id], "tag_mode": "any"}, db)
        assert n == len(com_tag), (
            f"leads com 2 tags contados em duplicidade: {n} != {len(com_tag)}"
        )
    finally:
        db.close()


def test_segmento_legado_nao_derruba_a_listagem():
    """
    campos_personalizados que nao e objeto (lista/string) existia no banco.
    jsonb_each_text estoura nesse caso; sem a guarda CASE, UM lead legado
    derrubaria GET /api/segments inteiro com 500.
    """
    from app.database import SessionLocal
    from app.routers.segments import _count_segment_leads
    client, d = _setup()
    legados = [l["id"] for l in d["leads"] if l["nome"].startswith("legado-")]
    assert legados, "a semente precisa ter leads legados para este teste valer"

    r = client.get("/api/segments")
    assert r.status_code == 200, (
        f"um lead com campos_personalizados nao-objeto derrubou a listagem "
        f"inteira: {r.status_code} {r.text[:200]}")

    from app.routers.segments import _resolve_segment_query
    db = SessionLocal()
    try:
        # ids diretos: robusto a leads que outros testes tenham inserido
        ids = {r[0] for r in
               _resolve_segment_query({"campo_chave": "origem"}, db,
                                      for_count=True).all()}
        for lid in legados:
            assert lid not in ids, (
                "campos_personalizados nao-objeto nao tem chave: nao pode casar")
        assert _count_segment_leads({"campo_chave": "origem"}, db) == len(ids)
    finally:
        db.close()


# ─────────────────────────────────────────────────────────────────────────
# 2. Nada de materializar Lead — guarda anti-regressao (item 13)
# ─────────────────────────────────────────────────────────────────────────

class _Contador:
    def __init__(self):
        self.queries = 0
        self.leads = 0

    def __enter__(self):
        from app.database import engine
        from app.models.lead import Lead
        self._engine, self._Lead = engine, Lead
        event.listen(engine, "before_cursor_execute", self._q)
        event.listen(Lead, "load", self._l)
        return self

    def __exit__(self, *a):
        event.remove(self._engine, "before_cursor_execute", self._q)
        event.remove(self._Lead, "load", self._l)

    def _q(self, conn, cur, stmt, params, ctx, many):
        self.queries += 1

    def _l(self, target, ctx):
        self.leads += 1


def _mede_listagem(client):
    with _Contador() as c:
        r = client.get("/api/segments")
        assert r.status_code == 200, r.text
    return c.queries, c.leads, r.json()


def test_listagem_nao_hidrata_nenhum_lead():
    """
    O nucleo da correcao. Antes: 22.167 objetos Lead para 7 segmentos.
    Depois tem que ser ZERO — a contagem inteira acontece no banco.
    """
    client, d = _setup()
    _criar_segmentos_do_guard(client)
    _, leads, _ = _mede_listagem(client)
    assert leads == 0, (
        f"{leads} objetos Lead hidratados na listagem: alguem voltou a filtrar "
        f"em Python. A contagem tem que ser COUNT no banco."
    )


def test_queries_nao_crescem_com_o_numero_de_leads():
    """
    100 leads e 500 leads, MESMOS segmentos: a query count tem que ficar
    igual. Se voltar o padrao 'lista segmentos + carrega N leads', o numero
    de leads hidratados dispara junto com a base.
    """
    client, d = _setup()
    _criar_segmentos_do_guard(client)
    from app.database import SessionLocal
    from app.models.lead import Lead
    from datetime import date

    medidas = {}
    db = SessionLocal()
    try:
        atual = db.query(Lead).count()
        for alvo in (100, 500):
            faltam = alvo - atual
            if faltam > 0:
                db.bulk_insert_mappings(Lead, [{
                    "nome": f"bulk {i}",
                    "campos_personalizados": {"origem": "Instagram"},
                    "destinos": ["Atacama"], "status_venda": "venda",
                    "is_active": True, "data_chegada": date(2026, 6, 1),
                } for i in range(faltam)])
                db.commit()
                atual = alvo
            medidas[alvo] = _mede_listagem(client)[:2]
    finally:
        # devolve a base ao estado da semente: os outros testes comparam
        # conjuntos de ids contra o snapshot e nao podem ver estes leads
        db.query(Lead).filter(Lead.nome.like("bulk %")).delete(
            synchronize_session=False)
        db.commit()
        db.close()

    q100, l100 = medidas[100]
    q500, l500 = medidas[500]
    assert q100 == q500, (
        f"queries variaram com o volume: {q100} com 100 leads, {q500} com 500. "
        f"Deve depender so da quantidade de segmentos."
    )
    assert l100 == l500 == 0, (
        f"leads hidratados cresceram com a base: {l100} -> {l500}"
    )


def _criar_segmentos_do_guard(client):
    for nome, filtros in (("guard-simples", {"is_active": True}),
                          ("guard-destino", {"destino": "Atacama"}),
                          ("guard-campo", {"campo_chave": "origem",
                                           "campo_valor": "instagram"}),
                          ("guard-tag", {"tag_ids": [1], "tag_mode": "any"}),
                          ("guard-comb", {"destino": "Atacama",
                                          "status_venda": "venda",
                                          "campo_chave": "origem"})):
        client.post("/api/segments", json={"nome": nome, "cor": "#123456",
                                           "filtros": filtros})


def test_count_usa_count_e_nao_all_mais_len():
    """Guarda estrutural que acompanha a comportamental acima."""
    corpo = FONTE.split("def _count_segment_leads", 1)[1].split("\n\n\n", 1)[0]
    codigo = "\n".join(l for l in corpo.splitlines()
                       if not l.strip().startswith("#") and '"""' not in l)
    assert ".count()" in codigo, "a contagem tem que ser COUNT no banco"
    assert ".all()" not in codigo, "carregar linhas para contar foi o bug"
    assert "len(" not in codigo, "len() de lista carregada foi o bug"
    assert "campos_personalizados" not in codigo, (
        "campo personalizado nao pode voltar a ser resolvido em Python aqui"
    )


def test_nenhum_endpoint_de_segmentos_filtra_campo_em_python():
    """
    preview e /leads tambem nao podem varrer o dict em Python. Os dois ainda
    tem um `for` de deduplicacao do joinedload — isso e outra coisa e pode
    ficar; o que nao pode e tocar em campos_personalizados.
    """
    for fn in ("get_segment_leads", "preview_segment"):
        corpo = FONTE.split(f"def {fn}", 1)[1].split("\n\n\n", 1)[0]
        assert "campos_personalizados" not in corpo, (
            f"{fn} voltou a resolver campo personalizado em Python")
        assert "needs_python" not in corpo, (
            f"{fn} voltou a ter ramo de filtragem em Python")


# ─────────────────────────────────────────────────────────────────────────
# 3. Dialect PostgreSQL (item 11)
# ─────────────────────────────────────────────────────────────────────────

def _sql(filtros, sqlite):
    from sqlalchemy.dialects.postgresql import dialect as pg
    from sqlalchemy.dialects.sqlite import dialect as lite
    from app.database import SessionLocal
    from app.routers import segments as S
    # o predicado de campo personalizado mora em app/query_filters.py (leads.py
    # usa o mesmo), entao o dialect precisa ser forcado LA tambem
    from app import query_filters as QF
    db = SessionLocal()
    try:
        orig, origQF = S.IS_SQLITE, QF.IS_SQLITE
        S.IS_SQLITE = QF.IS_SQLITE = sqlite
        try:
            q = S._resolve_segment_query(filtros, db, for_count=True)
            stmt = select(func.count()).select_from(q.subquery())
            return str(stmt.compile(dialect=lite() if sqlite else pg(),
                                    compile_kwargs={"literal_binds": True}))
        finally:
            S.IS_SQLITE, QF.IS_SQLITE = orig, origQF
    finally:
        db.close()


def test_guarda_de_tipo_json_existe_nos_dois_dialects():
    """
    campos_personalizados legado que nao e objeto: no PostgreSQL
    jsonb_each_text ESTOURA e derruba GET /api/segments inteiro. O SQLite nao
    estoura com array, entao esta guarda e invisivel para um teste de
    comportamento local — so da para fixa-la no SQL compilado.
    """
    filtros = {"campo_chave": "origem"}
    pg_sql = _sql(filtros, sqlite=False)
    assert "jsonb_typeof" in pg_sql, (
        "sem jsonb_typeof, um unico lead com campos_personalizados nao-objeto "
        "derruba a listagem inteira com 500 no PostgreSQL")
    lite_sql = _sql(filtros, sqlite=True)
    assert "json_type" in lite_sql, (
        "guarda de tipo tambem no ramo SQLite, para os dois ramos casarem")


def _sql_pg(filtros):
    """SQL compilado forcando o ramo PostgreSQL."""
    return _sql(filtros, sqlite=False)


def test_postgres_campo_personalizado_usa_jsonb_e_exists():
    sql = _sql_pg({"campo_chave": "origem", "campo_valor": "insta"})
    assert "EXISTS" in sql, "campo personalizado precisa virar EXISTS"
    assert "jsonb_each_text" in sql, "no PostgreSQL os pares saem por jsonb_each_text"
    assert "AS JSONB" in sql.upper(), "a coluna e json: precisa do cast para jsonb"
    assert "json_each(" not in sql, "json_each e a forma do SQLite, nao pode vazar"
    assert "jsonb_typeof" in sql, "sem a guarda de tipo, JSON legado derruba a query"


def test_postgres_valor_com_curinga_e_escapado():
    sql = _sql_pg({"campo_chave": "origem", "campo_valor": "100%"})
    assert "ESCAPE" in sql, (
        "% digitado pelo usuario tem que ser literal, nao curinga de LIKE"
    )


def test_postgres_destino_usa_jsonb_contains():
    sql = _sql_pg({"destino": "Atacama"})
    assert "@>" in sql and "JSONB" in sql.upper(), (
        f"destino deveria usar @> sobre jsonb: {sql[:200]}"
    )


def test_postgres_contagem_e_count_sem_join_multiplicador():
    sql = _sql_pg({"tag_ids": [1], "tag_mode": "any",
                   "campo_chave": "origem", "destino": "Atacama"})
    assert "count(" in sql.lower(), "tem que ser COUNT no banco"
    fora = sql.split("WHERE", 1)[0]
    assert " JOIN " not in fora.upper(), (
        f"a query de contagem nao pode ter JOIN (multiplica linha): {fora}"
    )
    assert "IN (SELECT" in sql.upper().replace("\n", " ") or "EXISTS" in sql, (
        "tags/funil entram por subquery, nao por JOIN"
    )


# ─────────────────────────────────────────────────────────────────────────
# 4. Fluxo real: criar, listar, editar (itens 10 e 18)
# ─────────────────────────────────────────────────────────────────────────

def test_criar_listar_editar_com_count_correto():
    client, d = _setup()
    esperado = len(_sem_chave_duplicada(
        _oraculo(d["leads"], {"campo_chave": "origem",
                              "campo_valor": "instagram"}, d["resp_id"]),
        d["leads"])) + 1   # +1: o lead de chave duplicada casa pela 2a chave

    r = client.post("/api/segments", json={
        "nome": "fluxo-crud", "cor": "#abcdef",
        "filtros": {"campo_chave": "origem", "campo_valor": "instagram"}})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["lead_count"] == esperado, (
        f"count na criacao: {r.json()['lead_count']} != {esperado}")

    lista = client.get("/api/segments").json()
    achado = next((s for s in lista["segments"] if s["id"] == sid), None)
    assert achado, "segmento criado nao apareceu na listagem"
    assert achado["lead_count"] == esperado, "count divergiu no reload"

    novo = len(_oraculo(d["leads"], {"campo_chave": "origem",
                                     "campo_valor": "meta"}, d["resp_id"]))
    assert novo != esperado, "o teste precisa de counts diferentes para valer"
    ru = client.put(f"/api/segments/{sid}", json={
        "filtros": {"campo_chave": "origem", "campo_valor": "meta"}})
    assert ru.status_code == 200, ru.text
    assert ru.json()["lead_count"] == novo, (
        f"count na edicao: {ru.json()['lead_count']} != {novo}")

    lista2 = client.get("/api/segments").json()
    achado2 = next(s for s in lista2["segments"] if s["id"] == sid)
    assert achado2["lead_count"] == novo, "count nao atualizou no reload apos edicao"
    assert achado2["filtros"]["campo_valor"] == "meta", "filtros nao atualizaram"

    client.delete(f"/api/segments/{sid}")


def test_segment_leads_devolve_o_mesmo_conjunto_do_preview():
    """Otimizar a contagem nao pode ter mexido na listagem de leads."""
    client, d = _setup()
    filtros = {"campo_chave": "origem", "campo_valor": "insta"}
    r = client.post("/api/segments", json={"nome": "fluxo-leads", "cor": "#111",
                                           "filtros": filtros})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    try:
        prev_ids, prev_total = _ids_do_preview(client, filtros)
        rl = client.get(f"/api/segments/{sid}/leads?limit=500")
        assert rl.status_code == 200, rl.text
        body = rl.json()
        assert {l["id"] for l in body["leads"]} == prev_ids, (
            "/leads e /preview divergiram — sao a mesma fonte de verdade")
        assert body["total"] == prev_total == r.json()["lead_count"], (
            f"totais divergiram: leads={body['total']} preview={prev_total} "
            f"count={r.json()['lead_count']}")
    finally:
        client.delete(f"/api/segments/{sid}")


def test_zero_resultados():
    client, d = _setup()
    ids, total = _ids_do_preview(client, {"campo_chave": "origem",
                                          "campo_valor": "zzzzz"})
    assert ids == set() and total == 0, f"deveria ser vazio: {total} {ids}"
    r = client.post("/api/segments", json={
        "nome": "vazio", "cor": "#000",
        "filtros": {"campo_chave": "origem", "campo_valor": "zzzzz"}})
    assert r.status_code == 201 and r.json()["lead_count"] == 0, r.text
    client.delete(f"/api/segments/{r.json()['id']}")


def test_get_segment_traz_count():
    client, d = _setup()
    r = client.post("/api/segments", json={"nome": "detalhe", "cor": "#222",
                                           "filtros": {"destino": "Atacama"}})
    sid = r.json()["id"]
    try:
        rg = client.get(f"/api/segments/{sid}")
        assert rg.status_code == 200
        assert rg.json()["lead_count"] == r.json()["lead_count"]
    finally:
        client.delete(f"/api/segments/{sid}")


def test_formato_da_resposta_preservado():
    """lead_count continua no payload da listagem — contrato nao mudou."""
    client, d = _setup()
    r = client.post("/api/segments", json={"nome": "contrato", "cor": "#333",
                                           "filtros": {"destino": "Atacama"}})
    assert r.status_code == 201, r.text
    self_id = r.json()["id"]
    try:
        body = client.get("/api/segments").json()
    finally:
        client.delete(f"/api/segments/{self_id}")
    assert "total" in body and "segments" in body
    assert body["segments"], "o segmento recem-criado tem que estar na listagem"
    for s in body["segments"]:
        assert "lead_count" in s and isinstance(s["lead_count"], int), (
            "lead_count nao pode sumir nem virar lazy no frontend")
        for campo in ("id", "nome", "cor", "filtros"):
            assert campo in s, f"campo {campo} sumiu do contrato"


# ─────────────────────────────────────────────────────────────────────────
# 5. Divergencias conhecidas — fixadas de proposito, reportadas no PR
# ─────────────────────────────────────────────────────────────────────────

def test_divergencia_json_null_nao_vira_a_string_none():
    """
    Python fazia str(None) -> "None", entao campo_valor="one" casava um campo
    nulo. Em SQL o valor e NULL e nao casa. Comportamento novo e o sensato;
    fica fixado para ninguem reintroduzir o antigo sem querer.
    """
    client, d = _setup()
    ids, _ = _ids_do_preview(client, {"campo_chave": "origem", "campo_valor": "one"})
    nulo = next(l["id"] for l in d["leads"] if l["nome"] == "null")
    assert nulo not in ids, "campo JSON null nao deve casar a busca por 'one'"


def test_divergencia_valor_nao_escalar_usa_json_e_nao_repr_python():
    """
    Valor lista: Python via "['a', 'b']", o banco ve ["a", "b"]. Buscar por
    'a' casa nos dois; buscar pelas aspas simples do repr Python so casava antes.
    """
    client, d = _setup()
    lista_id = next(l["id"] for l in d["leads"] if l["nome"] == "lista")
    casa, _ = _ids_do_preview(client, {"campo_chave": "origem", "campo_valor": "a"})
    assert lista_id in casa, "o conteudo do JSON continua pesquisavel"
    repr_py, _ = _ids_do_preview(client, {"campo_chave": "origem",
                                          "campo_valor": "['a'"})
    assert lista_id not in repr_py, "repr de lista Python nao e mais pesquisavel"


def test_divergencia_chave_duplicada_casa_qualquer_uma():
    """
    Um lead com "Origem" e "origem  " (normalizam igual) tinha resultado
    dependente da ORDEM do dict: Python pegava a PRIMEIRA chave e so olhava
    o valor dela. O EXISTS casa se QUALQUER uma satisfizer. Afeta apenas
    leads com chaves duplicadas apos normalizacao.
    """
    client, d = _setup()
    dup = next(l["id"] for l in d["leads"] if l["nome"] == "dup-chave")
    por_segunda, _ = _ids_do_preview(client, {"campo_chave": "origem",
                                              "campo_valor": "instagram"})
    assert dup in por_segunda, (
        "o EXISTS deve casar pela segunda chave; o codigo antigo parava na primeira"
    )
    por_primeira, _ = _ids_do_preview(client, {"campo_chave": "origem",
                                               "campo_valor": "facebook"})
    assert dup in por_primeira, "e tambem pela primeira"


ALL_TESTS = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    falhas = 0
    for fn in ALL_TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            falhas += 1
            print(f"FAIL  {fn.__name__}: {exc}")
        except Exception as exc:
            falhas += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(ALL_TESTS) - falhas}/{len(ALL_TESTS)} testes OK")
    sys.exit(1 if falhas else 0)
