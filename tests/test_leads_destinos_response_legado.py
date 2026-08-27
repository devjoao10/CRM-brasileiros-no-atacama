# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-WF2 — `destinos` legado nao pode derrubar a RESPOSTA.

A propriedade exigida, e o unico contrato deste arquivo:

    Um Lead com `destinos` legado ARMAZENAVEL nao pode transformar uma
    resposta de colecao em HTTP 500. A linha ruim degrada sozinha; as
    vizinhas validas continuam sendo devolvidas, com o conteudo certo.

Isto NAO e o defeito da camada de QUERY corrigido em 090cbfb (o
`cast(json -> jsonb)` que derrubava a CONSULTA). Aqui a consulta VOLTA, e o
500 nasce depois, quando `LeadResponse`/`LeadCardResponse` — os dois
declarados `Optional[list[str]]` — recusam o valor que veio do banco:

    pydantic_core._pydantic_core.ValidationError: 1 validation error for LeadResponse
    destinos.0
      Input should be a valid string [type=string_type, input_value=inf, input_type=float]

Medido em SQLite e em PostgreSQL 16, com uma linha ruim e uma linha VALIDA
lado a lado: TODA a colecao respondia 500 — `GET /api/leads`,
`GET /api/leads/segment`, `POST /api/segments/preview` e o Kanban.

Por que a coluna aceita isso: `leads.destinos` e `Column(JSON)`, que no
PostgreSQL compila para o tipo `json` (valida so a SINTAXE). `'[1e1000000]'`
e JSON sintaticamente valido — `::jsonb` estoura, `::json` guarda — e volta
para o Python como `[inf]`. Pela API isso nao entra (o `normalize_destinos`
da escrita coage tudo com `str()`); entra por psql, COPY/restore e carga
fora da ORM.

A SEMANTICA publica escolhida segue o contrato de ESCRITA que ja existe em
`normalize_destinos` — string no topo e uma lista separada por virgula —
com UMA excecao deliberada: onde a escrita FABRICARIA nome de destino
(`str(inf)` -> "inf", `str(None)` -> "None", `str(123)` -> "123"), a
resposta DESCARTA o elemento. Resposta nao inventa destino.

Rodar:  python tests/test_leads_destinos_response_legado.py
Contra PostgreSQL 16 de verdade:
    DATABASE_URL=postgresql+psycopg2://user:senha@host:porta/banco \
        python tests/test_leads_destinos_response_legado.py
