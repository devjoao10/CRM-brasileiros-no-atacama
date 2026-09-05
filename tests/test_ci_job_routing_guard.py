# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-WF2 — guard do roteamento de jobs do CI.

.github/workflows/test.yml divide a suite em dois jobs por uma string-marcador:
o job `crm` (Python 3.11 + requirements.txt) roda o que NAO casa o grep, o job
`conversas` (3.12 + conversas/requirements.txt) roda o que casa. O proprio
workflow declara a regra: o marcador esta "presente APENAS nos testes que
inserem conversas/ no sys.path".

O QUE ISTO PEGA — prosa que dispara o grep. tests/test_migration_m011.py
explicava na docstring, corretamente, por que pertencia ao job `crm`; ao
SOLETRAR o marcador dentro da explicacao, foi roteado para o job `conversas`,
onde `import app.main` do CRM estoura em ModuleNotFoundError (falta slowapi). A
frase que documentava o discriminador disparou o discriminador — grep nao le
Python: para ele, docstring e codigo sao a mesma coisa.

REGRA: se o arquivo casa o grep, o marcador precisa SOBREVIVER depois de tirar
comentarios e docstrings. Sobreviveu -> uso deliberado, roteamento correto (e o
que test_migration_m009.py e test_conversas_security.py fazem: uma atribuicao
declarada de proposito, mesmo sem usar o valor). Sobrou so prosa -> roteamento
acidental.

O SENTIDO INVERSO nao e mecanizavel aqui: o ambiente do job `crm` e
superconjunto do outro (so `httpx` e `pydantic` existem so no Conversas, e o job
crm instala os dois), entao um teste do Conversas parado no job `crm` PASSA — so
passa contra os pins errados. Isso e intencao, nao sintaxe; ver AUDIT-2026-08-W0
no test.yml, que registra test_conversas_security.py nesse estado.

