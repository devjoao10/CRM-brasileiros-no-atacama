# -*- coding: utf-8 -*-
"""
LEADS-KEYSET-01 — paginacao keyset, filtros server-side e campo personalizado
em SQL na pagina de Leads.

Tres problemas atacados:

1. ORDEM INDEFINIDA. `ORDER BY created_at DESC` sem desempate deixa a ordem
   entre timestamps iguais a criterio do banco. A base tem empates de sobra
   (import em lote grava varios leads no mesmo segundo). Paginando por OFFSET
   sobre uma ordem instavel, a mesma linha pode cair em duas paginas ou em
   nenhuma. O cursor fixa (created_at, id) — id e unico, logo a ordem e total.

2. CAMPO PERSONALIZADO EM PYTHON. GET /api/leads/segment carregava TODO lead
   que casasse os demais filtros e varria o dict: 19.000 objetos ORM medidos
   para devolver 50. Agora e EXISTS no banco (mesmo predicado de Segmentacoes,
   em app/query_filters.py).

3. TELEFONE. Nao existia filtro, e "tem telefone" nao e `IS NOT NULL`: a base
   tem "" e "   " vindos de import.

O oraculo dos filtros e Python puro sobre um snapshot conhecido, e a
comparacao e de CONJUNTOS DE IDs — nao de total.

Os testes de JavaScript rodam o codigo extraido do template no node, com DOM
stubado. Nao ha Playwright/Selenium/Cypress no repo e este pacote nao
justifica instalar um; o que da para provar sem browser e a maquina de estado
(reset de cursor, guarda de clique duplo, aborto do request antigo).

Rodar:  python tests/test_leads_keyset_pagination.py
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from urllib.parse import urlencode

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

pathlib.Path("scratch").mkdir(exist_ok=True)
os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": "sqlite:///./scratch/leads_keyset.db",
    "SEED_INITIAL_ADMIN": "true",
    "ADMIN_INITIAL_EMAIL": "admin@local.test",
    "ADMIN_INITIAL_PASSWORD": "LocalSmoke123!",
    "GEMINI_API_KEY": "",
})

from sqlalchemy import event, select, func  # noqa: E402

HTML = pathlib.Path("templates/leads.html").read_text(encoding="utf-8")
FONTE = pathlib.Path("app/routers/leads.py").read_text(encoding="utf-8")

N = 120          # > 2 lotes de 50
_C = {}


# ─────────────────────────────────────────────────────────────────────────
# Semente
# ─────────────────────────────────────────────────────────────────────────

def _setup():
    if _C:
        return _C["client"], _C["d"]

    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import SessionLocal, Base, engine
    from app.models.lead import Lead
    from app.models.tag import Tag, lead_tags
    from app.models.user import User
    from app.models.pipeline import Funnel, FunnelEntry
    from datetime import date, datetime, timedelta, timezone

    f = pathlib.Path("scratch/leads_keyset.db")
    if f.exists():
        try:
            f.unlink()
        except PermissionError:
            pass
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        u = User(nome="Resp", email="resp@local.test", hashed_password="x")
        db.add(u)
        t1 = Tag(nome="VIP", cor="#111")
        t2 = Tag(nome="Frio", cor="#222")
        db.add_all([t1, t2])
        f1 = Funnel(nome="F1", etapas=[{"id": "e1", "nome": "E1"}])
        f2 = Funnel(nome="F2", etapas=[{"id": "e1", "nome": "E1"}])
        db.add_all([f1, f2])
        db.commit()

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        linhas = []
        for i in range(N):
            # telefone: 1/4 None, 1/4 "", 1/4 "   ", 1/4 valido
            tel = [None, "", "   ", f"+5551{i:09d}"][i % 4]
            linhas.append({
                "nome": f"Lead {i:03d}" + (" Joao" if i % 7 == 0 else ""),
                "email": f"lead{i}@x.test" if i % 5 else None,
                "whatsapp": tel,
                "destinos": [["Atacama"], ["Uyuni"], ["Atacama", "Santiago"]][i % 3],
                "campos_personalizados": {
                    "origem": ["Instagram", "Facebook", "Google"][i % 3],
                    "obs": f"n{i}",
                },
                "status_venda": ["em_negociacao", "venda", "perda"][i % 3],
                "is_active": True,
                "responsavel_id": u.id if i % 3 == 0 else None,
                "data_chegada": date(2026, 1, 1) + timedelta(days=i % 60),
                # EMPATE PROPOSITAL: 10 leads por timestamp
                "created_at": base + timedelta(seconds=i // 10),
            })
        db.bulk_insert_mappings(Lead, linhas)
        db.commit()

        leads = db.query(Lead).order_by(Lead.id).all()
        ids = [l.id for l in leads]
        db.execute(lead_tags.insert(),
                   [{"lead_id": i, "tag_id": t1.id} for i in ids[::4]])
        db.execute(lead_tags.insert(),
                   [{"lead_id": i, "tag_id": t2.id} for i in ids[::6]])
        # um lead em DOIS funis: se joinedload multiplicar linha, o limit quebra
        db.add_all([
            FunnelEntry(lead_id=ids[0], funnel_id=f1.id, etapa_id="e1", posicao=0),
            FunnelEntry(lead_id=ids[0], funnel_id=f2.id, etapa_id="e1", posicao=0),
            FunnelEntry(lead_id=ids[1], funnel_id=f1.id, etapa_id="e1", posicao=1),
        ])
        db.commit()

        snap = [{
            "id": l.id, "nome": l.nome, "email": l.email, "whatsapp": l.whatsapp,
            "destinos": l.destinos, "status": l.status_venda,
            "responsavel_id": l.responsavel_id, "cp": l.campos_personalizados,
            "chegada": l.data_chegada.isoformat(),
            "created_at": l.created_at,
        } for l in leads]
        d = {"leads": snap, "resp_id": u.id, "tag_vip": t1.id, "tag_frio": t2.id,
             "funnel": f1.id, "dois_funis": ids[0]}
    finally:
        db.close()

    client = TestClient(app)
    client.__enter__()
    r = client.post("/api/auth/login",
                    json={"email": "admin@local.test", "password": "LocalSmoke123!"})
    assert r.status_code == 200, f"login: {r.status_code} {r.text}"
    _C["client"], _C["d"] = client, d
    return client, d


def _get(client, **params):
    # urlencode e obrigatorio: '+' cru numa query string decodifica como ESPACO,
    # e telefone comeca com '+'. O frontend usa URLSearchParams, que ja encoda.
    qs = urlencode({k: v for k, v in params.items() if v is not None})
    r = client.get(f"/api/leads?{qs}")
    assert r.status_code == 200, f"{qs}: {r.status_code} {r.text[:200]}"
    return r.json()


def _varre(client, **params):
    """Percorre TODO o conjunto por cursor. Devolve a ordem completa de ids."""
    ordem, cursor, voltas = [], None, 0
    while True:
        b = _get(client, cursor=cursor, **params)
        ordem += [l["id"] for l in b["leads"]]
        voltas += 1
        assert voltas < 200, "cursor nao terminou: possivel loop"
        if not b["has_more"]:
            assert b["next_cursor"] is None, "sem has_more nao pode sobrar cursor"
            return ordem, b["total"]
        cursor = b["next_cursor"]
        assert cursor, "has_more=true exige next_cursor"


# ─────────────────────────────────────────────────────────────────────────
# 1. Paginacao por cursor
# ─────────────────────────────────────────────────────────────────────────

def test_primeiro_lote_de_50_e_has_more():
    client, d = _setup()
    b = _get(client, limit=50)
    assert len(b["leads"]) == 50, f"esperado 50, veio {len(b['leads'])}"
    assert b["has_more"] is True and b["next_cursor"]
    assert b["total"] == N


def test_segundo_e_ultimo_lote():
    client, d = _setup()
    b1 = _get(client, limit=50)
    b2 = _get(client, limit=50, cursor=b1["next_cursor"])
    assert len(b2["leads"]) == 50 and b2["has_more"] is True
    b3 = _get(client, limit=50, cursor=b2["next_cursor"])
    assert len(b3["leads"]) == N - 100, f"ultimo lote: {len(b3['leads'])}"
    assert b3["has_more"] is False and b3["next_cursor"] is None


def test_varredura_sem_duplicados_e_sem_perdas():
    """O nucleo: 120 leads em 12 timestamps empatados, lotes de 7."""
    client, d = _setup()
    ordem, total = _varre(client, limit=7)
    assert total == N
    assert len(ordem) == N, f"linhas devolvidas {len(ordem)} != {N} (perdas)"
    assert len(set(ordem)) == N, (
        f"DUPLICADOS: {len(ordem) - len(set(ordem))} — o desempate falhou")
    assert set(ordem) == {l["id"] for l in d["leads"]}, "conjunto diferente da base"


def test_ordem_e_created_at_desc_id_desc():
    client, d = _setup()
    ordem, _ = _varre(client, limit=13)
    por_id = {l["id"]: l for l in d["leads"]}
    chaves = [(por_id[i]["created_at"], i) for i in ordem]
    assert chaves == sorted(chaves, reverse=True), (
        "ordem nao e (created_at DESC, id DESC)")


def test_empates_de_created_at_existem_de_verdade():
    """Se a semente perder os empates, os testes acima viram decorativos."""
    client, d = _setup()
    from collections import Counter
    c = Counter(l["created_at"] for l in d["leads"])
    assert max(c.values()) >= 10, f"semente sem empates suficientes: {c.most_common(1)}"


def test_cursor_invalido_vira_400():
    client, d = _setup()
    for ruim in ("xxx", "!!!", "YWJj", ""):
        r = client.get(f"/api/leads?limit=5&cursor={ruim}")
        assert r.status_code in (200, 400), f"cursor {ruim!r}: {r.status_code}"
        if ruim:
            assert r.status_code == 400, f"cursor {ruim!r} devia dar 400"


def test_lead_em_dois_funis_nao_encolhe_a_pagina():
    """joinedload em colecao pode multiplicar linha e comer o limit."""
    client, d = _setup()
    b = _get(client, limit=50)
    ids = [l["id"] for l in b["leads"]]
    assert len(ids) == 50 and len(set(ids)) == 50, "pagina com linha duplicada"
    alvo = next((l for l in b["leads"] if l["id"] == d["dois_funis"]), None)
    if alvo:
        assert len(alvo["funis"]) == 2, "os dois funis tem que aparecer no payload"


# ─────────────────────────────────────────────────────────────────────────
# 2. Filtros — paridade de conjunto contra oraculo Python
# ─────────────────────────────────────────────────────────────────────────

def _oraculo(leads, resp_id, **f):
    out = set()
    for l in leads:
        if "search" in f:
            q = f["search"].lower()
            campos = [l["nome"] or "", l["email"] or "", l["whatsapp"] or ""]
            if not any(q in (c or "").lower() for c in campos):
                continue
        if "destino" in f and f["destino"] not in (l["destinos"] or []):
            continue
        if "status_venda" in f and l["status"] != f["status_venda"]:
            continue
        if "responsavel_id" in f:
            esperado = None if f["responsavel_id"] == 0 else resp_id
            if l["responsavel_id"] != esperado:
                continue
        if "data_chegada_de" in f and l["chegada"] < f["data_chegada_de"]:
            continue
        if "data_chegada_ate" in f and l["chegada"] > f["data_chegada_ate"]:
            continue
        if "com_telefone" in f:
            tem = bool((l["whatsapp"] or "").strip())
            if tem != (f["com_telefone"] == "true"):
                continue
        out.add(l["id"])
    return out


FILTROS = [
    {"search": "Joao"},
    {"search": "lead10@x.test"},
    {"search": "+5551000000003"},
    {"search": "zzzz-nao-existe"},
    {"destino": "Atacama"},
    {"destino": "Santiago"},
    {"status_venda": "venda"},
    {"data_chegada_de": "2026-01-15", "data_chegada_ate": "2026-02-10"},
    {"com_telefone": "true"},
    {"com_telefone": "false"},
    {"destino": "Atacama", "com_telefone": "true"},
    {"search": "Joao", "destino": "Atacama", "com_telefone": "true"},
]


def test_todos_os_filtros_batem_conjunto_com_o_oraculo():
    client, d = _setup()
    problemas = []
    casos = FILTROS + [{"responsavel_id": d["resp_id"]}, {"responsavel_id": 0},
                       {"responsavel_id": 0, "destino": "Uyuni"}]
    for f in casos:
        esperado = _oraculo(d["leads"], d["resp_id"], **f)
        obtido, total = _varre(client, limit=17, **f)
        obtido = set(obtido)
        if obtido != esperado or total != len(esperado):
            problemas.append(
                f"{f}: esperado {len(esperado)} obtido {len(obtido)} total={total}; "
                f"sobrando {sorted(obtido - esperado)[:5]} "
                f"faltando {sorted(esperado - obtido)[:5]}")
    assert not problemas, "filtros divergiram:\n  " + "\n  ".join(problemas)


def test_filtro_aplicado_ANTES_da_paginacao():
    """
    Se o filtro rodasse depois do LIMIT, a 1a pagina traria menos que o limite
    e o total nao bateria com a varredura.
    """
    client, d = _setup()
    esperado = _oraculo(d["leads"], d["resp_id"], destino="Atacama")
    b = _get(client, limit=10, destino="Atacama")
    assert b["total"] == len(esperado), (
        f"total {b['total']} != conjunto {len(esperado)}: filtro depois do LIMIT?")
    assert len(b["leads"]) == 10, "pagina cheia: o filtro tem que estar no SQL"
    assert all(x["id"] in esperado for x in b["leads"]), "linha fora do filtro"


def test_com_e_sem_telefone_particionam_a_base():
    client, d = _setup()
    com, tcom = _varre(client, limit=40, com_telefone="true")
    sem, tsem = _varre(client, limit=40, com_telefone="false")
    assert set(com) & set(sem) == set(), "lead em com E sem telefone"
    assert tcom + tsem == N, f"{tcom} + {tsem} != {N}: sobrou ou faltou lead"
    por_id = {l["id"]: l for l in d["leads"]}
    for i in com:
        assert (por_id[i]["whatsapp"] or "").strip(), (
            f"lead {i} tem whatsapp {por_id[i]['whatsapp']!r} e entrou em 'com'")
    vazios = [i for i in sem if (por_id[i]["whatsapp"] or "").strip()]
    assert not vazios, f"leads com telefone real entraram em 'sem': {vazios[:5]}"


def test_telefone_cobre_null_vazio_e_so_espacos():
    client, d = _setup()
    sem, _ = _varre(client, limit=40, com_telefone="false")
    por_id = {l["id"]: l for l in d["leads"]}
    achados = {repr(por_id[i]["whatsapp"]) for i in sem}
    for esperado in ("None", "''", "'   '"):
        assert esperado in achados, (
            f"'sem telefone' nao pegou {esperado}; achou {sorted(achados)[:5]}")


def test_troca_de_filtro_com_cursor_antigo_nao_vaza_linha():
    """Cursor do conjunto A aplicado ao conjunto B nao pode devolver lead fora de B."""
    client, d = _setup()
    a = _get(client, limit=10)
    b = _get(client, limit=10, destino="Santiago", cursor=a["next_cursor"])
    permitido = _oraculo(d["leads"], d["resp_id"], destino="Santiago")
    assert all(x["id"] in permitido for x in b["leads"]), (
        "o cursor nao pode escapar do filtro — o filtro entra na query junto")


# ─────────────────────────────────────────────────────────────────────────
# 3. Campo personalizado (/segment) — sem materializar
# ─────────────────────────────────────────────────────────────────────────

def _seg(client, **params):
    qs = urlencode({k: v for k, v in params.items() if v is not None})
    r = client.get(f"/api/leads/segment?{qs}")
    assert r.status_code == 200, f"{qs}: {r.status_code} {r.text[:200]}"
    return r.json()


def _oraculo_campo(leads, chave, valor):
    ch = (chave or "").strip().lower()
    vl = (valor or "").strip().lower()
    out = set()
    for l in leads:
        cp = l["cp"] or {}
        k = next((k for k in cp if k.strip().lower() == ch), None)
        if k is not None and (not vl or vl in str(cp[k]).lower()):
            out.add(l["id"])
    return out


def test_campo_personalizado_bate_com_o_algoritmo_antigo():
    client, d = _setup()
    for chave, valor in (("origem", "instagram"), ("origem", None),
                         (" ORIGEM ", "GOOGLE"), ("origem", "zzz"),
                         ("nao_existe", None)):
        esperado = _oraculo_campo(d["leads"], chave, valor)
        b = _seg(client, limit=500, campo_chave=chave, campo_valor=valor)
        obtido = {l["id"] for l in b["leads"]}
        assert obtido == esperado and b["total"] == len(esperado), (
            f"campo {chave!r}/{valor!r}: obtido {len(obtido)} esperado {len(esperado)}")


def test_campo_personalizado_nao_materializa_a_base():
    client, d = _setup()
    with _Contador() as c:
        r = client.get("/api/leads/segment?limit=5&campo_chave=origem"
                       "&campo_valor=instagram")
        assert r.status_code == 200
    assert c.leads <= 10, (
        f"{c.leads} objetos Lead hidratados para devolver 5: voltou o filtro "
        f"em Python")


def test_segment_ordena_com_desempate():
    client, d = _setup()
    b = _seg(client, limit=500)
    por_id = {l["id"]: l for l in d["leads"]}
    chaves = [(por_id[l["id"]]["created_at"], l["id"]) for l in b["leads"]]
    assert chaves == sorted(chaves, reverse=True), "/segment sem desempate por id"


# ─────────────────────────────────────────────────────────────────────────
# 4. Guards de performance
# ─────────────────────────────────────────────────────────────────────────

class _Contador:
    def __enter__(self):
        from app.database import engine
        from app.models.lead import Lead
        self.q = self.leads = self.counts = 0
        self._e, self._L = engine, Lead
        event.listen(engine, "before_cursor_execute", self._q)
        event.listen(Lead, "load", self._l)
        return self

    def __exit__(self, *a):
        event.remove(self._e, "before_cursor_execute", self._q)
        event.remove(self._L, "load", self._l)

    def _q(self, conn, cur, stmt, params, ctx, many):
        self.q += 1
        if stmt.lstrip().lower().startswith("select count("):
            self.counts += 1

    def _l(self, *a):
        self.leads += 1


# ─── include_total: COUNT so na primeira pagina ──────────────────────────

def test_count_roda_na_primeira_pagina_e_nao_no_segundo_lote():
    """
    O nucleo do gate 3. Medido com 19.000 leads, o COUNT e 46% da requisicao
    com filtro de destino, 58% na busca textual e 82% no campo personalizado.
    Ele nao pode se repetir a cada "Carregar mais": o total do conjunto e o
    mesmo.
    """
    client, d = _setup()
    with _Contador() as c1:
        b1 = _get(client, limit=20)
    assert c1.counts == 1, f"a 1a pagina precisa contar uma vez: {c1.counts}"
    assert isinstance(b1["total"], int)

    with _Contador() as c2:
        b2 = _get(client, limit=20, cursor=b1["next_cursor"], include_total="false")
    assert c2.counts == 0, (
        f"o 2o lote executou {c2.counts} COUNT: include_total=false tem que pular")
    assert b2["total"] is None
    assert len(b2["leads"]) == 20 and b2["has_more"] is True and b2["next_cursor"]


def test_include_total_omitido_mantem_comportamento_legado():
    """n8n, pipeline.html e ai_tools nao enviam o parametro."""
    client, d = _setup()
    with _Contador() as c:
        b = _get(client, limit=10)
    assert c.counts == 1, "sem o parametro o COUNT continua acontecendo"
    assert isinstance(b["total"], int) and b["total"] == N, (
        f"total tem que continuar inteiro: {b['total']!r}")


def test_include_total_false_preserva_leads_cursor_e_has_more():
    client, d = _setup()
    com = _get(client, limit=30)
    sem = _get(client, limit=30, include_total="false")
    assert sem["total"] is None and isinstance(com["total"], int)
    assert [l["id"] for l in sem["leads"]] == [l["id"] for l in com["leads"]], (
        "pular o COUNT nao pode mudar as linhas")
    assert sem["has_more"] == com["has_more"] is True
    assert sem["next_cursor"] == com["next_cursor"], "o cursor tem que ser o mesmo"


def test_has_more_vem_do_limit_mais_um_e_nao_do_count():
    """Ultima pagina sem COUNT ainda precisa saber que acabou."""
    client, d = _setup()
    ordem, _ = _varre(client, limit=50)
    penultimo = _get(client, limit=100)
    b = _get(client, limit=100, cursor=penultimo["next_cursor"],
             include_total="false")
    assert b["total"] is None
    assert b["has_more"] is False and b["next_cursor"] is None, (
        "o fim da lista tem que ser detectado sem COUNT")
    assert len(b["leads"]) == N - 100


def test_varredura_com_include_total_false_nao_perde_nem_duplica():
    client, d = _setup()
    ordem, cursor = [], None
    while True:
        b = _get(client, limit=11, cursor=cursor,
                 include_total=("true" if cursor is None else "false"))
        ordem += [l["id"] for l in b["leads"]]
        if not b["has_more"]:
            break
        cursor = b["next_cursor"]
    assert len(ordem) == N == len(set(ordem)), (
        f"varredura sem COUNT: {len(ordem)} linhas, {len(set(ordem))} unicas")


def test_troca_de_filtro_conta_de_novo():
    client, d = _setup()
    a = _get(client, limit=20, destino="Atacama")
    _get(client, limit=20, cursor=a["next_cursor"], destino="Atacama",
         include_total="false")
    with _Contador() as c:
        b = _get(client, limit=20, destino="Uyuni")     # filtro novo, sem cursor
    assert c.counts == 1, "conjunto novo precisa de total novo"
    assert isinstance(b["total"], int) and b["total"] != a["total"], (
        f"os dois filtros tem que ter totais diferentes: {a['total']} e {b['total']}")


def test_limit_50_nao_hidrata_a_base_inteira():
    client, d = _setup()
    for extra in ({}, {"search": "Lead"}, {"destino": "Atacama"},
                  {"com_telefone": "true"}):
        with _Contador() as c:
            _get(client, limit=50, **extra)
        assert c.leads <= 51, (
            f"{extra}: {c.leads} objetos hidratados para limit=50")


def test_query_count_constante_com_o_volume():
    """50, 500 e 5000 leads: mesma pagina, mesmo numero de queries."""
    client, d = _setup()
    from app.database import SessionLocal
    from app.models.lead import Lead
    from datetime import date, datetime, timedelta, timezone
    medidas = {}
    db = SessionLocal()
    try:
        base = datetime(2025, 1, 1, tzinfo=timezone.utc)
        atual = db.query(Lead).count()
        for alvo in (50, 500, 5000):
            faltam = alvo - atual
            if faltam > 0:
                db.bulk_insert_mappings(Lead, [{
                    "nome": f"bulk {i}", "whatsapp": f"+5599{i:09d}",
                    "destinos": ["Atacama"], "campos_personalizados": {"origem": "X"},
                    "status_venda": "venda", "is_active": True,
                    "created_at": base + timedelta(seconds=i // 10),
                } for i in range(faltam)])
                db.commit()
                atual = alvo
            with _Contador() as c:
                _get(client, limit=50)
            medidas[alvo] = (c.q, c.leads)
    finally:
        db.query(Lead).filter(Lead.nome.like("bulk %")).delete(
            synchronize_session=False)
        db.commit()
        db.close()

    qs = {v[0] for v in medidas.values()}
    assert len(qs) == 1, f"query count variou com o volume: {medidas}"
    for alvo, (q, objs) in medidas.items():
        assert objs <= 51, f"{alvo} leads: {objs} objetos hidratados"


def test_sem_ramo_de_filtragem_python_em_leads():
    for fn in ("list_leads", "segment_leads"):
        corpo = FONTE.split(f"def {fn}", 1)[1].split("\n\n\n", 1)[0]
        # `Lead.campos_personalizados` (atributo de CLASSE) e expressao SQL e
        # pode ficar; `lead.campos_personalizados` (INSTANCIA) so existe se
        # alguem carregou o objeto para filtrar em Python — esse era o bug.
        assert "lead.campos_personalizados" not in corpo, (
            f"{fn} voltou a resolver campo personalizado em Python")
        assert "for lead in" not in corpo, f"{fn} voltou a iterar leads em Python"
        # todo .all() precisa vir depois de um .limit(): materializar o
        # conjunto inteiro para fatiar em Python foi exatamente o bug
        for linha in corpo.splitlines():
            if ".all()" in linha:
                trecho = corpo[:corpo.index(linha) + len(linha)]
                assert ".limit(" in trecho.rsplit("=", 1)[-1], (
                    f"{fn} materializa sem limit: {linha.strip()}")


# ─────────────────────────────────────────────────────────────────────────
# 5. Contrato da API
# ─────────────────────────────────────────────────────────────────────────

def test_contrato_antigo_preservado():
    client, d = _setup()
    b = _get(client, limit=5, skip=10)
    for campo in ("total", "skip", "limit", "leads"):
        assert campo in b, f"campo {campo} sumiu do contrato"
    assert b["skip"] == 10 and b["limit"] == 5
    assert len(b["leads"]) == 5
    l = b["leads"][0]
    for campo in ("id", "nome", "email", "whatsapp", "destinos", "tags",
                  "funis", "responsavel_nome"):
        assert campo in l, f"campo {campo} sumiu do lead (n8n/pipeline consomem)"


def test_skip_continua_funcionando_para_quem_ja_usa():
    """n8n usa ?limit=500 e a ferramenta de IA usa skip. Nao pode quebrar."""
    client, d = _setup()
    todos, _ = _varre(client, limit=50)
    p1 = _get(client, limit=30, skip=0)
    p2 = _get(client, limit=30, skip=30)
    assert [l["id"] for l in p1["leads"]] == todos[:30]
    assert [l["id"] for l in p2["leads"]] == todos[30:60], (
        "skip/limit tem que continuar coerente com a ordem nova")


def test_campos_novos_sao_aditivos():
    client, d = _setup()
    b = _get(client, limit=5)
    assert "next_cursor" in b and "has_more" in b
    seg = _seg(client, limit=5)
    assert seg["has_more"] is True, "/segment tambem informa has_more"


def test_pipeline_dropdown_continua_funcionando():
    """templates/pipeline.html consome /api/leads com exclude_funnel_id."""
    client, d = _setup()
    r = client.get(f"/api/leads?limit=20&exclude_funnel_id={d['funnel']}")
    assert r.status_code == 200, r.text
    b = r.json()
    assert "leads" in b
    assert d["dois_funis"] not in {l["id"] for l in b["leads"]}, (
        "exclude_funnel_id parou de excluir")


# ─────────────────────────────────────────────────────────────────────────
# 6. Deep-link ?open=<id>
# ─────────────────────────────────────────────────────────────────────────

def test_deep_link_busca_direta_por_id():
    client, d = _setup()
    ordem, _ = _varre(client, limit=50)
    primeiro, fora = ordem[0], ordem[-1]
    for lid in (primeiro, fora):
        r = client.get(f"/api/leads/{lid}")
        assert r.status_code == 200, f"lead {lid}: {r.status_code}"
        assert r.json()["id"] == lid
    assert fora not in set(ordem[:50]), "o 2o caso precisa estar fora da 1a pagina"


def test_deep_link_inexistente_e_404_controlado():
    client, d = _setup()
    r = client.get("/api/leads/99999999")
    assert r.status_code == 404, f"esperado 404, veio {r.status_code}"


def test_frontend_abre_deep_link_sem_varrer_a_lista():
    js = _js_da_pagina()
    bloco = js.split("const openId", 1)[1].split("});", 1)[0]
    assert "editLead(parseInt(openId))" in bloco, "deep-link precisa continuar abrindo"
    edit = js.split("async function editLead", 1)[1].split("\n    }", 1)[0]
    assert "/api/leads/${id}" in edit, "editLead busca por id, direto"
    assert "loadMore" not in bloco and "cursor" not in bloco, (
        "o deep-link nao pode paginar ate achar o lead")


# ─────────────────────────────────────────────────────────────────────────
# 7. PostgreSQL — SQL compilado
# ─────────────────────────────────────────────────────────────────────────

def _sql_pg(construir):
    from sqlalchemy.dialects.postgresql import dialect as pg
    from app.database import SessionLocal
    from app.routers import leads as L
    from app import query_filters as QF
    db = SessionLocal()
    try:
        orig, origQF = L.IS_SQLITE, QF.IS_SQLITE
        L.IS_SQLITE = QF.IS_SQLITE = False
        try:
            q = construir(db, L, QF)
            return str(q.statement.compile(
                dialect=pg(), compile_kwargs={"literal_binds": True}))
        finally:
            L.IS_SQLITE, QF.IS_SQLITE = orig, origQF
    finally:
        db.close()


def test_postgres_keyset_tem_desempate_e_ordem():
    from app.models.lead import Lead
    sql = _sql_pg(lambda db, L, QF: db.query(Lead.id)
                  .filter(L._keyset_filtro(_cursor_de_exemplo()))
                  .order_by(Lead.created_at.desc(), Lead.id.desc()))
    achatado = " ".join(sql.split())
    assert "ORDER BY leads.created_at DESC, leads.id DESC" in achatado, achatado
    # row-value: (created_at, id) < (ts, id). Equivale a
    # `created_at < ts OR (created_at = ts AND id < lid)`, mas vira SEEK no
    # indice em vez de SCAN — medido em 19k leads, 0,2 ms contra 4,1 ms.
    assert "(leads.created_at, leads.id) <" in achatado, (
        f"keyset precisa comparar o PAR (created_at, id): {achatado}")
    assert "leads.id" in achatado, "sem id no cursor a paginacao perde linhas"


def test_postgres_telefone_usa_trim_e_not_null():
    from app.models.lead import Lead
    sql = " ".join(_sql_pg(lambda db, L, QF: db.query(Lead.id)
                           .filter(L._tem_telefone())).split())
    assert "IS NOT NULL" in sql and "trim(" in sql.lower(), sql
    assert "leads.whatsapp" in sql


def test_postgres_campo_personalizado_e_jsonb_exists():
    from app.models.lead import Lead
    sql = _sql_pg(lambda db, L, QF: db.query(Lead.id).filter(
        QF.campo_personalizado_match(Lead.campos_personalizados, "origem", "insta")))
    assert "EXISTS" in sql and "jsonb_each_text" in sql, sql[:300]
    assert "jsonb_typeof" in sql, "guarda de tipo tem que sobreviver"
    assert "json_each(" not in sql, "forma SQLite vazou para o PostgreSQL"


def test_postgres_destino_e_jsonb_contains():
    from app.models.lead import Lead
    sql = _sql_pg(lambda db, L, QF: db.query(Lead.id).filter(
        L._json_list_contains(Lead.destinos, "Atacama")))
    assert "@>" in sql and "JSONB" in sql.upper(), sql[:300]


def test_postgres_busca_usa_ilike_nos_tres_campos():
    from app.models.lead import Lead
    from sqlalchemy import or_
    sql = " ".join(_sql_pg(lambda db, L, QF: db.query(Lead.id).filter(or_(
        Lead.nome.ilike("%x%"), Lead.email.ilike("%x%"),
        Lead.whatsapp.ilike("%x%")))).split())
    assert sql.count("ILIKE") == 3, f"busca precisa cobrir nome/email/whatsapp: {sql}"


def _cursor_de_exemplo():
    from app.routers.leads import _cursor_encode

    class _F:
        from datetime import datetime, timezone
        created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
        id = 42
    return _cursor_encode(_F())


# ─────────────────────────────────────────────────────────────────────────
# 8. JavaScript executado no node
# ─────────────────────────────────────────────────────────────────────────

def _js_da_pagina():
    blocos = re.findall(r"<script>(.*?)</script>", HTML, re.S)
    return max(blocos, key=len)


def _fn(nome):
    """Codigo real da funcao, preservando o `async` quando existir."""
    js = _js_da_pagina()
    m = re.search(r"(async\s+)?function " + re.escape(nome) + r"\(", js)
    assert m, f"{nome} sumiu de leads.html"
    return js[m.start():].split("\n    }", 1)[0] + "\n    }"


_HARNESS = """
'use strict';
let valores = %s;
let abortados = 0, pedidos = [];
const els = {};
function el(id) {
    if (!els[id]) els[id] = { style: {}, textContent: '', innerHTML: '',
                              disabled: false, rows: [] };
    els[id].value = valores[id] || '';   // sempre atual: o teste troca `valores`
    return els[id];
}
const document = { getElementById: el };
class AbortController {
    constructor() { this.signal = { aborted: false }; }
    abort() { this.signal.aborted = true; abortados++; }
}
const respostas = %s;
const Auth = { apiRequest: async (url, opts) => {
    const i = pedidos.length;
    pedidos.push(url);
    await new Promise(r => setTimeout(r, %d));
    if (opts && opts.signal && opts.signal.aborted) {
        const e = new Error('abort'); e.name = 'AbortError'; throw e;
    }
    const corpo = respostas[Math.min(i, respostas.length - 1)];
    return { ok: true, status: 200, json: async () => corpo };
}};
const PAGE_SIZE = 50;
let nextCursor = null, hasMore = false, carregando = false;
let leadsNaTela = [], reqAtual = null, debounceTimer = null;
let totalConhecido = 0, totalRenderizado = null;
function renderFooter() {}
function renderTable(total) { totalRenderizado = total; }
function mostrarErro() {}

