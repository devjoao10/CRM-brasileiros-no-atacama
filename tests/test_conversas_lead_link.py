# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-WF2 + CONV-LEAD-FONE-01 — vinculo conversa <-> lead do CRM sem
fabricar duplicado.

OS DEFEITOS
-----------
1. (WF2) `lookup_lead_by_whatsapp` decidia identidade por igualdade EXATA dos
   digitos, mas o conjunto de candidatos vinha de `WHERE whatsapp LIKE
   '%<10 digitos>%'` sobre a coluna CRUA. O lead que o formulario do site
   grava — `+55 11 98765-4322` — nunca entrava nessa lista: nem o casamento
   exato nem o guard de ambiguidade chegavam a ve-lo. Resultado: o lookup
   devolvia None, `auto_create_lead_in_crm` criava o MESMO cliente de novo, a
   conversa ficava presa ao duplicado e o lead real (com e-mail, destinos e
   responsavel) ficava orfao.
2. (CONV-LEAD-FONE-01) Mesmo com o pre-filtro certo, a igualdade exata nao
   reconhece o MESMO celular em outra grafia (nono digito, DDI): o lead
   cadastrado a mao no CRM passava por desconhecido e a conversa ganhava um
   duplicado. O lookup tem agora DUAS camadas — igualdade exata e, so se
   nenhum lead for exato, "mesma linha em outra grafia" (regra unica de
   `telefone_identidade.py`) — e a segunda so roda para wa_id brasileiro E.164.

O QUE ESTE ARQUIVO PROVA
------------------------
Parte 1 — ESTATICO (roda sempre, sem banco): o pre-filtro normaliza OS DOIS
LADOS no SQL (camada 1 exata e camada 2 "mesma linha"); nenhum SQL do lookup
interpola valor (todo `text()` das duas funcoes recebe UM literal); o LIKE nunca
incide sobre a coluna crua; o LIMIT do SQL e o limite do fail-closed sao o mesmo
numero; e `auto_link_conversation` NAO cria lead quando o lookup nao consegue
identificar um unico lead.

Parte 2 — COMPORTAMENTAL (so com PostgreSQL):
  2.1-2.3   (WF2) lead gravado com formatacao e ACHADO e nenhum duplicado nasce;
            corpus de formatos; e o par formatado/nao-formatado do mesmo cliente
            e AMBIGUO — sem vinculo e sem terceiro lead.
  2.4-2.9   (CONV-LEAD-FONE-01) o MESMO celular em outra grafia — sem o 9o
            digito (como a Meta entrega fora dos DDDs 11-19, 21, 22, 24, 27 e
            28), sem DDI ou com `+` — e reconhecido como o lead que ja existe;
            DDD diferente, fixo contra celular, os 10 ultimos digitos e numero
            sem DDD NAO casam (F10); o lead EXATO tem precedencia sobre o "mesma
            linha"; ambiguidade na camada 2 e recusada; o sufixo so vale entre
            numeros sem leitura brasileira; e o relato do dono ponta a ponta
            (lead cadastrado pelo modal + wa_id sem o 9o digito) vincula a
            conversa a ESSE lead sem criar duplicata.
  2.10      a camada 2 so roda para wa_id brasileiro E.164 (55 + 12/13 digitos):
            o wa_id da Meta e sempre E.164, entao 10/11 digitos ali e estrangeiro.
  2.11      falha na camada 2 NAO vira "numero novo": (None, True), sem vinculo e
            sem lead criado.
  2.12      fail-closed no LIMIT: pre-filtro que enche o limite de candidatos e
            ambiguo — truncar nunca elege um lead.
  Os logs de `app.services.crm` sao capturados com um handler: os WARNINGs de
  ambiguidade e do limite saem mascarados (so os 4 ultimos digitos), o ERROR de
  falha de consulta nao carrega a mensagem da excecao (os parametros do SQL sao
  o telefone) e o run verde nao joga linhas de log no stderr.

