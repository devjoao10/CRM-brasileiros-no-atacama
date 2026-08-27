# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-WF2 — o filtro de destino nao pode estourar em linha ARMAZENAVEL.

A propriedade exigida, e o unico contrato deste arquivo:

    Nenhuma operacao aplicada a coluna JSON crua pode falhar para algum valor
    que o PostgreSQL aceita guardar nela.

O que este arquivo provava ANTES (e por que isso nao bastava)
------------------------------------------------------------
A versao anterior exigia `CAST(destinos AS JSONB) @> ...` e mais nada. Ela
travava o MECANISMO — provava que o SQL compilava, nunca que ele SOBREVIVIA.
Passava verde enquanto `leads.destinos` (coluna `json`, validada so na sintaxe)
guardava valores que fazem o proprio cast estourar. Medido em PostgreSQL 16:

    ["\\u0000"]        UntranslatableCharacter    <- entra pelo POST /api/leads
    ["\\ud800"]        InvalidTextRepresentation  <- entra pelo POST /api/leads
    [1e1000000]        NumericValueOutOfRange     <- INSERT fora da ORM
    {"a": 1e1000000}   NumericValueOutOfRange     <- json legado nao-lista

Uma unica linha dessas derrubava a listagem de TODOS os leads com 500
permanente — mesmo defeito do F-043, em outra coluna. Era exatamente o erro dos
testes irmaos de campo personalizado: passavam por travarem `jsonb_each_text` /
`AS JSONB` em vez da propriedade.

O contrato agora e o CORPUS: cada valor que a coluna `json` aceita guardar tem
de devolver booleano, nunca erro. Quando o filtro nao souber processar a linha,
ela SOME do resultado (falha fechando); jamais derruba a consulta dos outros.

Rodar:  python tests/test_leads_destino_filter_dialect.py
Contra PostgreSQL de verdade (e onde a propriedade se prova):
    DATABASE_URL=postgresql+psycopg2://user:senha@host:porta/banco \
        python tests/test_leads_destino_filter_dialect.py
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

_URL_EXTERNA = os.environ.get("DATABASE_URL", "")
USANDO_SQLITE = not _URL_EXTERNA or _URL_EXTERNA.startswith("sqlite")

if USANDO_SQLITE:
    (ROOT / "scratch").mkdir(exist_ok=True)
    os.environ["DATABASE_URL"] = "sqlite:///./scratch/destino_filter_test.db"

os.environ["ENVIRONMENT"] = "development"
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ["SEED_INITIAL_ADMIN"] = "false"
os.environ["GEMINI_API_KEY"] = ""

from sqlalchemy import JSON, Column, Integer, MetaData, Table, select  # noqa: E402
from sqlalchemy.dialects import postgresql, sqlite  # noqa: E402

import app.query_filters as QF  # noqa: E402
import app.routers.leads as leads_mod  # noqa: E402
import app.routers.pipeline as pipeline_mod  # noqa: E402
import app.routers.segments as segments_mod  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover
    pass

COPIAS = (("leads.py", leads_mod), ("pipeline.py", pipeline_mod), ("segments.py", segments_mod))

_md = MetaData()
_T = Table("wf2_destinos", _md, Column("id", Integer, primary_key=True), Column("destinos", JSON))
_COL = _T.c.destinos
_QUALIFICADA = "wf2_destinos.destinos"
_RE_QUALIFICADA = re.escape(_QUALIFICADA)

_B = chr(92)  # a barra literal, montada como em app/query_filters.py

