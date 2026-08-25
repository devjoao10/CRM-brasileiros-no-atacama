# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-F2 — a m009 era FAIL-OPEN, e isso foi reproduzido em PostgreSQL 16
real: com `service_templates` ja existente e a constraint
`uq_service_templates_name_language` REMOVIDA, a migration reportava

    uq_service_templates_name_language:AUSENTE (verificar manualmente)

e mesmo assim terminava com

    [m009] OK (idempotente)
    EXIT CODE: 0

Ou seja: ela DETECTAVA a quebra do invariante central da tabela, escrevia isso
no log, e devolvia sucesso. Um pipeline que confia no exit code — que e o unico
jeito automatizavel de confiar numa migration — seguia adiante achando que o
schema estava integro. E o invariante em questao nao e cosmetico: sem a UNIQUE,
`(name, language)` deixa de ser identidade e a curadoria de templates passa a
poder ter duas linhas para o mesmo par, com a UI mostrando duplicata e o
"autorizado" virando ambiguo.

Este arquivo EXECUTA a migration de verdade, em banco descartavel, nos quatro
cenarios que importam:

  1. tabela ausente          -> cria, com a UNIQUE, exit 0
  2. segunda execucao        -> no-op, exit 0 (idempotente)
  3. tabela SEM a UNIQUE,
     SEM duplicatas          -> cria a constraint, exit 0
  4. tabela SEM a UNIQUE,
     COM duplicatas          -> ABORTA, exit != 0, lista as chaves em conflito,
                                NAO apaga e NAO deduplica nada

Alem disso, trava o DDL que seria emitido em PostgreSQL — producao e PostgreSQL,
e um teste que so exercitasse o caminho do SQLite poderia mascarar exatamente o
erro que ele deveria pegar.