Sem PostgreSQL a parte 2 PULA com mensagem explicita (o SQL e SO-PostgreSQL:
`regexp_replace`, `NOW()`, `::jsonb`, `RETURNING` — ver
tests/test_postgres_dialect_divergence.py). Pular e dito em voz alta; nunca
passa em silencio fingindo que verificou.

Rodar:
    python tests/test_conversas_lead_link.py                       # so estatico
    DATABASE_URL=postgresql+psycopg2://user:senha@host:porta/base \
        python tests/test_conversas_lead_link.py                   # completo
"""
import ast
import logging
import os
import pathlib
import re
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

# CONV-LEAD-FONE-01 — a camada 2 ("mesma linha em outra grafia") e SQL NOVO, e as
# garantias abaixo valem para TODO SQL do lookup, nao so para o da camada 1.
# Lidas do AST: so o SQL que de fato executa, sem o que a docstring cita como
# historia (o texto "whatsapp LIKE '%...%'" aparece la de proposito).
arvore = ast.parse(fonte)
chamadas_text = [
    n for n in ast.walk(arvore)
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "text"
]


def literais(chamada):
    """Texto SQL literal de uma chamada text(...) (a concatenacao implicita ja vem colada)."""
    return "".join(
        c.value for a in chamada.args for c in ast.walk(a)
        if isinstance(c, ast.Constant) and isinstance(c.value, str)
    )


sql_do_lookup = [(n, literais(n)) for n in chamadas_text if "regexp_replace(" in literais(n)]
NORMALIZADA = "regexp_replace(whatsapp, '[^0-9]', '', 'g')"

check(any(f"{NORMALIZADA} IN :grafias" in sql for _, sql in sql_do_lookup)
      and any(f"{NORMALIZADA} LIKE :sufixo" in sql for _, sql in sql_do_lookup),
      "a camada 2 pre-filtra pelas grafias compativeis (IN) e pelo sufixo (LIKE), "
      "os dois sobre a expressao NORMALIZADA da coluna — o lead gravado com '+', "
      "espaco ou '-' so aparece assim")

check(any("IN :grafias" in sql and "LIKE" not in sql for _, sql in sql_do_lookup),
      "existe a variante SEM LIKE: o trecho de sufixo so entra no SQL quando "
      "padrao_sufixo(numero) nao e None (menos de 11 digitos nao identifica ninguem)")

check(not any(re.search(r"\bwhatsapp\s+(?:NOT\s+)?I?LIKE\b", literais(n), re.IGNORECASE)
              for n in chamadas_text),
      "NENHUM SQL de crm.py aplica LIKE sobre a coluna CRUA (ela guarda '+', "
      "espaco, '()' e '-': um padrao so de digitos nunca casaria com o lead "
      "formatado — o LIKE e sempre sobre a expressao normalizada)")

check(sql_do_lookup and all(
          {type(x).__name__ for a in n.args for x in ast.walk(a)} == {"Constant"}
          for n, _ in sql_do_lookup),
      "o SQL do lookup e literal puro (sem f-string, .format ou concatenacao com "
      "variavel): o numero so entra por bind param, nunca na string SQL")

check(sql_do_lookup and all(sql.rstrip().endswith("LIMIT 50") for _, sql in sql_do_lookup),
      "toda consulta de candidatos do lookup tem LIMIT 50")

check(any(
          isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "bindparam"
          and n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == "grafias"
          and any(k.arg == "expanding" and isinstance(k.value, ast.Constant)
                  and k.value.value is True for k in n.keywords)
          for n in ast.walk(arvore)),
      "as grafias entram por bindparam('grafias', expanding=True): lista de "
      "valores vinculados, nunca texto montado")

# A garantia "o SQL do lookup e literal puro" acima so enxerga `text()` cujo literal
# contem `regexp_replace(`. Um `text(sql)` montado por concatenacao nao tem literal
# nenhum e passaria despercebido. Aqui: TODO `text()` das duas funcoes recebe
# exatamente UM argumento posicional, e ele e uma constante string.
FUNCOES_DO_LOOKUP = {"lookup_lead_by_whatsapp", "_leads_da_mesma_linha"}
funcoes_do_lookup = [
    n for n in ast.walk(arvore)
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in FUNCOES_DO_LOOKUP
]
text_do_lookup = [
    c for f in funcoes_do_lookup for c in ast.walk(f)
    if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "text"
]
check({f.name for f in funcoes_do_lookup} == FUNCOES_DO_LOOKUP and text_do_lookup and all(
          len(c.args) == 1 and not c.keywords
          and isinstance(c.args[0], ast.Constant) and isinstance(c.args[0].value, str)
          for c in text_do_lookup),
      f"todo text() de lookup_lead_by_whatsapp e _leads_da_mesma_linha ({len(text_do_lookup)}) "
      "recebe UMA constante string: um `text(sql)` montado por concatenacao nao passa")

# O LIMIT do SQL da camada 2 e o limite com que o fail-closed compara tem que ser o
# MESMO numero: se o SQL devolvesse mais linhas que o limite comparado, o truncamento
# passaria sem ser visto (e se devolvesse menos, nunca dispararia).
limite_constante = next(
    (n.value.value for n in arvore.body
     if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
     and any(isinstance(t, ast.Name) and t.id == "_LIMITE_CANDIDATOS" for t in n.targets)),
    None,
)
limites_do_sql_camada_2 = {
    int(n) for _, sql in sql_do_lookup if "IN :grafias" in sql
    for n in re.findall(r"LIMIT (\d+)\s*$", sql.strip())
}
check(isinstance(limite_constante, int) and limites_do_sql_camada_2 == {limite_constante},
      f"o LIMIT do SQL da camada 2 ({sorted(limites_do_sql_camada_2)}) e o limite do "
      f"fail-closed (_LIMITE_CANDIDATOS={limite_constante}) sao o mesmo numero")


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


class CapturaLogs(logging.Handler):
    """Guarda os registros de `app.services.crm` (CONV-LEAD-FONE-01). Serve a dois
    fins: o teste afirma o que foi logado — e que o telefone completo NAO foi — e o
    run verde nao joga linha de log no stderr (sem handler, o `lastResort` do
    logging imprime todo WARNING)."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.registros = []

    def emit(self, record):
        self.registros.append(record)

    def limpar(self):
        self.registros.clear()

    def mensagens(self, nivel):
        """Mensagens JA formatadas dos registros de exatamente este nivel."""
        return [r.getMessage() for r in self.registros if r.levelno == nivel]