"""
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_URL_EXTERNA = os.environ.get("DATABASE_URL", "")
USANDO_SQLITE = not _URL_EXTERNA or _URL_EXTERNA.startswith("sqlite")

if USANDO_SQLITE:
    (ROOT / "scratch").mkdir(exist_ok=True)
    _ARQ = ROOT / "scratch" / "destinos_response_legado.db"
    if _ARQ.exists():
        try:
            _ARQ.unlink()
        except PermissionError:
            pass
    os.environ["DATABASE_URL"] = "sqlite:///./scratch/destinos_response_legado.db"

os.environ.update({
    "ENVIRONMENT": "development",
    "SEED_INITIAL_ADMIN": "true",
    "ADMIN_INITIAL_EMAIL": "admin@local.test",
    "ADMIN_INITIAL_PASSWORD": "LocalSmoke123!",
    "GEMINI_API_KEY": "",
    "SECRET_KEY": "test-secret-key",
})

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover
    pass

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.lead import Lead  # noqa: E402
from app.models.pipeline import Funnel, FunnelEntry  # noqa: E402
from app.schemas.lead import DESTINOS_PRINCIPAIS  # noqa: E402

falhas = []


def check(cond, msg):
    print(f"  {'ok  ' if cond else 'FALHA'} {msg}")
    if not cond:
        falhas.append(msg)


# ─── O CORPUS — a especificacao ───────────────────────────────────────────
#
# (rotulo, JSON CRU gravado na coluna, `destinos` que a resposta deve trazer)
#
# Nenhum destes entra pela API: `normalize_destinos` coage lista com `str()` e
# o Pydantic recusa dict/escalar com 422. Todos entram por psql / COPY /
# restore / carga fora da ORM, e os 11 foram medidos como GRAVAVEIS tanto em
# SQLite quanto em PostgreSQL 16.
CORPUS = [
    ("lista valida",      '["Atacama"]',        ["Atacama"]),
    ("json null",         "null",               None),
    # string no topo: e o UNICO formato nao-lista que a ESCRITA sempre aceitou
    # ("Accept either a single string or a list of strings"), e carrega nome de
    # destino de verdade. A resposta le do mesmo jeito: separa por virgula.
    ("string no topo",    '"Atacama"',          ["Atacama"]),
    ("string com virgula", '"Atacama, Uyuni"',  ["Atacama", "Uyuni"]),
    # objeto: a escrita SEMPRE devolveu 422 para dict. Nao ha nome de destino
    # aqui para recuperar — vira ausencia de destino, nao um nome inventado.
    ("objeto",            '{"a": 1}',           None),
    # os quatro abaixo sao onde a escrita FABRICARIA nome. A resposta descarta.
    ("float overflow",    "[1e1000000]",        []),
    ("int",               "[123]",              []),
    ("bool",              "[true]",             []),
    ("null interno",      "[null]",             []),
    ("lista vazia",       "[]",                 []),
    ("lista aninhada",    '[["x"]]',            []),
    ("mista",             '["Atacama", 123]',   ["Atacama"]),
]

# Nomes que a coacao `str()` da ESCRITA produziria. Nenhum pode aparecer em
# resposta alguma: sao nome de destino INVENTADO a partir de valor nao-textual.
FABRICADOS = ["inf", "None", "True", "123", "['x']", "[object Object]"]

# AUDIT-2026-08-WF2 — "Toconao" nao esta em DESTINOS_PRINCIPAIS de
# proposito: `GET /api/leads/destinos` mistura os principais aos destinos
# ja cadastrados, entao sem um nome que SO vem da linha vizinha o "a
# vizinha continua aparecendo" passaria de graca naquela rota.
VIZINHA_DESTINOS = ["Uyuni", "Santiago", "Toconao"]
VIZINHA_WHATSAPP = "+5511988887777"

_C = {}


def _setup():
    """Semeia UM lead por valor do corpus + a vizinha VALIDA, todos no mesmo
    funil (e o funil que faz a linha ruim e a boa dividirem a mesma resposta)."""
    if _C:
        return _C["client"], _C

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM funnel_entries"))
        db.execute(text("DELETE FROM lead_history"))
        db.execute(text("DELETE FROM lead_tags"))
        db.execute(text("DELETE FROM leads"))
        db.commit()

        # `funnels.nome` e unico: contra um PostgreSQL persistente a segunda
        # execucao reaproveita o funil da primeira em vez de colidir.
        funnel = db.query(Funnel).filter(Funnel.nome == "WF2 Funil").first()
        if funnel is None:
            funnel = Funnel(nome="WF2 Funil", etapas=[{"id": "e1", "nome": "E1"}])
            db.add(funnel)
            db.commit()

        ids = {}
        for pos, (rotulo, _, _) in enumerate(CORPUS):
            lead = Lead(nome=f"WF2 {rotulo}", destinos=["Atacama"], is_active=True)
            db.add(lead)
            db.flush()
            ids[rotulo] = lead.id
            db.add(FunnelEntry(lead_id=lead.id, funnel_id=funnel.id,
                               etapa_id="e1", posicao=pos))

        vizinha = Lead(nome="WF2 VIZINHA VALIDA", destinos=list(VIZINHA_DESTINOS),
                       whatsapp=VIZINHA_WHATSAPP, is_active=True)
        db.add(vizinha)
        db.flush()
        db.add(FunnelEntry(lead_id=vizinha.id, funnel_id=funnel.id,
                           etapa_id="e1", posicao=len(CORPUS)))
        db.commit()

        _C.update({"ids": ids, "vizinha": vizinha.id, "funnel": funnel.id})
    finally:
        db.close()

    # raise_server_exceptions=False: queremos o STATUS que o cliente ve, nao a
    # excecao re-levantada pelo TestClient.
    client = TestClient(app, raise_server_exceptions=False)
    client.__enter__()
    r = client.post("/api/auth/login",
                    json={"email": "admin@local.test", "password": "LocalSmoke123!"})
    assert r.status_code == 200, f"login: {r.status_code} {r.text[:200]}"
    _C["client"] = client
    return client, _C


def _grava_cru(lead_id, cru):
    """Grava o JSON CRU na coluna, sem passar pela serializacao da ORM."""
    db = SessionLocal()
    try:
        db.execute(text("UPDATE leads SET destinos = :v"), {"v": '["Atacama"]'})
        db.execute(text("UPDATE leads SET destinos = :v WHERE id = :i"),
                   {"v": json.dumps(VIZINHA_DESTINOS), "i": _C["vizinha"]})
        db.execute(text("UPDATE leads SET destinos = :v WHERE id = :i"),
                   {"v": cru, "i": lead_id})
        db.commit()
    finally:
        db.close()


def _colecoes(client, funnel_id):
    """Toda resposta de COLECAO que expoe `destinos`, com o caminho ate a lista
    de leads de cada uma."""
    return [
        ("GET /api/leads",
         lambda: client.get("/api/leads?limit=500"),
         lambda b: b["leads"]),
        ("GET /api/leads/segment",
         lambda: client.get("/api/leads/segment?limit=500"),
         lambda b: b["leads"]),
        ("POST /api/segments/preview",
         lambda: client.post("/api/segments/preview", json={"is_active": True}),
         lambda b: b["leads"]),
        # Kanban: NAO passa por LeadResponse — usa LeadCardResponse.
        ("GET /api/pipeline/board/{id}",
         lambda: client.get(f"/api/pipeline/board/{funnel_id}"),
         lambda b: [c for e in b["stages"] for c in e["leads"]]),
        ("GET /api/pipeline/board/{id}/stage/{etapa}",
         lambda: client.get(f"/api/pipeline/board/{funnel_id}/stage/e1?limit=100"),
         lambda b: b["items"]),
    ]


# ─────────────────────────────────────────────────────────────────────────
# 1. A propriedade: linha legada nao derruba a COLECAO
# ─────────────────────────────────────────────────────────────────────────

def test_colecao_sobrevive_a_toda_linha_legada_armazenavel():
    client, c = _setup()
    for rotulo, cru, _ in CORPUS:
        _grava_cru(c["ids"][rotulo], cru)
        for nome, chamada, extrai in _colecoes(client, c["funnel"]):
            r = chamada()
            check(r.status_code == 200,
                  f"{rotulo} ({cru}) -> {nome} responde 200 (veio {r.status_code})")
            if r.status_code != 200:
                continue
            leads = extrai(r.json())
            achou = [x for x in leads
                     if x.get("id", x.get("lead_id")) == c["vizinha"]]
            check(len(achou) == 1,
                  f"{rotulo} -> {nome} ainda devolve a VIZINHA valida")
            if achou:
                check(achou[0]["destinos"] == VIZINHA_DESTINOS,
                      f"{rotulo} -> {nome} vizinha com destinos {VIZINHA_DESTINOS} "
                      f"(veio {achou[0]['destinos']!r})")


# ─────────────────────────────────────────────────────────────────────────
# 2. A propriedade: linha legada nao derruba a resposta INDIVIDUAL
# ─────────────────────────────────────────────────────────────────────────

def test_lead_individual_sobrevive_a_toda_linha_legada_armazenavel():
    client, c = _setup()
    for rotulo, cru, _ in CORPUS:
        lid = c["ids"][rotulo]
        _grava_cru(lid, cru)
        for nome, chamada in (
            ("GET /api/leads/{id}", lambda: client.get(f"/api/leads/{lid}")),
            # PUT sem `destinos` no corpo: `exclude_unset` deixa a coluna como
            # esta, e a resposta serializa o valor legado do mesmo jeito.
            ("PUT /api/leads/{id}",
             lambda: client.put(f"/api/leads/{lid}", json={"num_viajantes": 2})),
        ):
            r = chamada()
            check(r.status_code == 200,
                  f"{rotulo} ({cru}) -> {nome} responde 200 (veio {r.status_code})")

    # by-whatsapp so existe na vizinha; envenena ELA para cobrir esta rota.
    for rotulo, cru, esperado in CORPUS:
        _grava_cru(c["vizinha"], cru)
        r = client.get(f"/api/leads/by-whatsapp/{VIZINHA_WHATSAPP}")
        check(r.status_code == 200,
              f"{rotulo} ({cru}) -> GET /api/leads/by-whatsapp responde 200 "
              f"(veio {r.status_code})")
        if r.status_code == 200:
            check(r.json()["destinos"] == esperado,
                  f"{rotulo} -> by-whatsapp devolve {esperado!r} "
                  f"(veio {r.json()['destinos']!r})")


# ─────────────────────────────────────────────────────────────────────────
# 3. A semantica publica de cada formato
# ─────────────────────────────────────────────────────────────────────────

def test_semantica_publica_de_cada_formato_legado():
    client, c = _setup()
    for rotulo, cru, esperado in CORPUS:
        lid = c["ids"][rotulo]
        _grava_cru(lid, cru)
        r = client.get(f"/api/leads/{lid}")
        if r.status_code != 200:
            check(False, f"{rotulo} ({cru}) -> GET /api/leads/{{id}} respondeu "
                         f"{r.status_code}, nao da para conferir a semantica")
            continue
        check(r.json()["destinos"] == esperado,
              f"{rotulo} ({cru}) -> destinos == {esperado!r} "
              f"(veio {r.json()['destinos']!r})")


def test_resposta_nunca_fabrica_nome_de_destino():
    client, c = _setup()
    for rotulo, cru, _ in CORPUS:
        lid = c["ids"][rotulo]
        _grava_cru(lid, cru)
        for nome, chamada, extrai in _colecoes(client, c["funnel"]):
            r = chamada()
            if r.status_code != 200:
                continue
            nomes = [d for x in extrai(r.json()) for d in (x.get("destinos") or [])]
            sujos = [d for d in nomes if d in FABRICADOS]
            check(not sujos,
                  f"{rotulo} ({cru}) -> {nome} nao inventa nome de destino "
                  f"(apareceu {sujos!r})")


def test_dado_bruto_nao_e_alterado_pela_leitura():
    """A correcao e de RESPOSTA. Ler o lead nao pode reescrever a coluna."""
    client, c = _setup()
    for rotulo, cru, _ in CORPUS:
        lid = c["ids"][rotulo]
        _grava_cru(lid, cru)
        client.get(f"/api/leads/{lid}")
        client.get("/api/leads?limit=500")
        db = SessionLocal()
        try:
            # CAST para texto nos DOIS dialetos: o psycopg2 ja desserializa a
            # coluna `json`, e ai `"Atacama"` voltaria como str indistinguivel
            # do texto cru.
            depois = db.execute(text("SELECT CAST(destinos AS TEXT) FROM leads "
                                     "WHERE id = :i"), {"i": lid}).scalar()
        finally:
            db.close()
        check(json.loads(depois) == json.loads(cru),
              f"{rotulo} ({cru}) -> coluna intacta apos a leitura (veio {depois!r})")


# ─────────────────────────────────────────────────────────────────────────
# 4. Lead VALIDO sai identico ao de hoje
# ─────────────────────────────────────────────────────────────────────────

def test_lead_valido_sai_semanticamente_identico():
    """Nenhuma normalizacao a mais: sem strip, sem dedupe, sem reordenar, sem
    separar por virgula DENTRO de um elemento de lista."""
    client, c = _setup()
    validos = [
        ["Atacama"],
        ["Atacama", "Uyuni", "Santiago"],
        [],
        None,
        ["  Atacama  "],              # espaco nas pontas: preservado
        ["Atacama, Uyuni"],           # virgula DENTRO do elemento: NAO separa
        [""],                         # string vazia: preservada
        ["Sao Pedro de Atacama"],
        ["Atacama", "Atacama"],       # duplicado: ordem e repeticao preservadas
    ]
    lid = c["ids"]["lista valida"]
    for v in validos:
        db = SessionLocal()
        try:
            lead = db.query(Lead).filter(Lead.id == lid).first()
            lead.destinos = v
            db.commit()
        finally:
            db.close()
        r = client.get(f"/api/leads/{lid}")
        check(r.status_code == 200, f"valido {v!r} -> 200 (veio {r.status_code})")
        if r.status_code == 200:
            check(r.json()["destinos"] == v,
                  f"valido {v!r} -> sai identico (veio {r.json()['destinos']!r})")


# ─────────────────────────────────────────────────────────────────────────
# 5. Semantica que so existe no PostgreSQL
# ─────────────────────────────────────────────────────────────────────────

def test_postgres_json_aceita_o_que_jsonb_recusa():
    """`[1e1000000]` e a prova de que o tipo da COLUNA e o que deixa a linha
    existir: `json` valida so a sintaxe e guarda; `jsonb` converte para numeric
    e estoura. So faz sentido contra PostgreSQL de verdade."""
    if USANDO_SQLITE:
        print("  PULADO — exige PostgreSQL real. Rode com "
              "DATABASE_URL=postgresql+psycopg2://... para verificar este caso.")
        return
    with engine.connect() as conn:
        aceito = conn.execute(text("SELECT '[1e1000000]'::json")).scalar()
        check(aceito is not None,
              "tipo `json` GUARDA [1e1000000] (e por isso a linha existe)")
        try:
            conn.execute(text("SELECT '[1e1000000]'::jsonb"))
            check(False, "tipo `jsonb` deveria RECUSAR [1e1000000]")
        except Exception as exc:  # noqa: BLE001
            check("overflow" in str(exc).lower(),
                  f"tipo `jsonb` recusa [1e1000000] ({str(exc).splitlines()[0][:60]})")


# ────────────────────────────────────────────────────────────────────────
# 6. As rotas de AGREGACAO — outro mecanismo, mesmo finding
# ────────────────────────────────────────────────────────────────────────
#
# As secoes 1-5 cobrem SERIALIZACAO: o valor legado atravessa `LeadResponse` /
# `LeadCardResponse`. Estas duas rotas nao serializam lead nenhum — elas
# AGREGAM os elementos crus, e por isso a correcao de schema nao as alcanca:
#
#   GET /api/leads/destinos      poe cada elemento num `set` e faz `sorted()`
#   GET /api/analytics/reports   usa cada elemento como CHAVE de dict
#
# `sorted()` sobre {str, int} estoura TypeError; `set.add(["x"])` estoura
# TypeError (unhashable); chave de dict `["x"]` estoura TypeError; e uma
# chave `inf` sobrevive ate o `json.dumps(..., allow_nan=False)` do
# JSONResponse do FastAPI.
#
# `GET /api/leads/destinos` alimenta o DROPDOWN do filtro de destino: com uma
# linha legada, o filtro inteiro deixa de carregar.


def _agregacoes(client):
    """Toda resposta que AGREGA `destinos` de varios leads, com o caminho ate a
    lista de nomes de destino que ela publica."""
    return [
        # o dropdown do filtro de destino
        ("GET /api/leads/destinos",
         lambda: client.get("/api/leads/destinos"),
         lambda b: b["destinos"]),
        # o relatorio consolidado (breakdown destino -> contagem)
        ("GET /api/analytics/reports",
         lambda: client.get("/api/analytics/reports"),
         lambda b: list(b["breakdown"]["destinos"].keys())),
    ]


def _universo(rotulo_alvo, esperado, com_principais):
    """Os nomes de destino que a agregacao DEVE publicar com o alvo envenenado.

    `_grava_cru` deixa todo lead em `["Atacama"]`, a vizinha em
    VIZINHA_DESTINOS e so o alvo com o valor legado — entao o conjunto
    publicado e exatamente a uniao dos destinos publicos de todas as linhas.
    Igualdade EXATA, nao `in`: e o que reprova tanto o nome fabricado
    ("123", "true", "Infinity" — as chaves que o `json.dumps` produziria) como
    o destino legitimo que sumisse junto com a linha ruim.
    """
    universo = {"Atacama"} | set(VIZINHA_DESTINOS) | set(esperado or [])
    if com_principais:
        universo |= set(DESTINOS_PRINCIPAIS)
    return universo


def test_agregacao_sobrevive_a_toda_linha_legada_armazenavel():
    client, c = _setup()
    for rotulo, cru, esperado in CORPUS:
        _grava_cru(c["ids"][rotulo], cru)
        for nome, chamada, extrai in _agregacoes(client):
            r = chamada()
            check(r.status_code == 200,
                  f"{rotulo} ({cru}) -> {nome} responde 200 (veio {r.status_code})")
            if r.status_code != 200:
                continue
            publicados = extrai(r.json())
            com_principais = nome == "GET /api/leads/destinos"
            check(set(publicados) == _universo(rotulo, esperado, com_principais),
                  f"{rotulo} ({cru}) -> {nome} publica exatamente "
                  f"{sorted(_universo(rotulo, esperado, com_principais))!r} "
                  f"(veio {sorted(map(repr, publicados))!r})")
            for d in VIZINHA_DESTINOS:
                check(d in publicados,
                      f"{rotulo} ({cru}) -> {nome} ainda publica o destino "
                      f"legitimo {d!r} da linha vizinha")


def test_agregacao_nunca_fabrica_nome_de_destino():
    client, c = _setup()
    for rotulo, cru, _ in CORPUS:
        _grava_cru(c["ids"][rotulo], cru)
        for nome, chamada, extrai in _agregacoes(client):
            r = chamada()
            if r.status_code != 200:
                continue
            publicados = extrai(r.json())
            # `json.dumps` coage CHAVE de dict: 123 -> "123", True -> "true",
            # inf -> "Infinity". Nenhuma dessas e nome de destino.
            sujos = [d for d in publicados
                     if d in FABRICADOS or d in ("true", "false", "Infinity")]
            check(not sujos,
                  f"{rotulo} ({cru}) -> {nome} nao inventa nome de destino "
                  f"(apareceu {sujos!r})")
            check(all(isinstance(d, str) for d in publicados),
                  f"{rotulo} ({cru}) -> {nome} publica so string "
                  f"(veio {[type(d).__name__ for d in publicados]!r})")


ALL_TESTS = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    print(f"dialeto: {'SQLite' if USANDO_SQLITE else 'PostgreSQL'}\n")
    for fn in ALL_TESTS:
        print(fn.__name__)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            falhas.append(fn.__name__)
            print(f"  ERROR {type(exc).__name__}: {exc}")
        print()
    print(f"{len(falhas)} falha(s)")
    sys.exit(1 if falhas else 0)
