# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-F2 — a m010 era FAIL-OPEN, o mesmo defeito ja corrigido na m009,
reproduzido em PostgreSQL 16 real: com `template_param_maps` ja existente, 3
linhas validas, nenhuma duplicata, e a constraint `uq_template_param_maps_key`
AUSENTE (so a PK presente), a migration reportava

    uq_template_param_maps_key:AUSENTE (verificar manualmente)

e terminava com

    [m010] OK (idempotente)
    EXIT CODE: 0

Detectava a quebra do invariante, escrevia no log, e devolvia sucesso — um
pipeline que confia no exit code seguia adiante sobre schema quebrado.

O QUE SE PERDE SEM A UNIQUE, aqui, e concreto: `(name, language, position)` e a
identidade de um mapeamento `{{n}} -> @VARIAVEL`. Sem a restricao, duas linhas
podem mapear a MESMA posicao do MESMO template para tokens DIFERENTES, e o envio
escolhe uma por acaso — o cliente recebe `@PRIMEIRONOMECLIENTE` onde deveria vir
`@NOMEDOATENDENTE`, sem erro em lugar nenhum.

A CHAVE AQUI TEM TRES COLUNAS, e nao duas como na m009. O cenario D abaixo
inclui de proposito linhas que compartilham DUAS das tres colunas e nao sao
duplicatas: uma correcao copiada da m009 sem adaptar a chave abortaria sobre
dado legitimo, e este teste pega isso.

Cenarios (executam a migration DE VERDADE, em banco descartavel):

  A. tabela ausente                   -> cria, UNIQUE vigora, bootstrap vazio, exit 0
  B. segunda execucao                 -> no-op, exit 0
  C. sem UNIQUE, sem duplicatas       -> cria a garantia, dados intactos, exit 0,
                                         e a re-execucao continua no-op
  D. sem UNIQUE, com duplicatas       -> ABORTA com exit 2, lista todos os grupos
                                         e ids, nada apagado, nada deduplicado,
                                         nenhum token alterado, UNIQUE ainda ausente
  E. o DDL do ramo PostgreSQL         -> producao e PostgreSQL e esta suite roda
                                         SQLite; sem este bloco o ramo que importa
                                         nunca seria olhado

Rodar:  python tests/test_migration_m010.py
"""
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
TMP = REPO / "scratch" / "prova_m010"
if TMP.exists():
    shutil.rmtree(TMP)
TMP.mkdir(parents=True)

# Migration do CONVERSAS. O literal abaixo tambem e o discriminador de job do CI
# (.github/workflows/test.yml separa as suites com `grep -L/-l CONVERSAS_DIR`),
# entao ele coloca este teste no job que tem as dependencias do Conversas.
CONVERSAS_DIR = REPO / "conversas"
MIGRATION = REPO / "migrations" / "m010_conversas_template_param_maps.py"

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


ESQUEMA_SEM_UNIQUE = """
    CREATE TABLE template_param_maps (
        id INTEGER PRIMARY KEY,
        name VARCHAR(512) NOT NULL,
        language VARCHAR(10) NOT NULL,
        position INTEGER NOT NULL,
        token VARCHAR(61) NOT NULL,
        created_at TIMESTAMP
    );