Rodar:  python tests/test_migration_m009.py
"""
import io
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
TMP = REPO / "scratch" / "prova_m009"
if TMP.exists():
    shutil.rmtree(TMP)
TMP.mkdir(parents=True)

# Este arquivo exercita a migration do CONVERSAS. O literal abaixo tambem e o
# discriminador de job do CI (.github/workflows/test.yml separa as duas suites
# com `grep -L/-l CONVERSAS_DIR tests/test_*.py`), entao ele coloca este teste no
# job certo — o unico com as dependencias do Conversas.
CONVERSAS_DIR = REPO / "conversas"
MIGRATION = REPO / "migrations" / "m009_conversas_service_templates.py"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover
    pass

falhas = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        falhas.append(msg)


def roda(db_path):
    """Executa a migration em processo proprio e devolve (rc, saida)."""
    env = dict(os.environ)
    env["ENVIRONMENT"] = "development"
    env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    env["SECRET_KEY"] = "test-secret-key"
    env["CONVERSAS_SEED_DEV_DATA"] = "false"
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run(
        [sys.executable, str(MIGRATION)],
        cwd=str(REPO), env=env, capture_output=True,
        encoding="utf-8", errors="replace", timeout=300,
    )
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def uniques_de(db_path):
    """Nomes dos indices UNIQUE sobre service_templates, via sqlite3 cru."""
    con = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in con.execute("PRAGMA index_list('service_templates')") if r[2]}
    finally:
        con.close()


def unicidade_vigora(db_path):
    """
    O invariante EXISTE de fato? Tenta gravar um par duplicado e ve se o banco
    recusa. Isto e melhor que conferir o NOME do indice por dois motivos:

      * o nome nao e portavel. Uma UNIQUE declarada no CREATE TABLE vira, no
        SQLite, o auto-indice `sqlite_autoindex_service_templates_1` — o nome
        `uq_service_templates_name_language` nao aparece no PRAGMA, embora a
        restricao esteja valendo. Conferir nome aqui reprovaria um schema
        correto;
      * o que a migration precisa garantir e comportamento, nao nomenclatura.

    Sempre desfaz o que escreveu — o teste nao pode sujar o cenario que mede.
    """
    con = sqlite3.connect(str(db_path))
    try:
        alvo = con.execute(
            "SELECT name, language FROM service_templates LIMIT 1").fetchone()
        if alvo is None:
            alvo = ("sonda_de_unicidade", "pt_BR")
            con.execute(
                "INSERT INTO service_templates (name, language) VALUES (?, ?)", alvo)
        try:
            con.execute(
                "INSERT INTO service_templates (name, language) VALUES (?, ?)", alvo)
        except sqlite3.IntegrityError:
            return True
        finally:
            con.rollback()
        return False
    finally:
        con.close()


def linhas(db_path):
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute(
            "SELECT id, name, language FROM service_templates ORDER BY id"
        ).fetchall()
    finally:
        con.close()


# ═══ 1. tabela ausente -> cria, com a UNIQUE ════════════════════════════════
print("1) banco vazio: a migration cria a tabela COM a UNIQUE")

db1 = TMP / "novo.db"
rc, out = roda(db1)
check(rc == 0, f"exit 0 (veio {rc})\n{out[-600:] if rc else ''}")
check("service_templates:created" in out, "reporta que criou a tabela")
check(db1.exists(), "arquivo de banco criado")

check(unicidade_vigora(db1),
      f"(name, language) e UNICO de fato — o banco recusa o par repetido "
      f"(indices unicos: {sorted(uniques_de(db1))})")
check(linhas(db1) == [], "bootstrap VAZIO — nenhuma linha inserida")
check("bootstrap vazio" in out, "o bootstrap vazio e reportado, nao silencioso")
check("OK" in out, "imprime OK quando o schema esta integro")


# ═══ 2. segunda execucao -> no-op ═══════════════════════════════════════════
print()
print("2) segunda execucao: no-op, continua exit 0")

rc, out = roda(db1)
check(rc == 0, f"exit 0 na segunda execucao (veio {rc})")
check("service_templates:already-present" in out, "detecta a tabela existente")
check("created" not in out.split("acoes:")[-1], "nao recria a tabela")
check(linhas(db1) == [], "continua sem inserir linha")
check("OK" in out, "imprime OK — o schema segue integro")


# ═══ 3. tabela SEM a UNIQUE e SEM duplicatas -> cria a constraint ═══════════
print()
print("3) tabela existente SEM a UNIQUE, sem duplicatas: a constraint e criada")

db3 = TMP / "sem_unique.db"
con = sqlite3.connect(str(db3))
con.executescript("""
    CREATE TABLE service_templates (
        id INTEGER PRIMARY KEY,
        name VARCHAR(512) NOT NULL,
        language VARCHAR(10) NOT NULL,
        created_at TIMESTAMP
    );
    INSERT INTO service_templates (name, language) VALUES ('boas_vindas', 'pt_BR');
    INSERT INTO service_templates (name, language) VALUES ('boas_vindas', 'es_ES');
    INSERT INTO service_templates (name, language) VALUES ('confirmacao', 'pt_BR');
""")
con.commit()
con.close()

antes = linhas(db3)
check(len(antes) == 3, "cenario montado: 3 autorizacoes, nenhuma duplicada")
check("uq_service_templates_name_language" not in uniques_de(db3),
      "cenario montado: a UNIQUE realmente NAO existe")

rc, out = roda(db3)
check(rc == 0, f"exit 0 apos criar a constraint (veio {rc})\n{out[-800:] if rc else ''}")
check(unicidade_vigora(db3),
      f"a unicidade passou a VIGORAR — o banco agora recusa o par repetido "
      f"(indices unicos: {sorted(uniques_de(db3))})")
check(linhas(db3) == antes, "nenhuma linha foi alterada, inserida ou apagada")
check("criada" in out.lower() or "criad" in out.lower(),
      "a criacao da constraint aparece nas acoes")
check("OK" in out, "imprime OK — agora o schema esta integro")

# e idempotente: rodar de novo nao tenta recriar
rc2, out2 = roda(db3)
check(rc2 == 0, f"exit 0 na re-execucao (veio {rc2})")
check("present" in out2, "na segunda vez a UNIQUE ja aparece como presente")


# ═══ 4. tabela SEM a UNIQUE e COM duplicatas -> ABORTA ══════════════════════
print()
print("4) tabela existente SEM a UNIQUE, COM duplicatas: aborta sem tocar no dado")

db4 = TMP / "com_duplicata.db"
con = sqlite3.connect(str(db4))
con.executescript("""
    CREATE TABLE service_templates (
        id INTEGER PRIMARY KEY,
        name VARCHAR(512) NOT NULL,
        language VARCHAR(10) NOT NULL,
        created_at TIMESTAMP
    );
    INSERT INTO service_templates (name, language) VALUES ('boas_vindas', 'pt_BR');
    INSERT INTO service_templates (name, language) VALUES ('boas_vindas', 'pt_BR');
    INSERT INTO service_templates (name, language) VALUES ('confirmacao', 'pt_BR');
    INSERT INTO service_templates (name, language) VALUES ('lembrete', 'es_ES');
    INSERT INTO service_templates (name, language) VALUES ('lembrete', 'es_ES');
