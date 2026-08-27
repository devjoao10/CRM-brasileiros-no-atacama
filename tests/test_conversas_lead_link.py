# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-WF2 — vinculo conversa <-> lead do CRM sem fabricar duplicado.

O DEFEITO
---------
`lookup_lead_by_whatsapp` decide identidade por igualdade EXATA dos digitos,
mas o conjunto de candidatos vinha de `WHERE whatsapp LIKE '%<10 digitos>%'`
sobre a coluna CRUA. O lead que o formulario do site grava — `+55 11 98765-4322`
— nunca entrava nessa lista: nem o casamento exato nem o guard de ambiguidade
chegavam a ve-lo. Resultado: o lookup devolvia None, `auto_create_lead_in_crm`
criava o MESMO cliente de novo, a conversa ficava presa ao duplicado e o lead
real (com e-mail, destinos e responsavel) ficava orfao.

O QUE ESTE ARQUIVO PROVA
------------------------
1. ESTATICO (roda sempre, sem banco): o pre-filtro normaliza OS DOIS LADOS no
   SQL, e `auto_link_conversation` NAO cria lead quando o lookup nao consegue
   identificar um unico lead.
2. COMPORTAMENTAL (so com PostgreSQL): lead gravado com formatacao e ACHADO e
   nenhum duplicado nasce; corpus de formatos; e o par formatado/nao-formatado
   do mesmo cliente e detectado como AMBIGUO — sem vinculo e sem terceiro lead.

Sem PostgreSQL a parte 2 PULA com mensagem explicita (o SQL e SO-PostgreSQL:
`regexp_replace`, `NOW()`, `::jsonb`, `RETURNING` — ver
tests/test_postgres_dialect_divergence.py). Pular e dito em voz alta; nunca
passa em silencio fingindo que verificou.

Rodar:
    python tests/test_conversas_lead_link.py                       # so estatico
    DATABASE_URL=postgresql+psycopg2://user:senha@host:porta/base \
        python tests/test_conversas_lead_link.py                   # completo