"""


def uniques_de(db_path):
    con = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in con.execute("PRAGMA index_list('template_param_maps')") if r[2]}
    finally:
        con.close()


def unicidade_vigora(db_path):
    """
    A garantia EXISTE de fato? Tenta gravar um trio (name, language, position)
    repetido e ve se o banco recusa.

    Medir comportamento, e nao o NOME do indice, por dois motivos: o nome nao e
    portavel (uma UNIQUE declarada no CREATE TABLE vira, no SQLite, o auto-indice
    `sqlite_autoindex_...`, sem o nome da constraint) e o que a migration precisa
    garantir e a regra, nao a nomenclatura. Sempre desfaz o que escreveu.
    """
    con = sqlite3.connect(str(db_path))
    try:
        alvo = con.execute(
            "SELECT name, language, position FROM template_param_maps LIMIT 1").fetchone()
        if alvo is None:
            alvo = ("sonda_de_unicidade", "pt_BR", 1)
            con.execute(
                "INSERT INTO template_param_maps (name, language, position, token) "
                "VALUES (?, ?, ?, '@SONDA')", alvo)
        try:
            con.execute(
                "INSERT INTO template_param_maps (name, language, position, token) "
                "VALUES (?, ?, ?, '@SONDA')", alvo)
        except sqlite3.IntegrityError:
            return True
        finally:
            con.rollback()
        return False
    finally:
        con.close()


def linhas(db_path):
    """Linha INTEIRA, com token — o teste precisa provar que nada foi alterado."""
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute(
            "SELECT id, name, language, position, token FROM template_param_maps "
            "ORDER BY id"
        ).fetchall()
    finally:
        con.close()


# ═══ A. tabela ausente ══════════════════════════════════════════════════════
print("A) banco vazio: cria a tabela COM a garantia de unicidade")

dbA = TMP / "novo.db"
rc, out = roda(dbA)
check(rc == 0, f"exit 0 (veio {rc})\n{out[-600:] if rc else ''}")
check("template_param_maps:created" in out, "reporta que criou a tabela")
check(unicidade_vigora(dbA),
      f"(name, language, position) e UNICO de fato — o banco recusa o trio "
      f"repetido (indices unicos: {sorted(uniques_de(dbA))})")
check(linhas(dbA) == [], "bootstrap VAZIO — nenhuma linha inserida")
check("bootstrap vazio" in out, "o bootstrap vazio e reportado, nao silencioso")
check("OK" in out, "imprime OK quando o schema esta integro")


# ═══ B. segunda execucao ════════════════════════════════════════════════════
print()
print("B) segunda execucao: no-op, continua exit 0")

rc, out = roda(dbA)
check(rc == 0, f"exit 0 na segunda execucao (veio {rc})")
check("template_param_maps:already-present" in out, "detecta a tabela existente")
check("created" not in out.split("acoes:")[-1], "nao recria a tabela")
check(linhas(dbA) == [], "continua sem inserir linha")
check("OK" in out, "imprime OK — o schema segue integro")


# ═══ C. sem UNIQUE, sem duplicatas ══════════════════════════════════════════
print()
print("C) tabela existente SEM a UNIQUE, sem duplicatas: a garantia e criada")

dbC = TMP / "sem_unique.db"
con = sqlite3.connect(str(dbC))
con.executescript(ESQUEMA_SEM_UNIQUE + """
    INSERT INTO template_param_maps (name, language, position, token)
    VALUES ('boas_vindas', 'pt_BR', 1, '@PRIMEIRONOMECLIENTE');
    INSERT INTO template_param_maps (name, language, position, token)
    VALUES ('boas_vindas', 'pt_BR', 2, '@NOMEDOATENDENTE');
    INSERT INTO template_param_maps (name, language, position, token)
    VALUES ('boas_vindas', 'es_ES', 1, '@PRIMEIRONOMECLIENTE');