Rodar:  python tests/test_ci_job_routing_guard.py
"""
import ast
import io
import pathlib
import sys
import tokenize

# O marcador e MONTADO por concatenacao, nunca escrito inteiro. Um guard que o
# soletrasse seria roteado por ele para o job `conversas` — violando, no proprio
# arquivo, a regra que enforca. NAO "simplifique" isto para o literal.
MARCADOR = "CONVERSAS" + "_DIR"

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTES = sorted((ROOT / "tests").glob("test_*.py"))

falhas = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        falhas.append(msg)


def codigo_sem_prosa(fonte):
    """Devolve o texto do arquivo sem comentarios e sem docstrings.

    tokenize + ast da stdlib, nao regex: aspas triplas, strings cruas, `#`
    dentro de string e docstring multilinha quebram qualquer regex que tente
    fazer isto. Docstring aqui = statement que e SO uma string solta (ast.Expr
    sobre ast.Constant str) — pega modulo, classe, funcao e prosa solta no meio
    do corpo. String usada como VALOR (argumento, chave, comparacao) e codigo e
    fica.
    """
    prosa = set()
    for no in ast.walk(ast.parse(fonte)):
        if (isinstance(no, ast.Expr) and isinstance(no.value, ast.Constant)
                and isinstance(no.value.value, str)):
            prosa.update(range(no.value.lineno, no.value.end_lineno + 1))

    pedacos = []
    for tok in tokenize.generate_tokens(io.StringIO(fonte).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and tok.start[0] in prosa:
            continue
        pedacos.append(tok.string)
    return "\n".join(pedacos)


print(f"AUDIT-2026-08-WF2 — roteamento de jobs do CI ({len(TESTES)} arquivos)")

# Se o discriminador mudar de forma, tudo abaixo vira decorativo: a selecao do
# guard nao seria mais a mesma do CI, e ele passaria verde sobre nada.
workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
for _flag, _job in (("-L", "crm"), ("-l", "conversas")):
    check(f"grep {_flag} {MARCADOR} tests/test_*.py" in workflow,
          f"test.yml ainda seleciona o job `{_job}` com `grep {_flag}` sobre o marcador")

roteados = [c for c in TESTES if MARCADOR in c.read_text(encoding="utf-8")]
check(bool(roteados), "a selecao do job `conversas` nao esta vazia")

for caminho in roteados:
    check(MARCADOR in codigo_sem_prosa(caminho.read_text(encoding="utf-8")),
          f"{caminho.name}: usa o marcador como CODIGO, nao so em prosa")

# ── O gate de PostgreSQL do R-B nao pode virar inerte ─────────────────────
# `tests/test_v2_rb_cas_postgres.py` e o unico teste que FALHA (nao pula) sem
# banco, por exigencia do desenho BIA-V2 R-B (secoes 2.3 e 8). Isso so funciona
# com as DUAS metades juntas: excluido dos dois jobs comuns (senao quebra todo
# PR) E rodado por um job proprio que provisiona PostgreSQL. Apagar a segunda
# metade nao quebraria nada visivelmente — o arquivo simplesmente pararia de
# rodar, que e exatamente a "gate inerte" que o desenho proibe. Por isso o
# guard e aqui, e nao dentro do proprio arquivo (que nao rodaria para reclamar).
RB_CAS = "tests/test_v2_rb_cas_postgres.py"
check((ROOT / RB_CAS).exists(), f"{RB_CAS} existe")
check(r"grep -v '^tests/test_v2_rb_cas_postgres\.py$'" in workflow,
      "o job `crm` ainda EXCLUI o gate de PostgreSQL do R-B da sua selecao — "
      "sem a exclusao ele roda sem banco e derruba todo PR por configuracao")
check("rb-cas-postgres:" in workflow,
      "test.yml ainda declara o job `rb-cas-postgres` — sem ele o gate do R-B "
      "nao roda em lugar nenhum e a prova de concorrencia vira decorativa")
check("DATABASE_URL_RB_CAS_PROOF" in workflow,
      "o job `rb-cas-postgres` ainda passa DATABASE_URL_RB_CAS_PROOF — sem a "
      "env var o proprio teste falha, que e o comportamento desejado, mas o CI "
      "ficaria vermelho por configuracao em vez de por regressao")
check("postgres:16" in workflow,
      "o job `rb-cas-postgres` ainda provisiona um servico PostgreSQL")

# ── O E2E distribuido tem a MESMA estrutura de duas metades ────────────────
# `tests/test_v2_e2e_distribuido.py` casa o `grep -l` do job `conversas` (ele
# insere conversas/ no sys.path de verdade, como CODIGO), mas nao pode rodar la:
# ele sobe TAMBEM o app do CRM num processo proprio, e o ambiente do job
# `conversas` nao tem slowapi/passlib/bcrypt. E, como o gate do R-B, ele FALHA
# (nao pula) sem PostgreSQL. As duas metades precisam existir juntas: a exclusao
# no job `conversas` E o job proprio que provisiona banco e os DOIS virtualenvs.
E2E_DIST = "tests/test_v2_e2e_distribuido.py"
check((ROOT / E2E_DIST).exists(), f"{E2E_DIST} existe")
check(r"grep -v '^tests/test_v2_e2e_distribuido\.py$'" in workflow,
      "o job `conversas` ainda EXCLUI o E2E distribuido da sua selecao — sem a "
      "exclusao ele roda sem as dependencias do CRM e derruba todo PR")
check("e2e-distribuido:" in workflow,
      "test.yml ainda declara o job `e2e-distribuido` — sem ele o E2E distribuido "
      "nao roda em lugar nenhum e o fechamento de R-B/R15/R16/R17 fica sem prova "
      "fim-a-fim")
check("DATABASE_URL_E2E_DIST" in workflow,
      "o job `e2e-distribuido` ainda passa DATABASE_URL_E2E_DIST — sem a env var o "
      "proprio teste falha (comportamento desejado), mas o CI ficaria vermelho por "
      "configuracao em vez de por regressao")
for _var in ("E2E_DIST_PYTHON_CRM", "E2E_DIST_PYTHON_CONVERSAS"):
    check(_var in workflow,
          f"o job `e2e-distribuido` ainda passa {_var} — sem os dois interpretadores "
          f"separados um dos dois apps subiria com os pins do outro, que e exatamente "
          f"o que a separacao de ambientes deste repo existe para impedir")

# Mesma logica de inercia, um andar abaixo: o check 3e de test_v2_workers.py e
# a unica prova COMPORTAMENTAL de SKIP LOCKED (3a-3d rodam em SQLite, que
# descarta a clausula). Ele faz skip gracioso sem a env var, entao remover a
# variavel do workflow nao quebra nada de forma visivel — so desliga a prova.
check("DATABASE_URL_WORKERS_PROOF" in workflow,
      "o job `conversas` ainda liga a prova REAL de SKIP LOCKED "
      "(DATABASE_URL_WORKERS_PROOF) — sem ela o check 3e faz skip e a exclusao "
      "entre workers volta a ser provada so em SQLite, que ignora a clausula")

if falhas:
    print(f"\n{len(falhas)} FALHA(S) — roteamento de job do CI")
    print(
        "\nCausa: o arquivo casa o `grep -l` do job `conversas` SO por causa de\n"
        "comentario ou docstring. O grep nao le Python — a frase que descreve o\n"
        "discriminador dispara o discriminador. Consequencia: o arquivo roda no\n"
        "job 3.12 + conversas/requirements.txt, onde faltam slowapi, passlib,\n"
        "bcrypt, google-generativeai e outros sete; qualquer `import app.main`\n"
        "do CRM morre em ModuleNotFoundError.\n"
        "\nCorrecao: reescreva a prosa sem soletrar o marcador (ex.: \"o\n"
        "discriminador definido em .github/workflows/test.yml\"). Se o teste\n"
        "REALMENTE pertence ao Conversas, declare o marcador como CODIGO — e o\n"
        "que test_migration_m009.py e test_conversas_security.py fazem."
    )
    sys.exit(1)
print("\nROTEAMENTO DE JOBS OK")