%s
%s
%s
%s

(async () => {
%s
})();
"""


def _roda_js(corpo, valores=None, resposta=None, atraso=0):
    node = shutil.which("node")
    assert node, "node e necessario para exercitar o JS da pagina"
    if resposta is None:
        resposta = {"leads": [{"id": 1}], "total": 1,
                    "next_cursor": "C2", "has_more": True}
    if isinstance(resposta, dict):        # uma resposta = vale para todas
        resposta = [resposta]
    script = _HARNESS % (
        json.dumps(valores or {}), json.dumps(resposta), atraso,
        _fn("filtrosAtuais"), _fn("reloadLeads"), _fn("loadLeads"), _fn("loadMore"),
        corpo)
    p = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert p.returncode == 0, f"node falhou: {p.stderr.strip()[:400]}"
    return json.loads(p.stdout)


def test_js_filtros_viram_query_string():
    r = _roda_js(
        "console.log(JSON.stringify({qs: filtrosAtuais().toString()}));",
        valores={"filterSearch": " joao ", "filterDestino": "Atacama",
                 "filterTelefone": "false", "filterResponsavel": "0"})
    qs = r["qs"]
    assert "limit=50" in qs, qs
    assert "search=joao" in qs, f"busca precisa ir trimada ao servidor: {qs}"
    assert "destino=Atacama" in qs
    assert "com_telefone=false" in qs, f"filtro de telefone nao foi: {qs}"
    assert "responsavel_id=0" in qs, f"Agente IA (0) nao pode virar vazio: {qs}"


def test_js_troca_de_filtro_zera_o_cursor():
    """Mutation D: se reloadLeads nao zerar, o 2o conjunto herda o cursor."""
    r = _roda_js("""
        await loadLeads();                       // 1o lote, guarda cursor
        const depoisDoPrimeiro = nextCursor;
        valores.filterDestino = 'Uyuni';         // usuario troca o filtro
        await reloadLeads();
        console.log(JSON.stringify({
            depoisDoPrimeiro,
            urlDoReload: pedidos[pedidos.length - 1],
            acumulados: leadsNaTela.length,
        }));
    """)
    assert r["depoisDoPrimeiro"] == "C2", "1o lote precisa ter guardado o cursor"
    assert "cursor=" not in r["urlDoReload"], (
        f"reload apos troca de filtro mandou cursor do conjunto antigo: "
        f"{r['urlDoReload']}")
    assert "destino=Uyuni" in r["urlDoReload"], "o filtro novo tem que ir junto"
    assert r["acumulados"] == 1, "a lista tem que ter sido zerada antes do reload"


def test_js_carregar_mais_manda_o_cursor_e_acumula():
    r = _roda_js("""
        await loadLeads();
        await loadLeads();
        console.log(JSON.stringify({
            urls: pedidos, acumulados: leadsNaTela.length,
        }));
    """)
    assert "cursor=" not in r["urls"][0], "1o request nao tem cursor"
    assert "cursor=C2" in r["urls"][1], f"2o lote sem cursor: {r['urls'][1]}"
    assert r["acumulados"] == 2, "o 2o lote tem que somar, nao substituir"


def test_js_clique_duplo_em_carregar_mais_nao_duplica_pagina():
    r = _roda_js("""
        await loadLeads();
        loadMore(); loadMore(); loadMore();       // 3 cliques seguidos
        await new Promise(r => setTimeout(r, 60));
        console.log(JSON.stringify({ requests: pedidos.length }));
    """, atraso=20)
    assert r["requests"] == 2, (
        f"3 cliques geraram {r['requests']} requests: falta a guarda de carregando")


def test_js_resposta_antiga_nao_sobrescreve_a_nova():
    """Mutation F: sem AbortController o request lento antigo ganha do novo."""
    r = _roda_js("""
        const p1 = reloadLeads();                 // digitou 'j'
        const p2 = reloadLeads();                 // digitou 'jo' logo depois
        await Promise.all([p1, p2]);
        console.log(JSON.stringify({ abortados, acumulados: leadsNaTela.length }));
    """, atraso=15)
    assert r["abortados"] >= 1, "o request anterior tem que ser abortado"
    assert r["acumulados"] == 1, (
        f"duas respostas entraram na lista ({r['acumulados']}): a antiga "
        f"sobrescreveu/duplicou a nova")


def test_js_primeira_pagina_pede_total_e_carregar_mais_nao():
    r = _roda_js("""
        await loadLeads();                      // 1a pagina
        await loadLeads();                      // Carregar mais
        console.log(JSON.stringify({ urls: pedidos }));
    """)
    assert "include_total=false" not in r["urls"][0], (
        f"a 1a pagina precisa do total: {r['urls'][0]}")
    assert "include_total=false" in r["urls"][1], (
        f"o 2o lote tem que pular o COUNT: {r['urls'][1]}")
    assert "cursor=C2" in r["urls"][1]


def test_js_total_null_nao_apaga_o_total_da_ui():
    """
    O servidor devolve total=null no 2o lote. A UI ja sabe o numero e nao pode
    trocar "19000 leads encontrados" por "null leads encontrados".
    """
    r = _roda_js("""
        await loadLeads();
        const depoisDaPrimeira = totalRenderizado;
        await loadLeads();
        console.log(JSON.stringify({
            depoisDaPrimeira, depoisDoSegundo: totalRenderizado, totalConhecido,
        }));
    """, resposta=[
        {"leads": [{"id": 1}], "total": 19000, "next_cursor": "C2", "has_more": True},
        {"leads": [{"id": 2}], "total": None, "next_cursor": "C3", "has_more": True},
    ])
    assert r["depoisDaPrimeira"] == 19000
    assert r["depoisDoSegundo"] == 19000, (
        f"o total virou {r['depoisDoSegundo']!r} depois do lote sem COUNT")
    assert r["totalConhecido"] == 19000


def test_js_troca_de_filtro_volta_a_pedir_o_total():
    r = _roda_js("""
        await loadLeads();
        await loadLeads();                      // Carregar mais (sem total)
        valores.filterDestino = 'Uyuni';
        await reloadLeads();                    // conjunto novo
        console.log(JSON.stringify({ ultima: pedidos[pedidos.length - 1] }));
    """)
    assert "include_total=false" not in r["ultima"], (
        f"conjunto novo precisa recalcular o total: {r['ultima']}")
    assert "cursor=" not in r["ultima"], "e sem cursor do conjunto antigo"
    assert "destino=Uyuni" in r["ultima"]


def test_js_has_more_nao_depende_do_total():
    r = _roda_js("""
        await loadLeads();
        console.log(JSON.stringify({ hasMore, totalRenderizado }));
    """, resposta={"leads": [{"id": 1}], "total": None,
                   "next_cursor": "C9", "has_more": True})
    assert r["hasMore"] is True, "has_more vem do limit+1, nao do COUNT"


def test_js_paginacao_numerica_saiu():
    js = _js_da_pagina()
    assert "goToPage" not in js, "paginacao numerica tinha que sair"
    assert "Math.ceil(data.total / PAGE_SIZE)" not in js, "contagem de paginas ficou"
    assert "loadMoreBtn" in HTML and "Carregar mais" in HTML
    assert "PAGE_SIZE = 50" in js, "o lote inicial e de 50"


def test_js_botao_so_aparece_com_has_more():
    rodape = _fn("renderFooter")
    assert "hasMore ?" in rodape and "display" in rodape, rodape
    assert "btn.disabled = carregando" in rodape, "botao desabilita durante o request"
    assert "fim da lista" in rodape, "estado de fim de lista"


def test_js_erro_nao_vira_lista_vazia():
    erro = _fn("mostrarErro")
    assert "Erro ao carregar" in erro
    assert "Tentar de novo" in erro, "erro precisa permitir nova tentativa"
    assert "leadsNaTela.length" in erro, (
        "erro no 'Carregar mais' nao pode apagar o que ja esta na tela")
    carrega = _fn("loadLeads")
    assert "if (!resp.ok) { mostrarErro(resp.status); return; }" in carrega, (
        "resposta 4xx/5xx tem que virar erro, nao 'nenhum lead encontrado'")


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
            print(f"FAIL  {fn.__name__}: {str(exc)[:300]}")
        except Exception as exc:
            falhas += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {str(exc)[:300]}")
    print(f"\n{len(ALL_TESTS) - falhas}/{len(ALL_TESTS)} testes OK")
    sys.exit(1 if falhas else 0)