"""
import os
import pathlib
import sys
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
CRM_SERVICE = CONVERSAS_DIR / "app" / "services" / "crm.py"

# Schema descartavel: as tabelas do CRM sao criadas e DERRUBADAS aqui dentro,
# entao apontar DATABASE_URL para uma base povoada nao toca em nada existente.
SCHEMA = "wf2_lead_link_test"

NUMERO_META = "5511987654322"   # o que a Meta entrega no webhook
FORMATADO = "+55 11 98765-4322"  # o que o formulario do site grava

falhas = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        falhas.append(msg)


# ══════════════════════════════════════════════════════════════════════════
# 1. Estatico — vale em qualquer banco, roda sempre
# ══════════════════════════════════════════════════════════════════════════
print("1) crm.py — forma do pre-filtro e do guard de criacao")

fonte = CRM_SERVICE.read_text(encoding="utf-8")

check("regexp_replace(whatsapp, '[^0-9]', '', 'g') = :digitos" in fonte,
      "o pre-filtro normaliza OS DOIS LADOS no SQL (regexp_replace na coluna "
      "vs digitos do numero) — sem isso o lead gravado com '+', espaco ou '-' "
      "nunca entra na lista de candidatos")

check("WHERE whatsapp LIKE :pattern" not in fonte,
      "o pre-filtro LIKE sobre a coluna CRUA sumiu — era ele que escondia o "
      "lead formatado do casamento exato E do guard de ambiguidade")

check("return None, True" in fonte and "bloquear_criacao" in fonte,
      "lookup sinaliza 'nao da para afirmar que o numero e novo' e "
      "auto_link_conversation le esse sinal")

trecho = fonte[fonte.index("async def auto_link_conversation"):]
guard = trecho.find("if bloquear_criacao:")
criacao = trecho.find("auto_create_lead_in_crm(")
check(0 <= guard < criacao,
      "o guard vem ANTES de auto_create_lead_in_crm: com o pre-filtro corrigido "
      "a ambiguidade finalmente e detectada, e criar assim mesmo so produziria "
      "uma TERCEIRA copia do mesmo cliente")


# ══════════════════════════════════════════════════════════════════════════
# 2. Comportamental — exige PostgreSQL de verdade
# ══════════════════════════════════════════════════════════════════════════
url_bruta = os.environ.get("DATABASE_URL", "")
e_postgres = url_bruta.startswith(("postgresql", "postgres://"))

if not e_postgres:
    print("\n2) comportamento contra o banco — PULADO")
    print(f"     DATABASE_URL={url_bruta or '(vazia)'} nao e PostgreSQL.")
    print("     O SQL deste modulo e SO-PostgreSQL (regexp_replace, NOW(), "
          "::jsonb, RETURNING);")
    print("     em SQLite ele levanta e o resultado seria um verde falso.")
    print("     Rode de novo com DATABASE_URL=postgresql+psycopg2://... para "
          "provar o comportamento.")
    print(f"\n{'FALHOU' if falhas else 'OK'} — {len(falhas)} falha(s)")
    sys.exit(1 if falhas else 0)

print("\n2) comportamento contra PostgreSQL")

import psycopg2  # noqa: E402
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT  # noqa: E402


def dsn_psycopg2(url):
    """URL do SQLAlchemy -> DSN do psycopg2 (tira o '+psycopg2')."""
    return url.replace("postgresql+psycopg2://", "postgresql://", 1)


bruto = psycopg2.connect(dsn_psycopg2(url_bruta))
bruto.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
with bruto.cursor() as cur:
    # Sem lock_timeout, um DROP SCHEMA CASCADE que esbarre numa transacao aberta
    # (teste que falhou no meio) espera para SEMPRE — o teste travaria em vez de
    # reprovar. 10s e folga de sobra para um schema descartavel.
    cur.execute("SET lock_timeout = '10s'")
    cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    cur.execute(f"CREATE SCHEMA {SCHEMA}")

sep = "&" if "?" in url_bruta else "?"
opcao = urllib.parse.quote(f"-csearch_path={SCHEMA}")
os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": f"{url_bruta}{sep}options={opcao}",
    "SECRET_KEY": "wf2-lead-link",
    "CONVERSAS_SEED_DEV_DATA": "false",
    "META_APP_SECRET": "",
    "N8N_AGENT_ENABLED": "false",
})
sys.path.insert(0, str(CONVERSAS_DIR))

import asyncio  # noqa: E402
from sqlalchemy import text  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
import app.models.conversation  # noqa: E402,F401
import app.models.note  # noqa: E402,F401
import app.models.media_asset  # noqa: E402,F401
import app.models.quick_reply  # noqa: E402,F401
import app.models.tag  # noqa: E402,F401
import app.models.template  # noqa: E402,F401
import app.services.crm as crm  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402

# Tabelas do CRM que crm.py enxerga na base compartilhada (recorte minimo).
DDL = [
    "CREATE TABLE users (id SERIAL PRIMARY KEY, nome VARCHAR(200) NOT NULL,"
    " email VARCHAR(255), is_active BOOLEAN NOT NULL DEFAULT true)",
    "CREATE TABLE leads (id SERIAL PRIMARY KEY, nome VARCHAR(200) NOT NULL,"
    " email VARCHAR(255), whatsapp VARCHAR(30),"
    " campos_personalizados JSON NOT NULL DEFAULT '{}',"
    " status_venda VARCHAR(30) NOT NULL DEFAULT 'em_negociacao',"
    " is_active BOOLEAN NOT NULL DEFAULT true,"
    " responsavel_id INTEGER REFERENCES users(id) ON DELETE SET NULL,"
    " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
    " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
    "CREATE INDEX ix_leads_whatsapp ON leads (whatsapp)",
    "CREATE TABLE tags (id SERIAL PRIMARY KEY, nome VARCHAR(100) UNIQUE NOT NULL,"
    " cor VARCHAR(20), created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
    "CREATE TABLE lead_tags (lead_id INTEGER REFERENCES leads(id) ON DELETE CASCADE,"
    " tag_id INTEGER REFERENCES tags(id) ON DELETE CASCADE, PRIMARY KEY (lead_id, tag_id))",
    "CREATE TABLE funnels (id SERIAL PRIMARY KEY, nome VARCHAR(200) UNIQUE NOT NULL,"
    " etapas JSON NOT NULL DEFAULT '[]', is_active BOOLEAN NOT NULL DEFAULT true)",
    "CREATE TABLE funnel_entries (id SERIAL PRIMARY KEY,"
    " lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,"
    " funnel_id INTEGER NOT NULL REFERENCES funnels(id) ON DELETE CASCADE,"
    " etapa_id VARCHAR(80), posicao INTEGER DEFAULT 0,"
    " created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
    " updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
    "CREATE TABLE lead_history (id SERIAL PRIMARY KEY,"
    " lead_id INTEGER NOT NULL REFERENCES leads(id) ON DELETE CASCADE,"
    " evento VARCHAR(60) NOT NULL, descricao TEXT,"
    " dados JSON NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())",
]


def montar():
    with engine.begin() as con:
        for stmt in DDL:
            con.execute(text(stmt))
        con.execute(text(
            "INSERT INTO users (id, nome, email) VALUES (7, 'Ana Vendas', 'ana@x.com')"))
        con.execute(text(
            "INSERT INTO funnels (id, nome, etapas, is_active) VALUES (1, 'Vendas: Principal',"
            " '[{\"id\": \"sem_contato\", \"nome\": \"Sem Contato\"}]', true)"))
        con.execute(text("SELECT setval('leads_id_seq', 1000)"))
    Base.metadata.create_all(engine)


def semear(*valores):
    """Recomeca o recorte de leads sob teste. valores = (id, nome, whatsapp)."""
    with engine.begin() as con:
        con.execute(text("DELETE FROM leads"))
        for lead_id, nome, whatsapp in valores:
            con.execute(
                text("INSERT INTO leads (id, nome, whatsapp, email, responsavel_id)"
                     " VALUES (:i, :n, :w, :e, :r)"),
                {"i": lead_id, "n": nome, "w": whatsapp,
                 "e": "maria@site.com" if lead_id == 200 else None,
                 "r": 7 if lead_id == 200 else None},
            )


def ids_dos_leads(db):
    db.rollback()
    return [r.id for r in db.execute(text("SELECT id FROM leads ORDER BY id")).fetchall()]


def nova_conversa(db):
    db.execute(text("DELETE FROM conversations"))
    db.commit()
    conv = Conversation(whatsapp=NUMERO_META, nome="Maria", lead_id=0)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


db = SessionLocal()  # fechada no `finally` la embaixo, inclusive quando um check estoura


async def cenarios():
    # 2.1 lead do formulario, gravado COM formatacao
    semear((200, "Maria (site)", FORMATADO))
    lead, bloquear = await crm.lookup_lead_by_whatsapp(NUMERO_META, db)
    check(lead is not None and lead["id"] == 200,
          f"lead gravado como {FORMATADO!r} e ACHADO pelo numero {NUMERO_META} "
          f"(devolveu {lead and lead['id']})")
    check(bloquear is False, "casamento unico nao bloqueia nada")
    check(bool(lead) and lead["email"] == "maria@site.com" and lead["responsavel_nome"] == "Ana Vendas",
          "o lead achado traz e-mail e responsavel do CRM — e o registro REAL, "
          "nao uma casca criada pelo WhatsApp")

    conv = nova_conversa(db)
    ligado = await crm.auto_link_conversation(conv, db)
    check(ligado is True and conv.lead_id == 200,
          f"auto_link_conversation vincula a conversa ao lead 200 "
          f"(devolveu {ligado}, lead_id={conv.lead_id})")
    check(ids_dos_leads(db) == [200],
          f"NENHUM lead duplicado foi criado (leads={ids_dos_leads(db)})")

    # 2.2 corpus de formatos
    for fmt in ["+55 11 98765-4322", "5511987654322", "+5511987654322",
                "55 11 9 8765 4322"]:
        semear((200, "Maria", fmt))
        db.rollback()
        achado, _ = await crm.lookup_lead_by_whatsapp(NUMERO_META, db)
        check(bool(achado) and achado["id"] == 200,
              f"formato {fmt!r} casa com {NUMERO_META}")

    # LIMITE CONHECIDO, deliberado: numero gravado SEM DDI e outro conjunto de
    # digitos (11 vs 13) e esta funcao casa por identidade EXATA (F10). Quem for
    # tratar o eixo DDI muda AQUI de proposito, junto com o irmao do CRM
    # (app/routers/leads.py::get_lead_by_whatsapp, que ja aceita sufixo e
    # devolve 409 na ambiguidade). Nao e um PASS comemorando bug: e a fronteira
    # explicita para o check quebrar quando o contrato mudar.
    for fmt in ["(11) 98765-4322", "11987654322"]:
        semear((200, "Maria", fmt))
        db.rollback()
        achado, _ = await crm.lookup_lead_by_whatsapp(NUMERO_META, db)
        check(achado is None,
              f"LIMITE CONHECIDO: {fmt!r} (sem DDI) NAO casa com {NUMERO_META} — "
              f"eixo DDI e outro WP")

    # 2.3 ambiguidade: formatado + digits-only, o MESMO cliente
    semear((200, "Maria (site)", FORMATADO), (201, "Maria (wpp)", NUMERO_META))
    db.rollback()
    lead, bloquear = await crm.lookup_lead_by_whatsapp(NUMERO_META, db)
    check(lead is None,
          "dois leads com o mesmo numero normalizado: o lookup NAO elege "
          f"nenhum (devolveu {lead and lead['id']})")
    check(bloquear is True,
          "e sinaliza ambiguidade — antes o par formatado/nao-formatado era "
          "invisivel e o lookup devolvia o #201 com confianca")

    conv = nova_conversa(db)
    ligado = await crm.auto_link_conversation(conv, db)
    check(ligado is False and conv.lead_id == 0,
          f"a conversa fica SEM vinculo (devolveu {ligado}, lead_id={conv.lead_id})")
    check(ids_dos_leads(db) == [200, 201],
          f"e NENHUM terceiro lead nasce da ambiguidade (leads={ids_dos_leads(db)})")


try:
    montar()
    asyncio.run(cenarios())
finally:
    # A ORDEM importa: a sessao segura uma transacao aberta e o DROP SCHEMA
    # CASCADE fica na fila atras dela. Fechar primeiro, derrubar depois.
    db.close()
    engine.dispose()
    with bruto.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    bruto.close()

print(f"\n{'FALHOU' if falhas else 'OK'} — {len(falhas)} falha(s)")
sys.exit(1 if falhas else 0)