captura = CapturaLogs()
logger_crm = logging.getLogger("app.services.crm")

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


def nova_conversa(db, whatsapp=NUMERO_META):
    db.execute(text("DELETE FROM conversations"))
    db.commit()
    conv = Conversation(whatsapp=whatsapp, nome="Maria", lead_id=0)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


db = SessionLocal()  # fechada no `finally` la embaixo, inclusive quando um check estoura


async def procurar(numero, *leads):
    """Recomeca o recorte com SO estes leads ((id, nome, whatsapp)...) e procura `numero`."""
    semear(*leads)
    db.rollback()
    return await crm.lookup_lead_by_whatsapp(numero, db)


def id_de(lead):
    return lead["id"] if lead else None


def confere_aviso_mascarado(numero, trecho, rotulo):
    """CONV-LEAD-FONE-01: o WARNING emitido desde o ultimo `captura.limpar()` existe
    (UM, com `trecho` no texto) e traz so os 4 ultimos digitos do numero — o log nao
    carrega o telefone do cliente. Alem de "o numero completo nao aparece", vale a
    regra geral: nenhuma sequencia de 6+ digitos (ids e contagens sao curtos)."""
    avisos = [m for m in captura.mensagens(logging.WARNING) if trecho in m]
    mascara = "***" + numero[-4:]
    check(len(avisos) == 1 and mascara in avisos[0],
          f"{rotulo}: UM WARNING com {trecho!r} e o numero mascarado como {mascara} "
          f"(avisos={avisos})")
    check(all(numero not in m and numero[-11:] not in m and not re.search(r"\d{6,}", m)
              for m in avisos),
          f"{rotulo}: o WARNING nao contem o telefone (nem completo, nem os 11 ultimos "
          "digitos, nem qualquer sequencia de 6+ digitos)")


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

    # CONTRATO MUDOU DE PROPOSITO (CONV-LEAD-FONE-01). Este bloco congelava como
    # "LIMITE CONHECIDO" que numero gravado SEM DDI (11 digitos) nao casava com o
    # wa_id da Meta (13): o eixo DDI ficava para outro WP, junto com o irmao do
    # CRM (app/routers/leads.py::get_lead_by_whatsapp). Esse WP e este — a
    # camada 2 do lookup reconhece o MESMO celular em outra grafia, entao o lead
    # `(11) 98765-4322` / `11987654322` agora CASA com 5511987654322. Antes, o
    # webhook nao o achava e `auto_create_lead_in_crm` criava um duplicado. O que
    # o F10 protege (DDD diferente, fixo x celular, 10 ultimos digitos, numero
    # sem DDD) segue valendo: ver 2.5.
    for fmt in ["(11) 98765-4322", "11987654322"]:
        achado, bloquear = await procurar(NUMERO_META, (200, "Maria", fmt))
        check(id_de(achado) == 200 and bloquear is False,
              f"{fmt!r} (sem DDI) CASA com {NUMERO_META}: e a mesma linha, so "
              f"gravada sem o 55 (devolveu {id_de(achado)}, bloquear={bloquear})")

    # 2.3 ambiguidade: formatado + digits-only, o MESMO cliente
    semear((200, "Maria (site)", FORMATADO), (201, "Maria (wpp)", NUMERO_META))
    db.rollback()
    captura.limpar()
    lead, bloquear = await crm.lookup_lead_by_whatsapp(NUMERO_META, db)
    check(lead is None,
          "dois leads com o mesmo numero normalizado: o lookup NAO elege "
          f"nenhum (devolveu {lead and lead['id']})")
    check(bloquear is True,
          "e sinaliza ambiguidade — antes o par formatado/nao-formatado era "
          "invisivel e o lookup devolvia o #201 com confianca")
    confere_aviso_mascarado(NUMERO_META, "AMBIGUO", "ambiguidade na camada 1")

    conv = nova_conversa(db)
    ligado = await crm.auto_link_conversation(conv, db)
    check(ligado is False and conv.lead_id == 0,
          f"a conversa fica SEM vinculo (devolveu {ligado}, lead_id={conv.lead_id})")
    check(ids_dos_leads(db) == [200, 201],
          f"e NENHUM terceiro lead nasce da ambiguidade (leads={ids_dos_leads(db)})")

    # ── CONV-LEAD-FONE-01: o MESMO celular em outra grafia ────────────────
    # 2.4 A Meta entrega o wa_id SEM o 9o digito (DDD 31 esta fora de 11-19, 21,
    # 22, 24, 27 e 28) e o cadastro a mao grava COM ele — em qualquer formato.
    WA_SEM_NONO = "553187654321"
    for gravado in ["5531987654321", "(31) 98765-4321", "+55 31 98765-4321"]:
        lead, bloquear = await procurar(WA_SEM_NONO, (210, "Joao (modal)", gravado))
        check(id_de(lead) == 210 and bloquear is False,
              f"wa_id {WA_SEM_NONO} (sem o 9) acha o lead gravado como {gravado!r} "
              f"(devolveu {id_de(lead)}, bloquear={bloquear})")
    lead, bloquear = await procurar("5531987654321", (210, "Joao (BIA)", WA_SEM_NONO))
    check(id_de(lead) == 210 and bloquear is False,
          f"e o sentido inverso: wa_id COM o 9 acha o lead gravado cru como "
          f"{WA_SEM_NONO} (devolveu {id_de(lead)}, bloquear={bloquear})")
    # (O wa_id de 10 digitos sem DDI, que a camada 2 chegou a ler como DDD 11 + 8
    # digitos, saiu daqui: wa_id de 10/11 digitos nao e brasileiro E.164 e a camada
    # 2 nao roda para ele — ver 2.10.)

    # 2.5 NAO e a mesma linha (F10): nenhum casa, e NENHUM bloqueia a criacao —
    # o numero e de outra pessoa, nao e ambiguidade. As linhas de DDD 49/54 e
    # 19/51 e a do prefixo `00` sao as que o pre-filtro SQL DEVOLVE (o sufixo de
    # 11 digitos alinha): ali quem recusa e o Python, nao o SQL.
    NAO_CASAM = [
        ("5511987654322", "(21) 98765-4322", "DDD diferente (11 x 21), mesmo assinante"),
        ("5531933456789", "(31) 3345-6789", "fixo do lead x celular do wa_id de mesmo final"),
        ("553133456789", "5531933456789", "celular do lead x fixo do wa_id de mesmo final"),
        ("5511987654322", "(19) 8765-4322", "os 10 ultimos digitos nao identificam ninguem"),
        ("5511987654322", "98765-4322", "numero sem DDD nunca casa"),
        ("554987654321", "54987654321", "DDD 49 x 54: o sufixo alinha, a leitura canonica recusa"),
        ("551987654321", "51987654321", "DDD 19 x 51: idem"),
        ("5554987654321", "554987654321", "DDD 54 x 49, sentido inverso"),
        ("5511987654322", "+00 55 11 98765-4322",
         "LIMITE CONHECIDO: com prefixo-lixo `00` o lead nao le e o wa_id le - so a leitura "
         "decide, e o sufixo (que antes os juntava) so vale entre numeros sem leitura"),
    ]
    for wa, gravado, motivo in NAO_CASAM:
        lead, bloquear = await procurar(wa, (211, "Outra pessoa", gravado))
        check((lead, bloquear) == (None, False),
              f"{gravado!r} NAO casa com {wa} — {motivo} "
              f"(devolveu {id_de(lead)}, bloquear={bloquear})")

    # O mesmo ruido de pre-filtro, agora ao lado da linha CERTA: o lead 251 (DDD 54)
    # volta do SQL junto com o 250 (DDD 49, o mesmo numero com o 9). Se o ruido
    # contasse, viraria ambiguidade falsa e a conversa ficaria sem vinculo.
    lead, bloquear = await procurar("554987654321",
                                    (250, "Cliente DDD 49", "5549987654321"),
                                    (251, "Cliente DDD 54", "54987654321"))
    check(id_de(lead) == 250 and bloquear is False,
          f"candidato que so o sufixo alinha NAO conta como ambiguidade: vence a "
          f"linha certa (devolveu {id_de(lead)}, bloquear={bloquear})")

    # 2.6 PRECEDENCIA: o lead EXATO ganha de outro lead da mesma linha em outra
    # grafia — a camada 2 nem roda. Os dois sentidos, para o vencedor nao depender
    # de qual dos ids e o menor.
    for wa, exato in [("553187654321", 221), ("5531987654321", 220)]:
        lead, bloquear = await procurar(wa,
                                        (220, "Joao (modal, com o 9)", "5531987654321"),
                                        (221, "Joao (BIA, wa_id cru)", "553187654321"))
        check(id_de(lead) == exato and bloquear is False,
              f"com os leads 5531987654321 e 553187654321 no banco, o lookup por {wa} "
              f"devolve o EXATO (#{exato}) sem bloquear "
              f"(devolveu {id_de(lead)}, bloquear={bloquear})")

    # 2.7 Ambiguidade na camada 2: nenhum lead e exato, dois sao a mesma linha.
    semear((230, "Joao (modal)", "5531987654321"), (231, "Joao (planilha)", "(31) 98765-4321"))
    db.rollback()
    captura.limpar()
    lead, bloquear = await crm.lookup_lead_by_whatsapp(WA_SEM_NONO, db)
    check(lead is None and bloquear is True,
          f"dois leads da mesma linha, nenhum exato: o lookup NAO elege nenhum e "
          f"sinaliza ambiguidade (devolveu {id_de(lead)}, bloquear={bloquear})")
    confere_aviso_mascarado(WA_SEM_NONO, "AMBIGUO", "ambiguidade na camada 2")

    conv = nova_conversa(db, WA_SEM_NONO)
    ligado = await crm.auto_link_conversation(conv, db)
    check(ligado is False and conv.lead_id == 0,
          f"a conversa fica SEM vinculo (devolveu {ligado}, lead_id={conv.lead_id})")
    check(ids_dos_leads(db) == [230, 231],
          f"e NENHUM terceiro lead nasce da ambiguidade (leads={ids_dos_leads(db)})")

    # 2.8 Sufixo (regra (b)): SO entre numeros SEM leitura brasileira, e o LIKE incide
    # sobre os digitos NORMALIZADOS da coluna. O wa_id `5556987654321` tem a forma de um
    # E.164 brasileiro (55 + 11 digitos), mas o DDD 56 nao existe: nao le. O lead
    # `+56 9 8765 4321` tambem nao le (`56987654321`). Os 11 ultimos digitos coincidem,
    # entao (b) os junta — e o pre-filtro so traz o lead pelo LIKE, porque o texto cru
    # (com '+' e espacos) jamais terminaria nesses 11 digitos. E um cenario de
    # laboratorio: a Meta nao entrega esse wa_id; ele existe para provar o LIKE sobre a
    # expressao normalizada pelo caminho do Conversas, agora que o prefixo `00` (2.5) e
    # o wa_id estrangeiro (2.10) nao chegam mais ao sufixo.
    lead, bloquear = await procurar("5556987654321",
                                    (240, "Cliente (formatado)", "+56 9 8765 4321"))
    check(id_de(lead) == 240 and bloquear is False,
          f"wa_id 5556987654321 (sem leitura) casa por sufixo de 11 digitos com o lead "
          f"gravado como '+56 9 8765 4321' (devolveu {id_de(lead)}, bloquear={bloquear})")

    # 2.9 O RELATO DO DONO, ponta a ponta: lead cadastrado pelo modal do CRM
    # (55 + DDD + 9 digitos) e conversa nova cujo wa_id a Meta entrega SEM o 9o
    # digito. Antes: lookup None -> auto_create_lead_in_crm -> lead DUPLICADO e a
    # conversa presa nele. Agora: a conversa vincula ao lead que o dono digitou.
    semear((300, "Joao (modal)", "5548988711776"))
    db.rollback()
    conv = nova_conversa(db, "554888711776")
    ligado = await crm.auto_link_conversation(conv, db)
    check(ligado is True and conv.lead_id == 300,
          f"conversa com wa_id 554888711776 vincula ao lead 300, cadastrado como "
          f"5548988711776 (devolveu {ligado}, lead_id={conv.lead_id})")
    check(ids_dos_leads(db) == [300],
          f"e NENHUM lead novo e criado (leads={ids_dos_leads(db)})")

    # 2.10 A camada 2 so roda para wa_id BRASILEIRO E.164 (55 + 12/13 digitos). O wa_id
    # da Meta e sempre E.164 e o `/initiate` forca o 55: um wa_id de 10/11 digitos aqui
    # e ESTRANGEIRO, nunca um brasileiro sem DDI. Lido como brasileiro, o celular do Peru
    # `51987654321` viraria o "DDD 51" de Porto Alegre e a conversa ficaria presa PARA
    # SEMPRE ao lead do modal `5551987654321`. A regra (telefone_identidade) casa cada
    # par abaixo — a 1a checagem confirma — e quem barra e o lookup, nao a regra.
    NAO_E164_BR = [
        ("51987654321", "5551987654321", "Peru (11 digitos) x lead do modal em DDD 51"),
        ("56987654321", "5556987654321",
         "Chile x lead que o modal antigo corrompia (F-084, adiado: no Conversas deixa de casar)"),
        ("1187654322", "5511987654322", "10 digitos, sem DDI: o modulo leria DDD 11 + 8 digitos"),
        ("11987654322", "5511987654322", "11 digitos, sem DDI: o modulo leria DDD 11 + 9 digitos"),
        # Fronteiras da guarda: comprimento E prefixo contam, os dois.
        ("5587654321", "5555987654321",
         "10 digitos que comecam em 55: e o DDD 55 (RS) sem DDI, nao um E.164 brasileiro"),
        ("55987654321", "5555987654321", "11 digitos que comecam em 55: idem"),
        ("595981234567", "00595981234567",
         "Paraguai: 12 digitos que NAO comecam em 55 x lead com prefixo `00`"),
    ]
    for wa, gravado, motivo in NAO_E164_BR:
        check(crm.mesmo_telefone(wa, gravado),
              f"pre-condicao: a regra casa {wa} com {gravado} — {motivo}")
        lead, bloquear = await procurar(wa, (270, "Lead do CRM", gravado))
        check((lead, bloquear) == (None, False),
              f"wa_id {wa} NAO acha {gravado}: a camada 2 nao roda para quem nao e "
              f"55 + 12/13 digitos — {motivo} (devolveu {id_de(lead)}, bloquear={bloquear})")
    # A guarda desliga so a camada 2: a igualdade EXATA (camada 1) continua valendo.
    lead, bloquear = await procurar("51987654321", (271, "Cliente do Peru", "+51 987 654 321"))
    check(id_de(lead) == 271 and bloquear is False,
          f"o mesmo wa_id estrangeiro ainda acha o lead gravado IGUAL ('+51 987 654 321'): "
          f"a camada 1 nao depende da guarda (devolveu {id_de(lead)}, bloquear={bloquear})")

    # 2.11 Falha na camada 2 NAO e "numero novo": o lookup devolve (None, True) e
    # auto_link_conversation nao vincula nem cria. `padrao_sufixo` devolvendo um INT
    # faz o SQL sair como `... LIKE 12345` — erro de analise no PostgreSQL —, um erro
    # que SO acontece dentro da camada 2 (a camada 1 ja passou). O lead 260 e o que a
    # camada 2 acharia se funcionasse: se o erro fosse engolido como "nenhum lead", o
    # resultado seria (None, False) e a conversa ganharia um duplicado.
    semear((260, "Maria", "(11) 98765-4322"))
    conv = nova_conversa(db)
    sufixo_original = crm.padrao_sufixo
    crm.padrao_sufixo = lambda numero: 12345
    try:
        captura.limpar()
        lead, bloquear = await crm.lookup_lead_by_whatsapp(NUMERO_META, db)
        erros = captura.mensagens(logging.ERROR)
        db.rollback()  # o lookup nao desfaz a transacao abortada (fica para um WP proprio)
        check((lead, bloquear) == (None, True),
              f"consulta da camada 2 que falha: (None, True), nunca 'numero novo' "
              f"(devolveu {id_de(lead)}, bloquear={bloquear})")
        check(len(erros) == 1 and re.search(r"\w+Error\b", erros[0])
              and not re.search(r"\d{6,}", erros[0]),
              f"o ERROR loga o TIPO da excecao e nunca a mensagem (o SQLAlchemy carrega os "
              f"parametros, ou seja, o telefone) (erros={erros})")
        captura.limpar()
        ligado = await crm.auto_link_conversation(conv, db)
        leads = ids_dos_leads(db)
        check(ligado is False and conv.lead_id == 0,
              f"auto_link_conversation nao vincula (devolveu {ligado}, lead_id={conv.lead_id})")
        check(leads == [260], f"e NENHUM lead novo e criado (leads={leads})")
    finally:
        crm.padrao_sufixo = sufixo_original
    lead, bloquear = await procurar(NUMERO_META, (260, "Maria", "(11) 98765-4322"))
    check(id_de(lead) == 260 and bloquear is False,
          f"controle: sem a falha injetada o MESMO lookup acha o lead 260 — o (None, True) "
          f"acima veio da camada 2 (devolveu {id_de(lead)}, bloquear={bloquear})")

    # 2.12 FAIL-CLOSED no LIMIT. O pre-filtro da camada 2 traz no maximo LIMITE linhas, na
    # ordem em que o banco as varre; se vier cheio, o Python decidiria sobre uma AMOSTRA:
    # um lead legitimo que ficou de fora nao conta, e quem sobrou parece unico. Entao
    # encher o limite e ambiguidade — (None, True), sem vinculo e sem lead criado.
    # O "ruido" sao leads que o pre-filtro traz (terminam nos 11 ultimos digitos do wa_id)
    # e o Python recusa (14 digitos, nenhum le como brasileiro); entram ANTES do legitimo.
    limite = crm._LIMITE_CANDIDATOS

    def ruido(quantos):
        return [(300 + i, f"Ruido {i}", f"{900 + i}11987654322") for i in range(quantos)]

    legitimo = (400, "Maria (legitima)", "(11) 98765-4322")
    for quantos, decide, rotulo in [
        (limite - 2, True, "limite-1 candidatos: o pre-filtro nao enche o limite, decide"),
        (limite - 1, False, "exatamente o limite: nao da para saber se truncou, recusa"),
        (limite, False, "limite+1: truncado de verdade, o legitimo ficou de fora, recusa"),
    ]:
        captura.limpar()
        lead, bloquear = await procurar(NUMERO_META, *ruido(quantos), legitimo)
        if decide:
            check(id_de(lead) == 400 and bloquear is False,
                  f"{rotulo} (devolveu {id_de(lead)}, bloquear={bloquear})")
            continue
        check((lead, bloquear) == (None, True),
              f"{rotulo} (devolveu {id_de(lead)}, bloquear={bloquear})")
        confere_aviso_mascarado(NUMERO_META, "limite", rotulo)
        conv = nova_conversa(db)
        ligado = await crm.auto_link_conversation(conv, db)
        leads = ids_dos_leads(db)
        check(ligado is False and conv.lead_id == 0 and len(leads) == quantos + 1,
              f"{rotulo}: a conversa fica SEM vinculo e NENHUM lead nasce "
              f"(devolveu {ligado}, lead_id={conv.lead_id}, leads={len(leads)} de {quantos + 1})")


try:
    montar()
    logger_crm.addHandler(captura)
    logger_crm.propagate = False  # o registro fica so no handler de captura: nada vai ao root nem ao stderr
    asyncio.run(cenarios())
finally:
    logger_crm.removeHandler(captura)
    logger_crm.propagate = True
    # A ORDEM importa: a sessao segura uma transacao aberta e o DROP SCHEMA
    # CASCADE fica na fila atras dela. Fechar primeiro, derrubar depois.
    db.close()
    engine.dispose()
    with bruto.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    bruto.close()

print(f"\n{'FALHOU' if falhas else 'OK'} — {len(falhas)} falha(s)")
sys.exit(1 if falhas else 0)
