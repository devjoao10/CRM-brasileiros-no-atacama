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