""")
con.commit()
con.close()

antesC = linhas(dbC)
check(len(antesC) == 3, "cenario montado: 3 mapeamentos (o mesmo do PostgreSQL real)")
check(not unicidade_vigora(dbC), "cenario montado: a garantia realmente NAO existe")

rc, out = roda(dbC)
check(rc == 0, f"exit 0 apos criar a garantia (veio {rc})\n{out[-800:] if rc else ''}")
check(unicidade_vigora(dbC),
      f"a unicidade passou a VIGORAR (indices unicos: {sorted(uniques_de(dbC))})")
check(linhas(dbC) == antesC,
      "nenhuma linha foi alterada, inserida ou apagada — token incluido")
check("criada" in out.lower(), "a criacao da garantia aparece nas acoes")
check("OK" in out, "imprime OK — agora o schema esta integro")

rc2, out2 = roda(dbC)
check(rc2 == 0, f"exit 0 na re-execucao (veio {rc2})")
check("present" in out2, "na segunda vez a garantia ja aparece como presente")
check(linhas(dbC) == antesC, "a re-execucao tambem nao mexeu em nada")


# ═══ D. sem UNIQUE, com duplicatas ══════════════════════════════════════════
print()
print("D) tabela existente SEM a UNIQUE, COM duplicatas: aborta sem tocar no dado")

dbD = TMP / "com_duplicata.db"
con = sqlite3.connect(str(dbD))
con.executescript(ESQUEMA_SEM_UNIQUE + """
    -- grupo 1: MESMO trio, tokens DIFERENTES. E exatamente a ambiguidade que a
    -- UNIQUE existe para impedir: o envio escolheria um dos dois por acaso.
    INSERT INTO template_param_maps (name, language, position, token)
    VALUES ('boas_vindas', 'pt_BR', 1, '@PRIMEIRONOMECLIENTE');
    INSERT INTO template_param_maps (name, language, position, token)
    VALUES ('boas_vindas', 'pt_BR', 1, '@NOMEDOATENDENTE');

    -- NAO sao duplicatas: compartilham DUAS das tres colunas da chave.
    -- Uma correcao copiada da m009 (que chaveia por duas colunas) abortaria
    -- aqui, sobre dado perfeitamente legitimo.
    INSERT INTO template_param_maps (name, language, position, token)
    VALUES ('boas_vindas', 'pt_BR', 2, '@DESTINO');
    INSERT INTO template_param_maps (name, language, position, token)
    VALUES ('boas_vindas', 'es_ES', 1, '@PRIMEIRONOMECLIENTE');
    INSERT INTO template_param_maps (name, language, position, token)
    VALUES ('confirmacao', 'pt_BR', 1, '@PRIMEIRONOMECLIENTE');

    -- grupo 2, com posicao de DOIS DIGITOS: se o relatorio ordenar a posicao
    -- como texto, 10 vem antes de 2 e a saida fica errada.
    INSERT INTO template_param_maps (name, language, position, token)
    VALUES ('lembrete', 'es_ES', 10, '@DATACHEGADA');
    INSERT INTO template_param_maps (name, language, position, token)
    VALUES ('lembrete', 'es_ES', 10, '@DATAPARTIDA');
    INSERT INTO template_param_maps (name, language, position, token)
    VALUES ('lembrete', 'es_ES', 2, '@DESTINO');