# ─── O CORPUS — a especificacao ───────────────────────────────────────────
# (rotulo, texto JSON exatamente como fica na coluna, casa com 'Atacama'?)
# Todo item aqui e ARMAZENAVEL: o INSERT na coluna `json` passa para os 19.
CORPUS = [
    ("normal",                '["Atacama"]',                                  True),
    ("dois destinos",         '["Uyuni", "Atacama"]',                         True),
    ("outro destino",         '["Uyuni"]',                                    False),
    ("caixa diferente",       '["atacama"]',                                  False),
    ("overflow numerico",     '[1e1000000]',                                  False),
    ("objeto c/ overflow",    '{"a": 1e1000000}',                             False),
    ("escalar overflow",      '1e1000000',                                    False),
    ("NUL escapado",          '["' + _B + 'u0000"]',                          False),
    ("NUL + Atacama",         '["Atacama", "' + _B + 'u0000"]',               False),
    ("substituto solto",      '["' + _B + 'ud800"]',                          False),
    ("par substituto valido", '["' + _B + 'ud83d' + _B + 'ude00"]',           False),
    ("emoji + Atacama",       '["Atacama", "' + _B + 'ud83d' + _B + 'ude00"]', True),
    ("acento escapado",       '["Ilha de P' + _B + 'u00e1scoa"]',             False),
    ("acento escap. + Atac.", '["caf' + _B + 'u00e9", "Atacama"]',            True),
    ("acento UTF-8 cru",      '["Ilha de Páscoa"]',                           False),
    ("string no topo",        '"Atacama"',                                    False),
    ("null",                  'null',                                         False),
    ("lista vazia",           '[]',                                           False),
    ("objeto vazio",          '{}',                                           False),
]

# "NUL + Atacama" e "par substituto valido" merecem leitura atenta:
#   - ["Atacama", "\u0000"] TEM Atacama e ainda assim casa False. E a falha
#     FECHANDO: a linha nao da para processar, entao ela some do filtro. O
#     custo e um lead invisivel; o beneficio e ninguem levar 500.
#   - ["\ud83d\ude00"] e o que o json.dumps/ensure_ascii da ORM
#     grava para um emoji. Tem de continuar CONVERSIVEL (o guard nao pode
#     barra-lo): a linha e legitima, e "emoji + Atacama" prova que ela
#     continua encontravel.

falhas = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        falhas.append(msg)


def _sql(modulo, is_sqlite):
    """Compila _json_list_contains do modulo forcando o ramo desejado.

    O ramo mora em app/query_filters.py — as tres copias so delegam. Por isso
    quem e forcado aqui e QF.IS_SQLITE, e nao o do router.
    """
    original = QF.IS_SQLITE
    QF.IS_SQLITE = is_sqlite
    try:
        expr = modulo._json_list_contains(_COL, "Atacama")
        dial = sqlite.dialect() if is_sqlite else postgresql.dialect()
        return str(expr.compile(dialect=dial, compile_kwargs={"literal_binds": True}))
    finally:
        QF.IS_SQLITE = original


# ─── 1. As tres copias sao a MESMA expressao ──────────────────────────────

def test_tres_copias_delegam_para_a_mesma_expressao():
    for is_sqlite in (False, True):
        gerado = {nome: _sql(m, is_sqlite) for nome, m in COPIAS}
        check(len(set(gerado.values())) == 1,
              f"as TRES copias de _json_list_contains geram o MESMO SQL "
              f"({'SQLite' if is_sqlite else 'PostgreSQL'}) — elas delegam para "
              f"app/query_filters.py, entao nao ha como divergirem. "
              f"Divergiram: {gerado if len(set(gerado.values())) > 1 else ''}")


# ─── 2. (P1) a coluna crua so chega a operacao TOTAL ──────────────────────

# Medido em PostgreSQL 16.14, linha a linha, contra o corpus inteiro: das
# operacoes abaixo NENHUMA falha para valor algum que a coluna `json` guarde.
# Toda funcao que olha o CONTEUDO (::jsonb, ->, ->>, json_array_elements_text)
# des-escapa avidamente e estoura — e so pode receber linha que passou no gate.
_TOTAIS = (
    (rf"CAST\({_RE_QUALIFICADA} AS VARCHAR\)", "::text devolve o texto cru, sem des-escapar nada"),
    (rf"json_typeof\({_RE_QUALIFICADA}\)", "le so o tipo do topo, nunca o conteudo"),
    (rf"THEN {_RE_QUALIFICADA}", "o THEN do gate — a unica saida da coluna crua"),
)


