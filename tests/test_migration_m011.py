# -*- coding: utf-8 -*-
"""
AUDIT-2026-08 — executa a migration m011 DE VERDADE, num banco descartavel em
scratch/, nos dois cenarios que importam. Nenhum banco real e tocado: o script
so aceita rodar contra SQLite com --allow-sqlite, e o arquivo e criado e
destruido aqui dentro.

Rodar:  python tests/test_migration_m011.py

JOB DO CI: este arquivo cai no job `crm` (nao contem o literal CONVERSAS_DIR,
que e o discriminador em .github/workflows/test.yml). E deliberado, apesar de
ele montar o schema dos DOIS servicos: os unicos pacotes que
conversas/requirements.txt tem a mais sao `httpx` — que o job crm ja instala
explicitamente, para o TestClient — e `pydantic`, que vem como dependencia
transitiva do fastapi. Ou seja, o ambiente do job crm cobre os dois lados; o do
job conversas nao cobriria o do CRM (faltariam slowapi, passlib, bcrypt,
google-generativeai e outros sete).

Por que isto existe: `RELEASE_READINESS.md` afirma que a m011 "detecta
duplicatas e se recusa a continuar, sem apagar nada". Essa afirmacao vale zero
se vier de ter LIDO o script. A migration e a unica peca desta entrega feita
para rodar contra dado de producao, entao ela e a que menos pode ser aceita de
palavra.

Dois cenarios:
  A) banco LIMPO  -> os quatro indices unicos nascem, exit 0, e rodar de novo
                     continua exit 0 (idempotente).
  B) banco SUJO   -> com duas conversas do MESMO numero, tem que ABORTAR com
                     exit 2, listar os ids, NAO criar o indice e NAO apagar
                     linha nenhuma.
"""
import io
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
TMP = REPO / "scratch" / "prova_m011"
if TMP.exists():
    shutil.rmtree(TMP)
TMP.mkdir(parents=True)

falhas = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        falhas.append(msg)


def cria_banco(caminho, com_duplicata):
    """Sobe o schema real dos DOIS servicos e semeia dados."""
    env = dict(os.environ)
    env.update(ENVIRONMENT="development", SEED_INITIAL_ADMIN="false",
               SECRET_KEY="x", CONVERSAS_SEED_DEV_DATA="false",
               DATABASE_URL=f"sqlite:///{caminho.as_posix()}",
               PYTHONIOENCODING="utf-8")
    for cwd in (REPO, REPO / "conversas"):
        r = subprocess.run(
            [sys.executable, "-c",
             "import app.main; from app.database import Base, engine; "
             "Base.metadata.create_all(bind=engine); print('ok')"],
            cwd=str(cwd), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace")
        if r.returncode != 0:
            print(r.stdout[-800:], r.stderr[-800:])
            raise SystemExit(f"nao consegui montar o schema em {cwd}")

    con = sqlite3.connect(caminho)
    # PRODUCAO nao tem os indices: la o schema nasceu de um create_all() ANTERIOR
    # a esta auditoria, quando os Index(unique=True) ainda nao existiam nos
    # models. Reproduzir isso e o ponto do teste — com os indices ja criados por
    # create_all, o cenario "banco sujo" seria impossivel de montar e a prova
    # nao provaria nada.
    for (nome_idx,) in list(con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'uq_%'")):
        con.execute(f"DROP INDEX {nome_idx}")
    con.commit()
    con.execute("INSERT INTO conversations (lead_id, whatsapp, nome, status, unread_count, is_bot_active) "
                "VALUES (0, '5511900000001', 'A', 'aberta', 0, 0)")
    if com_duplicata:
        # exatamente o defeito que o indice existe para impedir
        con.execute("INSERT INTO conversations (lead_id, whatsapp, nome, status, unread_count, is_bot_active) "
                    "VALUES (0, '5511900000001', 'A duplicada', 'aberta', 0, 0)")
    con.commit()
    con.close()


def roda(caminho):
    env = dict(os.environ)
    env.update(ENVIRONMENT="development", PYTHONIOENCODING="utf-8",
               DATABASE_URL=f"sqlite:///{caminho.as_posix()}")
    return subprocess.run(
        [sys.executable, "migrations/m011_audit_unique_constraints.py", "--allow-sqlite"],
        cwd=str(REPO), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace")


def indices(caminho):
    con = sqlite3.connect(caminho)
    n = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'uq_%'")]
    con.close()
    return set(n)


def linhas(caminho, tabela):
    con = sqlite3.connect(caminho)
    n = con.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]
    con.close()
    return n


print("A) banco LIMPO — os indices nascem e a execucao e idempotente")
limpo = TMP / "limpo.db"
cria_banco(limpo, com_duplicata=False)
antes = indices(limpo)
check(antes == set(), "o banco parte SEM os indices unicos (como producao)")
r1 = roda(limpo)
check(r1.returncode == 0, f"exit 0 em banco limpo (veio {r1.returncode})")
depois = indices(limpo)
novos = depois - antes
check(len(novos) >= 4, f"criou os indices unicos ({sorted(novos)})")
check("uq_conversations_whatsapp" in depois, "uq_conversations_whatsapp presente")
r2 = roda(limpo)
check(r2.returncode == 0, f"segunda execucao tambem sai 0 (veio {r2.returncode})")
check(indices(limpo) == depois, "segunda execucao nao muda nada (idempotente)")

print()
print("B) banco SUJO — duas conversas com o MESMO numero")
sujo = TMP / "sujo.db"
cria_banco(sujo, com_duplicata=True)
antes_linhas = linhas(sujo, "conversations")
check(antes_linhas == 2, f"o banco comeca com a duplicata plantada ({antes_linhas} linhas)")
r3 = roda(sujo)
saida = (r3.stdout or "") + (r3.stderr or "")
check(r3.returncode == 2, f"exit 2 (duplicatas) — veio {r3.returncode}")
check("uq_conversations_whatsapp" not in indices(sujo),
      "NAO criou o indice unico sobre dado inconsistente")
check(linhas(sujo, "conversations") == antes_linhas,
      "NAO apagou nem deduplicou linha nenhuma")
check("5511900000001" in saida or "conversations" in saida,
      "a saida diz QUAL tabela/valor precisa de reconciliacao")

print()
if falhas:
    print(f"{len(falhas)} FALHA(S)")
    print("--- saida do cenario B ---")
    print(saida[-1500:])
    sys.exit(1)
print("OK: m011 cria os indices, e idempotente, e ABORTA sem destruir dado quando ha duplicata")
