# -*- coding: utf-8 -*-
"""
PERF-PIPE-01 — review final do PR do pipeline paginado.

Cobre o que a revisao dirigida pediu:

  1. dropdown "adicionar lead ao funil" volta a excluir quem ja esta no funil,
     agora via NOT EXISTS em SQL (a exclusao client-side dependia do board
     inteiro carregado). O 409 continua existindo contra corrida.
  2. /locate com lead em MAIS DE UM funil. Regra DECIDIDA:
       A. entry no prefer_funnel_id      -> esse funil vence
       B. sem entry la, mas ha em outro  -> localiza e abre o outro
       C. varios outros funis            -> fallback deterministico
                                            (created_at ASC, id ASC)
       D. nenhuma entry                  -> 404
  3. paridade dos filtros que migraram do JavaScript para o SQL — comparando o
     CONJUNTO de lead_id, nao apenas o status HTTP.
  4. contadores "X de Y" quando ha filtro.
  5. compilacao no dialeto PostgreSQL (producao) das queries novas.
  6. updated_at NULL nao derruba a paginacao.

Rodar:  python tests/test_pipeline_review_final.py
   ou:  python -m pytest tests/test_pipeline_review_final.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import os  # noqa: E402

pathlib.Path("scratch").mkdir(exist_ok=True)
os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": "sqlite:///./scratch/pipeline_review.db",
    "SEED_INITIAL_ADMIN": "true",
    "ADMIN_INITIAL_EMAIL": "admin@local.test",
    "ADMIN_INITIAL_PASSWORD": "LocalSmoke123!",
    "GEMINI_API_KEY": "",
})

_C = {}


def _setup():
    """Seed pequeno e explicito: cada lead existe para provar um filtro."""
    if _C:
        return _C["client"], _C["ctx"]

    from datetime import date
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import SessionLocal, Base, engine
    from app.models.lead import Lead
    from app.models.tag import Tag
    from app.models.user import User
    from app.models.pipeline import Funnel, FunnelEntry

    db_file = pathlib.Path("scratch/pipeline_review.db")
    if db_file.exists():
        try:
            db_file.unlink()
        except PermissionError:
            pass

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        t_a = Tag(nome="TagA", cor="#a00")
        t_b = Tag(nome="TagB", cor="#0a0")
        db.add_all([t_a, t_b])
        resp_user = User(nome="Vendedor", email="v@l.test",
                         hashed_password="x", role="user")
        db.add(resp_user)
        db.commit()
        db.refresh(t_a); db.refresh(t_b); db.refresh(resp_user)

        # L1 Atacama, TagA, responsavel, 4 viajantes, chegada 2026-03-10
        # L2 Uyuni,   TagB, Agente IA,   2 viajantes, chegada 2026-06-20
        # L3 Atacama+Uyuni, TagA+TagB, responsavel, 9 viajantes, chegada 2026-03-25
        # L4 Santiago, sem tag, Agente IA, sem viajantes, sem chegada
        l1 = Lead(nome="Ana Atacama", whatsapp="+5551111111111", destinos=["Atacama"],
                  num_viajantes=4, data_chegada=date(2026, 3, 10),
                  responsavel_id=resp_user.id)
        l2 = Lead(nome="Bruno Uyuni", whatsapp="+5551222222222", destinos=["Uyuni"],
                  num_viajantes=2, data_chegada=date(2026, 6, 20))
        l3 = Lead(nome="Carla Dupla", whatsapp="+5551333333333",
                  destinos=["Atacama", "Uyuni"], num_viajantes=9,
                  data_chegada=date(2026, 3, 25), responsavel_id=resp_user.id)
        l4 = Lead(nome="Davi Santiago", whatsapp="+5551444444444",
                  destinos=["Santiago"])
        # L5 fica FORA do funil: prova a exclusao do dropdown
        l5 = Lead(nome="Elena Fora", whatsapp="+5551555555555", destinos=["Atacama"])
        db.add_all([l1, l2, l3, l4, l5])
        db.commit()
        for lead in (l1, l3):
            lead.tags.append(t_a)
        for lead in (l2, l3):
            lead.tags.append(t_b)

        f1 = Funnel(nome="Funil Um", etapas=[{"id": "e1", "nome": "Etapa 1"}])
        f2 = Funnel(nome="Funil Dois", etapas=[{"id": "z1", "nome": "Zeta 1"}])
        f3 = Funnel(nome="Funil Tres", etapas=[{"id": "w1", "nome": "Wave 1"}])
        db.add_all([f1, f2, f3])
        db.commit()
        db.refresh(f1); db.refresh(f2); db.refresh(f3)

        db.add_all([
            FunnelEntry(lead_id=l1.id, funnel_id=f1.id, etapa_id="e1", posicao=0),
            FunnelEntry(lead_id=l2.id, funnel_id=f1.id, etapa_id="e1", posicao=1),
            FunnelEntry(lead_id=l3.id, funnel_id=f1.id, etapa_id="e1", posicao=2),
            FunnelEntry(lead_id=l4.id, funnel_id=f1.id, etapa_id="e1", posicao=3),
            # L1 tambem no funil 2: lead em MAIS DE UM funil
            FunnelEntry(lead_id=l1.id, funnel_id=f2.id, etapa_id="z1", posicao=0),
            # L2 em DOIS funis alem do f1: caso C (fallback com varios candidatos)
            FunnelEntry(lead_id=l2.id, funnel_id=f2.id, etapa_id="z1", posicao=1),
            FunnelEntry(lead_id=l2.id, funnel_id=f3.id, etapa_id="w1", posicao=0),
        ])
        db.commit()

        ctx = {"f1": f1.id, "f2": f2.id, "f3": f3.id, "resp": resp_user.id,
               "tagA": t_a.id, "tagB": t_b.id,
               "l1": l1.id, "l2": l2.id, "l3": l3.id, "l4": l4.id, "l5": l5.id}
    finally:
        db.close()

    client = TestClient(app)
    client.__enter__()
    r = client.post("/api/auth/login",
                    json={"email": "admin@local.test", "password": "LocalSmoke123!"})
    assert r.status_code == 200, f"login: {r.status_code}"
    _C["client"], _C["ctx"] = client, ctx
    return client, ctx


def _ids(client, url):
    r = client.get(url)
    assert r.status_code == 200, f"{url} -> {r.status_code}: {r.text[:200]}"
    j = r.json()
    campo = "items" if "items" in j else "leads"
    return {i.get("lead_id", i.get("id")) for i in j[campo]}, j


def _stage(ctx, **p):
    base = f"/api/pipeline/board/{ctx['f1']}/stage/e1"
    qs = "&".join(f"{k}={v}" for k, v in p.items())
    return base + ("?" + qs if qs else "")


# ── 1. DROPDOWN: exclusao server-side ────────────────────────────────────

def test_dropdown_exclui_leads_ja_no_funil():
    client, ctx = _setup()
    todos, _ = _ids(client, "/api/leads?limit=100")
    assert ctx["l1"] in todos and ctx["l5"] in todos, "sanidade do seed"

    disponiveis, _ = _ids(client, f"/api/leads?limit=100&exclude_funnel_id={ctx['f1']}")
    assert ctx["l5"] in disponiveis, "lead FORA do funil precisa aparecer"
    for chave in ("l1", "l2", "l3", "l4"):
        assert ctx[chave] not in disponiveis, (
            f"{chave} JA esta no funil e nao pode aparecer no dropdown"
        )


def test_exclusao_do_dropdown_e_uma_query_so():
    """NOT EXISTS correlacionado: nao pode virar uma query por lead."""
    from sqlalchemy import event
    from app.database import engine

    client, ctx = _setup()
    contador = []

    def _ouvir(conn, cur, stmt, params, c, many):
        contador.append(stmt)

    event.listen(engine, "before_cursor_execute", _ouvir)
    try:
        _ids(client, f"/api/leads?limit=100&exclude_funnel_id={ctx['f1']}")
    finally:
        event.remove(engine, "before_cursor_execute", _ouvir)
    assert len(contador) <= 10, f"{len(contador)} queries — N+1 na exclusao?"


def test_409_continua_protegendo_contra_corrida():
    """A UI nao oferece mais o lead, mas o backend segue recusando o duplicado."""
    client, ctx = _setup()
    r = client.post(f"/api/pipeline/funnels/{ctx['f1']}/leads",
                    json={"lead_id": ctx["l1"], "etapa_id": "e1"})
    assert r.status_code == 409, f"esperava 409, veio {r.status_code}"


# ── 2. LOCATE com lead em MAIS DE UM funil ───────────────────────────────

def test_lead_pode_estar_em_mais_de_um_funil():
    client, ctx = _setup()
    r1 = client.get(f"/api/pipeline/locate/{ctx['l1']}?prefer_funnel_id={ctx['f1']}")
    r2 = client.get(f"/api/pipeline/locate/{ctx['l1']}?prefer_funnel_id={ctx['f2']}")
    assert r1.json()["funnel_id"] == ctx["f1"]
    assert r2.json()["funnel_id"] == ctx["f2"]
    assert r1.json()["entry_id"] != r2.json()["entry_id"], "entries distintas por funil"


def test_prefer_funnel_reproduz_a_regra_antiga():
    """
    Antes, o frontend procurava o lead SO dentro do funil aberto. Com
    prefer_funnel_id o resultado e o mesmo: o funil da tela vence.
    """
    client, ctx = _setup()
    body = client.get(
        f"/api/pipeline/locate/{ctx['l1']}?prefer_funnel_id={ctx['f2']}").json()
    assert body["funnel_id"] == ctx["f2"], (
        "o funil aberto precisa vencer, mesmo havendo entry mais antiga em outro"
    )
    assert body["etapa_id"] == "z1"


# ── Regra decidida do /locate: A, B, C, D ────────────────────────────────

def test_A_lead_no_funil_preferido_vence():
    client, ctx = _setup()
    body = client.get(
        f"/api/pipeline/locate/{ctx['l1']}?prefer_funnel_id={ctx['f2']}").json()
    assert body["funnel_id"] == ctx["f2"], "o funil preferido tem prioridade"


def test_B_lead_fora_do_preferido_com_exatamente_um_outro_funil():
    client, ctx = _setup()
    # l4 so existe no f1; pedindo f2, o fallback deve achar o f1
    body = client.get(
        f"/api/pipeline/locate/{ctx['l4']}?prefer_funnel_id={ctx['f2']}").json()
    assert body["funnel_id"] == ctx["f1"], (
        "com 1 outro funil, o fallback tem que localizar o lead (nao 404)"
    )


def test_C_fallback_com_multiplos_outros_funis_e_deterministico():
    client, ctx = _setup()
    # l2 esta em f1, f2 e f3. Pedindo um funil onde ele NAO esta, o fallback
    # precisa devolver SEMPRE o mesmo — created_at ASC, id ASC.
    url = f"/api/pipeline/locate/{ctx['l2']}?prefer_funnel_id=999999"
    respostas = [client.get(url).json() for _ in range(5)]
    entries = {r["entry_id"] for r in respostas}
    assert len(entries) == 1, (
        f"fallback nao-deterministico com varios funis: {entries}"
    )
    assert respostas[0]["funnel_id"] == ctx["f1"], (
        "com created_at ASC, id ASC a entry mais antiga (f1) tem que vencer"
    )


def test_D_lead_sem_funnel_entry_da_404():
    client, ctx = _setup()
    r = client.get(f"/api/pipeline/locate/{ctx['l5']}?prefer_funnel_id={ctx['f1']}")
    assert r.status_code == 404, (
        f"lead sem nenhuma entry deve dar 404, veio {r.status_code}"
    )


# ── 3. PARIDADE DOS FILTROS (conjunto de lead_id, nao status HTTP) ───────

def test_filtro_destino():
    client, ctx = _setup()
    ids, _ = _ids(client, _stage(ctx, destino="Atacama"))
    assert ids == {ctx["l1"], ctx["l3"]}, (
        f"destino Atacama deveria casar L1 e L3 (L3 tem 2 destinos), veio {ids}"
    )


def test_filtro_tag_unica_e_multiplas_sao_OR():
    client, ctx = _setup()
    so_a, _ = _ids(client, _stage(ctx, tag_ids=ctx["tagA"]))
    assert so_a == {ctx["l1"], ctx["l3"]}, f"TagA: {so_a}"

    a_ou_b, _ = _ids(client, _stage(ctx) + f"?tag_ids={ctx['tagA']}&tag_ids={ctx['tagB']}")
    assert a_ou_b == {ctx["l1"], ctx["l2"], ctx["l3"]}, (
        f"multiplas tags e OR (mesma regra do JavaScript antigo), veio {a_ou_b}"
    )


def test_filtro_responsavel_e_agente_ia():
    client, ctx = _setup()
    humano, _ = _ids(client, _stage(ctx, responsavel_id=ctx["resp"]))
    assert humano == {ctx["l1"], ctx["l3"]}, f"responsavel humano: {humano}"

    ia, _ = _ids(client, _stage(ctx, responsavel_id=0))
    assert ia == {ctx["l2"], ctx["l4"]}, f"responsavel_id=0 (Agente IA): {ia}"


def test_filtro_viajantes_minimo():
    client, ctx = _setup()
    ids, _ = _ids(client, _stage(ctx, viajantes_min=4))
    assert ids == {ctx["l1"], ctx["l3"]}, f"viajantes >= 4: {ids}"
    # lead sem num_viajantes (NULL) NAO entra — igual ao JS, que comparava numero
    assert ctx["l4"] not in ids, "lead sem num_viajantes nao pode casar >= N"


def test_remover_etapa_com_lead_e_recusado():
    """
    AUDIT-2026-08-WC (F-246) — trocar a lista de etapas orfanava leads em silencio.

    `Funnel.etapas` e uma coluna JSON substituida inteira, e
    `funnel_entries.etapa_id` aponta para essas strings sem nenhuma FK por tras.
    Remover uma etapa deixava todo lead dela com um `etapa_id` inexistente — e o
    board so renderiza entries cujo `etapa_id` esta na lista atual. Os leads nao
    eram apagados: ficavam INVISIVEIS, sem erro e sem caminho de volta pela
    interface.

    Recusar e melhor que migrar em silencio: para onde vai um lead que estava em
    "Proposta enviada" e uma decisao de negocio, e adivinhar perderia informacao
    sem ninguem ficar sabendo.
    """
    client, ctx = _setup()
    funnel_id = ctx["f1"]

    atual = client.get(f"/api/pipeline/funnels/{funnel_id}").json()
    etapas = list(atual["etapas"])
    # A fixture nasce com UMA etapa. Acrescentamos uma vazia para poder testar
    # os dois lados da regra: remover a ocupada e recusado, remover a vazia nao.
    if len(etapas) < 2:
        etapas = etapas + [{"id": "e2_vazia", "nome": "Etapa vazia"}]
        r_add = client.put(f"/api/pipeline/funnels/{funnel_id}", json={"etapas": etapas})
        assert r_add.status_code == 200, f"nao consegui preparar a fixture: {r_add.status_code}"
        etapas = client.get(f"/api/pipeline/funnels/{funnel_id}").json()["etapas"]

    ocupada = next(e for e in etapas if e["id"] == "e1")
    restantes = [e for e in etapas if e["id"] != ocupada["id"]]

    r = client.put(f"/api/pipeline/funnels/{funnel_id}", json={"etapas": restantes})
    assert r.status_code == 409,         f"remover etapa com lead deve ser recusado com 409, veio {r.status_code}"
    corpo = r.json().get("detail", "")
    assert ocupada["id"] in corpo, f"o 409 precisa NOMEAR a etapa: {corpo}"
    assert "lead" in corpo.lower(), f"o 409 precisa dizer quantos leads: {corpo}"

    # E o funil NAO pode ter sido alterado pela tentativa recusada.
    depois = client.get(f"/api/pipeline/funnels/{funnel_id}").json()
    assert [e["id"] for e in depois["etapas"]] == [e["id"] for e in etapas],         "a recusa nao pode ter alterado as etapas"

    # Renomear/reordenar SEM remover continua permitido.
    reordenado = list(reversed(etapas))
    r2 = client.put(f"/api/pipeline/funnels/{funnel_id}", json={"etapas": reordenado})
    assert r2.status_code == 200,         f"reordenar sem remover deve continuar funcionando, veio {r2.status_code}"

    # E remover uma etapa VAZIA continua permitido.
    vazias = [e for e in etapas if e["id"] != "e1"]
    assert vazias, "a fixture precisa de ao menos uma etapa vazia para este caso"
    sem_a_vazia = [e for e in etapas if e["id"] != vazias[0]["id"]]
    r3 = client.put(f"/api/pipeline/funnels/{funnel_id}", json={"etapas": sem_a_vazia})
    assert r3.status_code == 200,         f"remover etapa VAZIA deve continuar permitido, veio {r3.status_code}"


def test_filtro_viajantes_exato():
    """
    AUDIT-2026-08-WC5 — "pelo menos X" e "exatamente X" sao perguntas diferentes.

    O filtro nasceu como MINIMO (a UI diz "pelo menos X" e
    `test_filtro_viajantes_minimo`, logo acima, trava esse contrato). A operacao
    precisa tambem separar viajante solo de casal e de familia, o que o minimo
    nao consegue expressar. Trocar a semantica do parametro existente atenderia
    um dos dois e quebraria o outro — inclusive quem ja tivesse salvo um filtro.
    Por isso e um parametro NOVO, e os dois convivem.

    Fixtures: l1 tem 4 viajantes, l2 tem 2, l3 tem 9, l4 tem NULL.
    """
    client, ctx = _setup()

    ids, _ = _ids(client, _stage(ctx, viajantes_exato=4))
    assert ids == {ctx["l1"]}, f"viajantes == 4 deve trazer SO o l1: {ids}"
    assert ctx["l3"] not in ids, "l3 tem 9 viajantes — o exato nao pode se comportar como minimo"

    ids2, _ = _ids(client, _stage(ctx, viajantes_exato=2))
    assert ids2 == {ctx["l2"]}, f"viajantes == 2 deve trazer SO o l2: {ids2}"

    ids3, _ = _ids(client, _stage(ctx, viajantes_exato=1))
    assert ids3 == set(), f"nenhum lead tem exatamente 1 viajante: {ids3}"

    # NULL nao casa com numero nenhum, igual ao ramo do minimo.
    ids4, _ = _ids(client, _stage(ctx, viajantes_exato=9))
    assert ctx["l4"] not in ids4, "lead sem num_viajantes nao pode casar == N"

    # O minimo continua intacto ao lado — este e o ponto do parametro novo.
    ids5, _ = _ids(client, _stage(ctx, viajantes_min=4))
    assert ids5 == {ctx["l1"], ctx["l3"]}, f"viajantes >= 4 nao pode ter mudado: {ids5}"


def test_filtro_viajantes_min_e_exato_juntos_e_recusado():
    """
    Mandar os dois seria ambiguo: o backend nao deve escolher um em silencio.
    """
    client, ctx = _setup()
    resp = client.get(_stage(ctx, viajantes_min=2, viajantes_exato=4))
    assert resp.status_code == 422,         f"min + exato juntos devem ser recusados com 422, veio {resp.status_code}"


def test_filtro_chegada_intervalo():
    client, ctx = _setup()
    ids, _ = _ids(client, _stage(ctx, chegada_de="2026-03-01", chegada_ate="2026-03-31"))
    assert ids == {ctx["l1"], ctx["l3"]}, f"chegada em marco: {ids}"


def test_valores_vazios_nao_filtram():
    client, ctx = _setup()
    todos, _ = _ids(client, _stage(ctx))
    vazio, _ = _ids(client, _stage(ctx, q="", destino="", periodo=""))
    assert vazio == todos, "parametros vazios nao podem filtrar nada"


def test_combinacao_destino_mais_responsavel():
    client, ctx = _setup()
    ids, _ = _ids(client, _stage(ctx, destino="Atacama", responsavel_id=ctx["resp"]))
    assert ids == {ctx["l1"], ctx["l3"]}, f"destino AND responsavel: {ids}"


def test_combinacao_tags_mais_chegada():
    client, ctx = _setup()
    ids, _ = _ids(client, _stage(ctx) +
                  f"?tag_ids={ctx['tagB']}&chegada_de=2026-03-01&chegada_ate=2026-03-31")
    assert ids == {ctx["l3"]}, (
        f"TagB AND chegada em marco: so L3 (L2 tem TagB mas chega em junho), veio {ids}"
    )


def test_combinacao_destino_tags_viajantes():
    client, ctx = _setup()
    ids, _ = _ids(client, _stage(ctx) +
                  f"?destino=Atacama&tag_ids={ctx['tagA']}&viajantes_min=5")
    assert ids == {ctx["l3"]}, (
        f"Atacama AND TagA AND >=5 viajantes: so L3 (L1 tem 4), veio {ids}"
    )


# ── 4. CONTADORES ────────────────────────────────────────────────────────

def test_total_do_filtro_difere_do_total_da_etapa():
    client, ctx = _setup()
    _, sem = _ids(client, _stage(ctx))
    _, com = _ids(client, _stage(ctx, destino="Atacama"))
    meta = client.get(f"/api/pipeline/board/{ctx['f1']}/meta").json()
    total_etapa = next(s["total"] for s in meta["stages"] if s["id"] == "e1")

    assert sem["total"] == total_etapa == 4, "sem filtro, total = total da etapa"
    assert com["total"] == 2, f"com filtro, total = do filtro, veio {com['total']}"
    assert com["total"] != total_etapa, (
        "a UI precisa dos DOIS numeros para escrever '2 de 4'"
    )


def test_template_monta_o_rotulo_x_de_y():
    """Guard estatico: o cabecalho nao pode voltar a mostrar so o total."""
    html = pathlib.Path("templates/pipeline.html").read_text(encoding="utf-8")
    assert "st.filtrado + ' de ' + meta.total" in html, (
        "o cabecalho da coluna precisa mostrar 'X de Y' quando ha filtro ativo"
    )


# ── 5. DIALETO POSTGRESQL (producao) ─────────────────────────────────────

def test_queries_novas_compilam_em_postgresql():
    from datetime import datetime, timezone
    from sqlalchemy import tuple_
    from sqlalchemy.dialects import postgresql
    from app.database import SessionLocal
    from app.models.pipeline import FunnelEntry
    import app.routers.pipeline as pipe

    db = SessionLocal()
    try:
        filtros = {"q": "ana", "periodo": "7d", "responsavel_id": 1,
                   "destino": "Atacama", "tag_ids": [1, 2],
                   "viajantes_min": 2, "chegada_de": None, "chegada_ate": None}
        # forca o ramo PostgreSQL do helper de destino
        original = pipe.IS_SQLITE
        pipe.IS_SQLITE = False
        try:
            q = pipe._stage_query(db, 1, "e1", filtros)
            q = q.order_by(FunnelEntry.updated_at.desc(), FunnelEntry.id.desc())
            q = q.filter(tuple_(FunnelEntry.updated_at, FunnelEntry.id)
                         < (datetime.now(timezone.utc), 10))
            sql = str(q.statement.compile(dialect=postgresql.dialect()))
        finally:
            pipe.IS_SQLITE = original
    finally:
        db.close()

    baixo = sql.lower()
    assert "order by" in baixo and "desc" in baixo, "keyset perdeu o ORDER BY"
    assert "jsonb" in baixo, (
        "o filtro de destino precisa castar para JSONB no PostgreSQL "
        "(a coluna e `json` e @> so existe para jsonb)"
    )
    assert "@>" in sql, "operador de containment ausente"
    assert "ilike" in baixo, "a busca precisa ser case-insensitive"
    assert "exists" in baixo or "in (" in baixo, "filtro de tags nao compilou"
    # nada especifico de SQLite pode vazar para producao
    for proibido in ("julianday", "strftime", "sqlite_"):
        assert proibido not in baixo, f"funcao especifica de SQLite no SQL: {proibido}"


def test_exclusao_do_dropdown_compila_em_postgresql():
    from sqlalchemy.dialects import postgresql
    from app.database import SessionLocal
    from app.models.lead import Lead
    from app.models.pipeline import FunnelEntry

    db = SessionLocal()
    try:
        ja = (db.query(FunnelEntry.id)
              .filter(FunnelEntry.lead_id == Lead.id, FunnelEntry.funnel_id == 1)
              .exists())
        sql = str(db.query(Lead).filter(~ja).statement
                  .compile(dialect=postgresql.dialect())).lower()
    finally:
        db.close()
    assert "not (exists" in sql or "not exists" in sql, f"esperava NOT EXISTS: {sql[:200]}"


# ── 6. updated_at NULL ───────────────────────────────────────────────────

def test_updated_at_e_nullable_no_ddl_mas_inalcancavel_pela_aplicacao():
    from app.models.pipeline import FunnelEntry
    col = FunnelEntry.__table__.c.updated_at
    assert col.nullable is True, "o DDL permite NULL (server_default, sem NOT NULL)"
    assert col.server_default is not None, (
        "o default do banco e o que garante que NULL nunca aparece por INSERT normal"
    )


def test_cursor_nao_estoura_com_updated_at_nulo():
    """Uma linha com NULL (so por SQL direto) nao pode derrubar a coluna."""
    import app.routers.pipeline as pipe

    class _Fake:
        updated_at = None
        id = 7

    assert pipe._cursor_encode(_Fake()) is None, (
        "sem cursor a coluna para de paginar; antes isso era AttributeError -> 500"
    )


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