def test_postgres_coluna_crua_so_em_operacao_total():
    sql = _sql(leads_mod, is_sqlite=False)
    resto = sql
    for padrao, _ in _TOTAIS:
        resto = re.sub(padrao, "", resto)
    check(_QUALIFICADA not in resto,
          f"(P1) a coluna crua so aparece em {[d for _, d in _TOTAIS]}; "
          f"qualquer outra operacao roda linha a linha ANTES de qualquer "
          f"protecao — foi assim que `cast(coluna, JSONB)` transformou uma "
          f"linha legada em 500 permanente para TODOS os leads. Sobrou: "
          f"{resto if _QUALIFICADA in resto else ''}")


def test_postgres_nao_casta_mais_para_jsonb():
    for nome, modulo in COPIAS:
        sql = _sql(modulo, is_sqlite=False)
        check("JSONB" not in sql.upper(),
              f"{nome}: o cast para jsonb SUMIU DE PROPOSITO e nao pode voltar "
              f"— e ele que estoura com [1e1000000], que e json valido e "
              f"armazenavel. Sem cast, a classe inteira de falha de conversao "
              f"numerica deixa de existir")
        check("json_array_elements_text(" in sql,
              f"{nome}: os elementos sao expandidos NO BANCO ja como texto — "
              f"json_array_elements_text devolve 1e1000000 como texto, sem "
              f"converter para numeric (medido)")
        check("json_typeof(" in sql,
              f"{nome}: a guarda de TIPO — json_array_elements_text estoura em "
              f"objeto/escalar/null (medido), e destinos legado pode ser os tres")
        check("strpos(" in sql,
              f"{nome}: o guard de conversibilidade (allowlist que falha "
              f"FECHANDO) — sem ele, ['" + _B + "u0000'] volta a derrubar a query")


# ─── 3. Ramo SQLite permanece como era ────────────────────────────────────

def test_ramo_sqlite_inalterado():
    for nome, modulo in COPIAS:
        sql = _sql(modulo, is_sqlite=True).upper()
        check("LIKE" in sql, f"{nome}: ramo SQLite deveria usar LIKE/ILIKE, veio: {sql}")
        check("@>" not in sql, f"{nome}: ramo SQLite nao pode usar @>, veio: {sql}")
        check("JSON_ARRAY_ELEMENTS_TEXT" not in sql,
              f"{nome}: forma PostgreSQL vazou para o SQLite, veio: {sql}")


# ─── 4. Alcancabilidade: o corpus NAO e teorico ───────────────────────────

def test_veneno_entra_pela_api():
    """A validacao de escrita NAO fecha esta porta — e por isso que o guard no
    filtro e obrigatorio, e nao 'defesa em profundidade' opcional.

    `_rejeita_nul` existe em app/schemas/lead.py, mas so decora os campos de
    DICT (datas_destinos, dias_por_destino, campos_personalizados). `destinos`
    passa por `normalize_destinos`, que so faz str().strip() — e NUL nao e
    espaco em branco para o Python.
    """
    from app.schemas.lead import LeadCreate
    for rotulo, valor in (("NUL", chr(0)), ("substituto solto", chr(0xD800))):
        try:
            m = LeadCreate(nome="x", destinos=[valor])
            aceito = m.destinos == [valor]
        except Exception:
            aceito = False
        check(aceito,
              f"POST /api/leads aceita destinos=[{rotulo}] hoje — se algum dia "
              f"passar a recusar, otimo, mas o guard no filtro continua sendo "
              f"quem impede o 500 das linhas ja gravadas e das que entram por "
              f"psql/n8n/COPY de restore")