""")
con.commit()
con.close()

antes4 = linhas(db4)
check(len(antes4) == 5, "cenario montado: 5 linhas, 2 pares duplicados")

rc, out = roda(db4)
check(rc != 0, f"exit code NAO-ZERO (veio {rc}) — este era o defeito: dava 0")
check("OK" not in out.split("[m009]")[-1] or "abort" in out.lower(),
      "NAO imprime OK quando o schema esta quebrado")
check("boas_vindas" in out and "pt_BR" in out,
      "reporta a chave conflitante (name, language) na saida")
check("lembrete" in out and "es_ES" in out,
      "reporta TODOS os pares em conflito, nao so o primeiro")
check("1" in out and "2" in out, "reporta os ids das linhas conflitantes")

check(linhas(db4) == antes4,
      f"NADA foi apagado nem deduplicado (antes={len(antes4)}, depois={len(linhas(db4))})")
check(not unicidade_vigora(db4),
      "nenhuma restricao foi criada sobre dado sujo — seria impossivel, e a "
      "tentativa nao pode ter apagado linha para caber")

# e a falha e estavel: rodar de novo aborta de novo, nunca "passa na segunda"
rc2, _ = roda(db4)
check(rc2 == rc, f"a falha e estavel entre execucoes (1a={rc}, 2a={rc2})")


# ═══ 5. o caminho de PRODUCAO e PostgreSQL ══════════════════════════════════
print()
print("5) o DDL do ramo PostgreSQL, que a suite SQLite nao executa")

fonte = MIGRATION.read_text(encoding="utf-8")

check("ALTER TABLE" in fonte and "ADD CONSTRAINT" in fonte,
      "existe o ramo PostgreSQL com ALTER TABLE ... ADD CONSTRAINT")
check("uq_service_templates_name_language" in fonte,
      "a constraint criada tem o nome que a verificacao procura")

# A guarda de duplicata precisa rodar ANTES do DDL. Medir POSICAO NO ARQUIVO
# seria um proxy fraco — e de fato quebrou quando uma docstring passou a citar
# "ADD CONSTRAINT". O que importa e a ordem DENTRO de run(), no fluxo real.
corpo_run = fonte.split("def run(", 1)[1]
chama_dup = corpo_run.find("_duplicatas(")
chama_ddl = corpo_run.find("_criar_unique(")
check(chama_dup > 0 and chama_ddl > 0 and chama_dup < chama_ddl,
      "dentro de run(), a consulta de duplicatas e chamada ANTES do DDL")
check("raise DuplicatasEncontradas" in corpo_run[chama_dup:chama_ddl],
      "e entre as duas ha o `raise` que impede o DDL quando ha duplicata")
# O comportamento em si ja foi provado no cenario 4; isto trava a ORDEM, que um
# teste de caixa preta nao consegue distinguir de "tentou o DDL e falhou".

for proibido in ("DELETE FROM", "DROP TABLE", "TRUNCATE", "INSERT INTO"):
    check(proibido not in fonte.upper().replace("SYS.PATH.INSERT", ""),
          f"a migration nao executa {proibido}")

# `sys.exit` com codigo != 0 e o unico contrato que um pipeline consegue ler.
check("sys.exit" in fonte, "a migration termina com sys.exit explicito")


print()
shutil.rmtree(TMP, ignore_errors=True)
if falhas:
    print(f"{len(falhas)} FALHA(S)")
    for f in falhas:
        print(f"  - {f}")
    sys.exit(1)
print("OK: a m009 falha ALTO quando o invariante esta quebrado")