""")
con.commit()
con.close()

antesD = linhas(dbD)
check(len(antesD) == 8, "cenario montado: 8 linhas, 2 grupos duplicados, 4 linhas legitimas")

rc, out = roda(dbD)
check(rc == 2, f"exit code 2, o contrato da m009 (veio {rc}) — este era o defeito: dava 0")
check("OK (idempotente)" not in out, "NAO imprime OK quando o schema esta quebrado")

# todos os grupos, nao so o primeiro
check("boas_vindas" in out, "reporta o grupo 1 (boas_vindas)")
check("lembrete" in out, "reporta o grupo 2 (lembrete) — nao para no primeiro")
check("pt_BR" in out and "es_ES" in out, "reporta o idioma de cada grupo")
check("position" in out.lower() or "posicao" in out.lower(),
      "a posicao aparece nomeada no relatorio")

# ids das linhas conflitantes
check("[1, 2]" in out or "1, 2" in out, f"reporta os ids do grupo 1\n{out[-700:]}")
check("[6, 7]" in out or "6, 7" in out, "reporta os ids do grupo 2")

# A CHAVE DE TRES COLUNAS. Este bloco e o que pega uma correcao copiada da
# m009 sem adaptar a chave: la ela e (name, language), e com duas colunas as
# quatro linhas legitimas abaixo seriam acusadas de conflito.
relatorio = out.split("ABORTADA", 1)[-1]

check("4 linha(s) envolvida(s)" in relatorio,
      "o relatorio conta 4 linhas em conflito, das 8 existentes — nao 8")
check("2 grupo(s)" in relatorio, "e conta 2 grupos, nao mais")

# As legitimas, pelo token que so elas tem e pela posicao que so elas usam:
check("@DESTINO" not in relatorio,
      "('boas_vindas','pt_BR',2) e ('lembrete','es_ES',2) NAO sao conflito — "
      "so compartilham DUAS das tres colunas com as duplicadas")
check("confirmacao" not in relatorio,
      "('confirmacao','pt_BR',1) NAO e conflito — nome diferente")
check("position=2 " not in relatorio and not relatorio.rstrip().endswith("position=2"),
      "nenhum grupo de position=2 aparece no relatorio")
check(relatorio.count("position=") == 2,
      f"exatamente dois grupos listados (got {relatorio.count('position=')})")

# A ordenacao usa a posicao como NUMERO: com dois grupos, 1 vem antes de 10.
check(0 <= relatorio.find("position=1 ") < relatorio.find("position=10"),
      "position ordenada como numero, nao como texto (1 antes de 10)")

# nada foi tocado
check(linhas(dbD) == antesD,
      f"NADA apagado, alterado ou deduplicado (antes={len(antesD)}, depois={len(linhas(dbD))})")
check([r[4] for r in linhas(dbD)] == [r[4] for r in antesD],
      "nenhum token foi modificado")
check(not unicidade_vigora(dbD), "a garantia continua AUSENTE — nada foi criado sobre dado sujo")

rc2, _ = roda(dbD)
check(rc2 == 2, f"a falha e estavel entre execucoes (1a={rc}, 2a={rc2})")


# ═══ E. o ramo PostgreSQL ═══════════════════════════════════════════════════
print()
print("E) o DDL de PRODUCAO (PostgreSQL), que esta suite SQLite nao executa")

fonte = MIGRATION.read_text(encoding="utf-8")

check("ADD CONSTRAINT" in fonte, "existe o ramo PostgreSQL com ADD CONSTRAINT")
check("uq_template_param_maps_key" in fonte,
      "a garantia criada tem o nome exigido")
check('"name", "language", "position"' in fonte
      or "('name', 'language', 'position')" in fonte
      or '("name", "language", "position")' in fonte
      or "CHAVE = (" in fonte,
      "a chave de tres colunas esta declarada em um lugar so")

corpo_run = fonte.split("def run(", 1)[1]
chama_dup = corpo_run.find("_duplicatas(")
chama_ddl = corpo_run.find("_criar_unique(")
check(chama_dup > 0 and chama_ddl > 0 and chama_dup < chama_ddl,
      "dentro de run(), a consulta de duplicatas e chamada ANTES do DDL")
check("raise DuplicatasEncontradas" in corpo_run[chama_dup:chama_ddl],
      "e entre as duas ha o `raise` que impede o DDL quando ha duplicata")

for proibido in ("DELETE FROM", "DROP TABLE", "TRUNCATE", "UPDATE ", "INSERT INTO"):
    check(proibido not in fonte.upper().replace("SYS.PATH.INSERT", ""),
          f"a migration nao executa {proibido.strip()}")

# A chave da migration nao pode divergir do MODELO. Sem este check, alguem
# poderia acrescentar uma coluna a UniqueConstraint em conversas/app/models e a
# migration seguiria criando a garantia antiga em bancos existentes — schema
# diferente conforme a idade do banco, que e o defeito que migrations existem
# para impedir. Por AST, e nao por regex, para nao casar texto de comentario.
import ast as _ast  # noqa: E402

modelo_src = (CONVERSAS_DIR / "app" / "models" / "template.py").read_text(encoding="utf-8")
chave_do_modelo = None
for no in _ast.walk(_ast.parse(modelo_src)):
    if not (isinstance(no, _ast.Call)
            and getattr(no.func, "id", "") == "UniqueConstraint"):
        continue
    nomeado = [k.value.value for k in no.keywords
               if k.arg == "name" and isinstance(k.value, _ast.Constant)]
    if nomeado == ["uq_template_param_maps_key"]:
        chave_do_modelo = tuple(
            a.value for a in no.args if isinstance(a, _ast.Constant))
check(chave_do_modelo == ("name", "language", "position"),
      f"o modelo declara a chave (name, language, position) (achei {chave_do_modelo})")

chave_da_migration = None
for no in _ast.walk(_ast.parse(fonte)):
    if (isinstance(no, _ast.Assign)
            and any(getattr(t, "id", "") == "CHAVE" for t in no.targets)
            and isinstance(no.value, _ast.Tuple)):
        chave_da_migration = tuple(
            e.value for e in no.value.elts if isinstance(e, _ast.Constant))
check(chave_da_migration == chave_do_modelo,
      f"a chave da migration {chave_da_migration} e IDENTICA a do modelo "
      f"{chave_do_modelo} — se divergirem, bancos novos e antigos ficam com "
      f"schemas diferentes")

check("sys.exit(2)" in fonte, "duplicata termina com sys.exit(2)")
check("sys.exit(1)" in fonte, "schema nao integro apos o DDL termina com sys.exit(1)")


print()
shutil.rmtree(TMP, ignore_errors=True)
if falhas:
    print(f"{len(falhas)} FALHA(S)")
    for f in falhas:
        print(f"  - {f}")
    sys.exit(1)
print("OK: a m010 falha ALTO quando o invariante esta quebrado")