# ─── 5. A PROPRIEDADE, contra PostgreSQL de verdade ───────────────────────
# Nao ha como provar "o PostgreSQL nao estoura" sem um PostgreSQL: o SQLite nao
# tem `jsonb`, aceita tudo e nunca reproduz. Sem servidor os testes 1-4 rodam e
# sozinhos ja reprovam a implementacao anterior — o que se perde e forca, nao
# veredito.

def test_corpus_contra_postgres_real():
    if USANDO_SQLITE:
        print("  SKIP: sem DATABASE_URL de PostgreSQL — testes 1-4 cobriram a "
              "forma; a propriedade so se mede contra servidor real")
        return

    from sqlalchemy import text
    from app.database import engine

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS wf2_destinos"))
        conn.execute(text("CREATE TABLE wf2_destinos (id serial primary key, destinos json)"))

    # o predicado REAL de producao, pela porta que os routers usam
    original = QF.IS_SQLITE
    QF.IS_SQLITE = False
    try:
        predicado = leads_mod._json_list_contains(_COL, "Atacama")
    finally:
        QF.IS_SQLITE = original
    consulta = select(_T.c.id).where(predicado)
    try:
        for rotulo, bruto, esperado in CORPUS:
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM wf2_destinos"))
                # a coluna `json` guarda? se nao guardar, o corpus esta errado
                try:
                    conn.execute(text("INSERT INTO wf2_destinos (destinos) VALUES (:d)"),
                                 {"d": bruto})
                except Exception as exc:
                    check(False, f"corpus invalido: {rotulo} nao e armazenavel "
                                 f"({type(exc).__name__}) — remova-o do corpus")
                    continue
            with engine.connect() as conn:
                try:
                    achou = conn.execute(consulta).first() is not None
                except Exception as exc:
                    prim = str(exc).strip().splitlines()[0]
                    check(False, f"{rotulo}: a consulta ESTOUROU para uma linha "
                                 f"armazenavel ({bruto!r}) — {prim[:100]}. Uma "
                                 f"linha assim derruba a listagem de TODOS os "
                                 f"leads com 500 permanente (F-043)")
                    continue
            check(achou == esperado,
                  f"{rotulo}: {bruto!r} deveria casar={esperado}, casou={achou}")

        # ── termo ACENTUADO, nas duas formas em que a linha pode estar ──
        # Guarda a paridade semantica com o `@>` que saiu, e barra o atalho
        # obvio de "so comparar o texto cru": a ORM grava acento como escape
        # (json.dumps/ensure_ascii) e psql/n8n gravam UTF-8 direto. As DUAS
        # formas tem de ser encontradas pelo MESMO termo digitado.
        QF.IS_SQLITE = False
        try:
            acentuado = select(_T.c.id).where(
                leads_mod._json_list_contains(_COL, "Ilha de Páscoa"))
        finally:
            QF.IS_SQLITE = original
        for forma, bruto in (("escape ensure_ascii (ORM)",
                              '["Ilha de P' + _B + 'u00e1scoa"]'),
                             ("UTF-8 cru (psql/n8n)", '["Ilha de Páscoa"]')):
            with engine.begin() as conn:
                conn.execute(text("DELETE FROM wf2_destinos"))
                conn.execute(text("INSERT INTO wf2_destinos (destinos) VALUES (:d)"),
                             {"d": bruto})
            with engine.connect() as conn:
                achou = conn.execute(acentuado).first() is not None
            check(achou, f"termo acentuado encontra a linha gravada como "
                         f"{forma} ({bruto!r}) — comparar TEXTO CRU em vez de "
                         f"expandir os elementos quebraria exatamente isto")
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS wf2_destinos"))


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    print(f"dialeto: {'SQLite' if USANDO_SQLITE else 'PostgreSQL'}\n")
    for fn in ALL_TESTS:
        print(fn.__name__)
        try:
            fn()
        except Exception as exc:
            falhas.append(fn.__name__)
            print(f"  ERROR {type(exc).__name__}: {exc}")
        print()
    print(f"{len(falhas)} falha(s)")
    sys.exit(1 if falhas else 0)
