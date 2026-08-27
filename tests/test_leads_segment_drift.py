# -*- coding: utf-8 -*-
"""
PERPETUA-PRODUCTION-DRIFT-01 — regressão dos hotfixes consolidados da VPS.

Prova, em processo e sem nenhuma dependência externa, que:
  1. GET /api/leads/segment NÃO retorna 500 por TypeError com leads no
     resultado (era o bug: `_build_lead_response(l, db)` com a assinatura de
     1 parâmetro — hotfixado direto na VPS em 2026-07-08 e consolidado aqui).
  2. A resposta preserva a estrutura {total, skip, limit, leads} e os
     filtros/paginação continuam funcionando (search, skip, limit).
  3. Guard estático: nenhuma chamada `_build_lead_response(l, db)` volta ao
     arquivo (tripwire da regressão sem depender de dados).
  4. docker-compose.yml: o serviço `crm` recebe INTERNAL_AI_AUTH_SECRET por
     EXPANSÃO de variável (`${INTERNAL_AI_AUTH_SECRET:-}`), exatamente uma vez,
     somente no serviço crm, sem valor literal/hardcoded — e o YAML continua
     parseável. O .env real NUNCA é lido.

NÃO toca produção. Sem Gemini (GEMINI_API_KEY vazio), sem rede, sem n8n
(lead criado direto no SQLite descartável de scratch/, sem passar pela rota
que dispara automações), sem dados reais.

Rodar:  python tests/test_leads_segment_drift.py
   ou:  python -m pytest tests/test_leads_segment_drift.py
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import os  # noqa: E402

pathlib.Path("scratch").mkdir(exist_ok=True)
os.environ.update({
    "ENVIRONMENT": "development",
    # AUDIT-2026-08-WF2 — respeita DATABASE_URL do ambiente para o mesmo
    # arquivo rodar contra o PostgreSQL de auditoria; sem nada definido cai no
    # SQLite descartavel de sempre, que e o que o CI tem. Mesmo desvio de
    # tests/test_pipeline_funnel_race.py.
    "DATABASE_URL": (os.environ.get("DATABASE_URL")
                     or "sqlite:///./scratch/leads_segment_test.db"),
    "SEED_INITIAL_ADMIN": "true",
    "ADMIN_INITIAL_EMAIL": "admin@local.test",
    "ADMIN_INITIAL_PASSWORD": "LocalSmoke123!",
    # GEMINI vazio de propósito: nenhum teste pode chamar o Gemini de verdade.
    "GEMINI_API_KEY": "",
})

ROOT = pathlib.Path(__file__).resolve().parent.parent
ADMIN_EMAIL = "admin@local.test"
ADMIN_PASSWORD = "LocalSmoke123!"

_DB_INITIALIZED = False


def _client():
    # Mesmo padrão da suíte test_perpetua_internal_auth.py: deleta o DB
    # descartável UMA vez (Windows trava o arquivo entre clients) e reusa.
    global _DB_INITIALIZED
    db = pathlib.Path("scratch/leads_segment_test.db")
    if not _DB_INITIALIZED and db.exists():
        try:
            db.unlink()
        except PermissionError:
            pass
    _DB_INITIALIZED = True
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _seed_leads():
    """Insere leads direto no banco descartável — sem passar pela rota POST
    (que poderia disparar integrações) e garantindo resultado não vazio no
    segment, condição necessária para reproduzir o bug antigo."""
    from app.database import SessionLocal
    from app.models.lead import Lead
    db = SessionLocal()
    try:
        # AUDIT-2026-08-WF2 — semeia pelo e-mail de cada lead, nao por "tabela
        # vazia": com DATABASE_URL apontando para o PostgreSQL de auditoria o
        # banco e COMPARTILHADO e quase nunca esta vazio, e o `count() == 0`
        # pulava o seed, derrubando os dois testes de segment por falta de dado.
        for nome, email, whats, destinos in (
                ("Lead Teste Um", "um@teste.local", "+56000000001", ["Atacama"]),
                ("Lead Teste Dois", "dois@teste.local", "+56000000002", ["Uyuni"])):
            if not db.query(Lead).filter(Lead.email == email).first():
                db.add(Lead(nome=nome, email=email, whatsapp=whats,
                            destinos=destinos))
        db.commit()
    finally:
        db.close()


def _login(client):
    r = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, f"login falhou: {r.status_code} {r.text[:200]}"


# ── 1. Regressão principal: segment com leads não retorna 500 ──────────────

def test_segment_retorna_200_com_leads():
    with _client() as client:
        _seed_leads()
        _login(client)
        r = client.get("/api/leads/segment")
        assert r.status_code == 200, (
            f"segment quebrou (era o bug do drift: TypeError por "
            f"_build_lead_response(l, db)): {r.status_code} {r.text[:300]}"
        )
        body = r.json()
        for key in ("total", "skip", "limit", "leads"):
            assert key in body, f"campo '{key}' ausente da resposta"
        assert body["total"] >= 2, "esperava os leads semeados no total"
        assert len(body["leads"]) >= 2, "esperava leads na lista (condição do bug)"
        lead = body["leads"][0]
        for key in ("id", "nome"):
            assert key in lead, f"campo '{key}' ausente no LeadResponse"


def test_segment_preserva_filtros_e_paginacao():
    with _client() as client:
        _seed_leads()
        _login(client)
        r = client.get("/api/leads/segment", params={"search": "Lead Teste Um", "skip": 0, "limit": 1})
        assert r.status_code == 200, f"segment com filtros quebrou: {r.status_code}"
        body = r.json()
        assert body["skip"] == 0 and body["limit"] == 1, "skip/limit não ecoados"
        assert body["total"] >= 1, "filtro search não encontrou o lead semeado"
        assert len(body["leads"]) == 1, "limit=1 não respeitado"
        assert "Um" in body["leads"][0]["nome"]

        r2 = client.get("/api/leads/segment", params={"destino": "Atacama"})
        assert r2.status_code == 200, f"filtro destino quebrou: {r2.status_code}"
        assert r2.json()["total"] >= 1


# ── 2. Guard estático do código (tripwire da regressão) ────────────────────

def test_guard_estatico_sem_chamada_com_db():
    src = (ROOT / "app" / "routers" / "leads.py").read_text(encoding="utf-8")
    assert "_build_lead_response(l, db)" not in src, (
        "regressão do drift: chamada com 'db' voltou ao leads.py"
    )
    assert "def _build_lead_response(lead: Lead)" in src, (
        "assinatura de _build_lead_response mudou — revisar o guard"
    )


# ── 3. Guard estático do docker-compose (drift B) ──────────────────────────

COMPOSE_PATH = ROOT / "docker-compose.yml"
EXPECTED_LINE = "- INTERNAL_AI_AUTH_SECRET=${INTERNAL_AI_AUTH_SECRET:-}"


def _crm_block():
    """Bloco do serviço crm: de '  crm:' até o próximo serviço no mesmo nível."""
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    m = re.search(r"^  crm:\n(.*?)(?=^  \w[\w-]*:|\Z)", text, re.M | re.S)
    assert m, "bloco do serviço crm não encontrado no compose"
    return text, m.group(0)


def test_compose_crm_recebe_internal_ai_auth_secret():
    text, crm = _crm_block()
    assert EXPECTED_LINE in crm, (
        "serviço crm sem INTERNAL_AI_AUTH_SECRET=${INTERNAL_AI_AUTH_SECRET:-}"
    )
    assert text.count("INTERNAL_AI_AUTH_SECRET") == 2, (
        "esperava a variável exatamente 1x no compose (nome + expansão na mesma linha)"
    )
    # Somente por expansão — nunca literal (nenhum valor hardcoded)
    for m in re.finditer(r"INTERNAL_AI_AUTH_SECRET=([^\s]+)", text):
        assert m.group(1).startswith("${"), "valor literal detectado para o segredo"
    # Somente no serviço crm
    fora = text.replace(crm, "")
    assert "INTERNAL_AI_AUTH_SECRET" not in fora, (
        "INTERNAL_AI_AUTH_SECRET vazou para outro serviço do compose"
    )


def test_compose_continua_parseavel():
    import yaml
    data = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    env = data["services"]["crm"]["environment"]
    assert any(e.startswith("INTERNAL_AI_AUTH_SECRET=") for e in env), (
        "environment do crm sem INTERNAL_AI_AUTH_SECRET após parse YAML"
    )
    for svc in ("postgres", "n8n", "conversas"):
        other_env = data["services"].get(svc, {}).get("environment", []) or []
        assert not any(str(e).startswith("INTERNAL_AI_AUTH_SECRET") for e in other_env), (
            f"variável indevida no serviço {svc}"
        )


def test_env_example_documenta_variavel():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "INTERNAL_AI_AUTH_SECRET" in env_example, (
        ".env.example deveria documentar INTERNAL_AI_AUTH_SECRET (nome, sem valor)"
    )


# ── 4. AUDIT-2026-08-WF2: o filtro de campo personalizado nao pode morrer ──
#
# `leads.campos_personalizados` e `json` (validado so na sintaxe). O filtro
# castava cada linha para `jsonb`, e UMA linha que nao castasse derrubava a
# query inteira, para TODOS os leads (F-043). O guard que veio depois so
# reconhecia o escape de NUL — a revisao adversarial mediu, contra PostgreSQL
# 16.14, que `{"orcamento": 1e1000000}` passa por ele e mata a query
# (NumericValueOutOfRange), e que ele ainda excluia por engano a linha que
# guarda a BARRA escapada, que casta sem problema nenhum.
#
# Estes testes cobrem os dois lados:
#   - compilacao: o ramo PostgreSQL nao pode voltar a castar para jsonb;
#   - execucao: o corpus adversarial no banco ATIVO (SQLite no CI, PostgreSQL
#     quando DATABASE_URL apontar para ele) nao derruba a query e nao some com
#     linha legitima.

WF2_BARRA = chr(92)

# texto JSON CRU, como psql/n8n/COPY gravariam (ver migrations/m011, F5).
WF2_CORPUS = [
    (1, "objeto normal",   '{"origem": "instagram"}'),
    (2, "array no topo",   '["instagram"]'),
    (3, "json null",       'null'),
    (4, "string no topo",  '"instagram"'),
    (5, "num 1e1000000",   '{"orcamento": 1e1000000}'),
    (6, "NUL real",        '{"origem": "instagram", "obs": "' + WF2_BARRA + 'u0000"}'),
    (7, "barra literal",   '{"origem": "ref' + WF2_BARRA * 2 + 'u0000alpha"}'),
    (8, "u0000 sem barra", '{"origem": "ref-u0000-alpha"}'),
    (9, "numero 1e2",      '{"orcamento": 1e2}'),
]


def _wf2_resumo(sql):
    """Corta o SQL antes do bloco _ESPACOS: ele tem caracteres que o console do
    Windows (cp1252) nao imprime, e derrubaria o proprio relatorio de falha."""
    corte = sql.find("WHERE lower(")
    return (sql[:corte] if corte > 0 else sql)[:400]


def _wf2_sql(is_sqlite, dialect):
    """Compila o predicado REAL forcando o ramo desejado, sem conexao."""
    import app.query_filters as qf
    from app.models.lead import Lead
    original = qf.IS_SQLITE
    qf.IS_SQLITE = is_sqlite
    try:
        expr = qf.campo_personalizado_match(Lead.campos_personalizados, "origem", "x")
        return str(expr.compile(dialect=dialect,
                                compile_kwargs={"literal_binds": True}))
    finally:
        qf.IS_SQLITE = original


def test_wf2_ramo_postgres_nao_casta_mais_para_jsonb():
    """O cast era a causa raiz: some com ele e a classe inteira de falha de
    conversao (o overflow numerico incluso) deixa de existir."""
    from sqlalchemy.dialects import postgresql
    sql = _wf2_sql(False, postgresql.dialect())
    assert "JSONB" not in sql.upper(), (
        "ramo PostgreSQL voltou a castar para jsonb — uma linha que nao caste "
        f"derruba a query inteira de novo: {_wf2_resumo(sql)}"
    )
    assert "json_each_text(" in sql, (
        f"esperava json_each_text sobre a coluna json: {_wf2_resumo(sql)}"
    )
    assert "json_typeof(" in sql, (
        f"esperava json_typeof (total) no lugar de jsonb_typeof: {_wf2_resumo(sql)}"
    )


def test_wf2_guard_nao_e_denylist_de_um_escape_so():
    """Guard tem de ser allowlist: remove os escapes que SABE converter e exige
    que nao sobre nenhum. Uma denylist de padroes ja perdeu duas vezes."""
    from sqlalchemy.dialects import postgresql
    sql = _wf2_sql(False, postgresql.dialect())
    # so a regiao do GUARD: depois de "WHERE lower(" vem a comparacao de
    # chave/valor, que usa LIKE de proposito (contains + autoescape).
    guard = _wf2_resumo(sql)
    assert guard.count("regexp_replace(") >= 2, (
        "guard voltou a ser procura de padrao unico; o allowlist precisa "
        f"remover par substituto E escape conversivel: {guard}"
    )
    assert "strpos(" in guard, (
        f"esperava strpos (substring literal), nao LIKE: {guard}"
    )
    # tripwire da revisao 1: LIKE trata a barra como escape e ja barrou linha boa
    assert " LIKE " not in guard.upper(), (
        f"guard nao pode usar LIKE — a barra e o escape default: {guard}"
    )


def test_wf2_ramo_sqlite_inalterado():
    """Sem regressao de dialeto: o SQLite nunca teve o defeito e continua igual."""
    from sqlalchemy.dialects import sqlite as sqlite_dialect
    sql = _wf2_sql(True, sqlite_dialect.dialect())
    assert "json_each(" in sql, f"ramo SQLite deveria usar json_each: {_wf2_resumo(sql)}"
    assert "json_type(" in sql, f"ramo SQLite deveria usar json_type: {_wf2_resumo(sql)}"
    baixo = sql.lower()
    assert "jsonb" not in baixo, f"jsonb vazou para o ramo SQLite: {_wf2_resumo(sql)}"
    assert "regexp_replace" not in baixo, (
        f"o guard do PostgreSQL vazou para o ramo SQLite: {_wf2_resumo(sql)}"
    )


def _wf2_tabela(engine):
    """Cria a tabela descartavel do corpus no backend ATIVO e a semeia com o
    texto JSON cru (sem passar por json.dumps, que nunca produziria estas
    linhas)."""
    from sqlalchemy import Column, Integer, JSON, MetaData, String, Table, text
    from app.database import IS_SQLITE
    md = MetaData()
    t = Table("wf2_corpus", md,
              Column("id", Integer, primary_key=True),
              Column("nome", String),
              Column("campos_personalizados", JSON))
    with engine.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS wf2_corpus"))
    md.create_all(engine)
    valor = ":j" if IS_SQLITE else "CAST(:j AS json)"
    with engine.begin() as c:
        for i, nome, js in WF2_CORPUS:
            c.execute(text("INSERT INTO wf2_corpus (id, nome, campos_personalizados) "
                           "VALUES (:i, :n, " + valor + ")"),
                      {"i": i, "n": nome, "j": js})
    return t


def _wf2_ids(engine, t, chave, valor):
    from sqlalchemy import select
    from app.query_filters import campo_personalizado_match
    q = select(t.c.id).where(
        campo_personalizado_match(t.c.campos_personalizados, chave, valor))
    with engine.connect() as c:
        return {r[0] for r in c.execute(q)}


def test_wf2_corpus_adversarial_no_banco_ativo():
    from sqlalchemy import text
    from app.database import engine, IS_SQLITE
    t = _wf2_tabela(engine)
    try:
        # Defeito 1 (HIGH): a linha 5 nao tem nada a ver com a busca, mas o cast
        # para jsonb estourava nela e matava a query para TODO mundo.
        # Defeito 2 (MEDIUM): a linha 7 guarda a BARRA escapada, casta sem
        # problema, e o guard de NUL a excluia em silencio.
        ids = _wf2_ids(engine, t, "origem", "ref")
        assert ids == {7, 8}, (
            "busca origem~'ref' deveria achar a linha da barra escapada (7) e a "
            f"que so tem a substring sem barra (8); veio {sorted(ids)}"
        )

        # A linha 6 tem o escape de NUL de verdade: no PostgreSQL ela e
        # descartada do filtro (a troca deliberada — some UMA linha, em vez de a
        # funcionalidade sumir para todos); no SQLite nunca houve defeito e ela
        # aparece normalmente.
        ids = _wf2_ids(engine, t, "origem", "instagram")
        esperado = {1, 6} if IS_SQLITE else {1}
        assert ids == esperado, (
            f"busca origem~'instagram' deveria devolver {sorted(esperado)} neste "
            f"backend; veio {sorted(ids)}"
        )

        # A linha do overflow numerico deixa de ser fatal e volta a ser achavel.
        ids = _wf2_ids(engine, t, "orcamento", "")
        assert ids == {5, 9}, (
            f"busca so por presenca da chave 'orcamento' deveria achar 5 e 9; "
            f"veio {sorted(ids)}"
        )

        # Nao-objeto no topo continua descartado, sem derrubar nada.
        ids = _wf2_ids(engine, t, "origem", "instagram")
        assert not ({2, 3, 4} & ids), (
            f"JSON legado que nao e objeto nao pode casar chave: {sorted(ids)}"
        )
    finally:
        with engine.begin() as c:
            c.execute(text("DROP TABLE IF EXISTS wf2_corpus"))


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
