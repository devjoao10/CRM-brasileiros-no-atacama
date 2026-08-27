# -*- coding: utf-8 -*-
"""
AUDIT-2026-08 — executa a migration m011 DE VERDADE, num banco descartavel em
scratch/, nos dois cenarios que importam. Nenhum banco real e tocado: o script
so aceita rodar contra SQLite com --allow-sqlite, e o arquivo e criado e
destruido aqui dentro.

Rodar:  python tests/test_migration_m011.py

JOB DO CI: este arquivo cai no job `crm` — nao casa o discriminador definido em
.github/workflows/test.yml. AUDIT-2026-08-WF2: esta frase SOLETRAVA o marcador
para explica-lo, e o grep nao le prosa — a explicacao roteava o arquivo para o
job `conversas`, onde `import app.main` do CRM morre em ModuleNotFoundError.
tests/test_ci_job_routing_guard.py agora barra a recaida. O job `crm` e
deliberado, apesar de este teste montar o schema dos DOIS servicos: os unicos
pacotes que conversas/requirements.txt tem a mais sao `httpx` — que o job crm
ja instala explicitamente, para o TestClient — e `pydantic`, que vem como
dependencia transitiva do fastapi. Ou seja, o ambiente do job crm cobre os dois
lados; o do job conversas nao cobriria o do CRM (faltariam slowapi, passlib,
bcrypt, google-generativeai e outros sete).

Por que isto existe: `RELEASE_READINESS.md` afirma que a m011 "detecta
duplicatas e se recusa a continuar, sem apagar nada". Essa afirmacao vale zero
se vier de ter LIDO o script. A migration e a unica peca desta entrega feita
para rodar contra dado de producao, entao ela e a que menos pode ser aceita de
palavra.

Quatro cenarios:
  A) banco LIMPO  -> os quatro indices unicos nascem, exit 0, e rodar de novo
                     continua exit 0 (idempotente).
  B) banco SUJO   -> com duas conversas do MESMO numero, tem que ABORTAR com
                     exit 2, listar os ids, NAO criar o indice e NAO apagar
                     linha nenhuma.
  C) banco SUJO em DUAS tabelas (AUDIT-2026-08-WF2) -> o abort por dado nao
                     pode arrastar junto o que nao depende de dado: o F5 e os
                     indices das tabelas limpas continuam sendo aplicados, e o
                     relatorio lista as DUAS tabelas sujas numa rodada so.
  D) alvo ERRADO (AUDIT-2026-08-WF2) -> banco sem nenhuma das tabelas nao e
                     "NO-OP": e RECUSA, exit != 0, sem imprimir OK.
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


def cria_banco(caminho, com_duplicata, dupe_funnel=False):
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
    if dupe_funnel:
        # AUDIT-2026-08-WF2 — segunda tabela suja, para provar que o relatorio
        # nao para na primeira: o operador precisa das DUAS numa rodada so.
        con.execute("INSERT INTO funnel_entries (id, lead_id, funnel_id, etapa_id, posicao) "
                    "VALUES (41, 7, 3, 'a', 0)")
        con.execute("INSERT INTO funnel_entries (id, lead_id, funnel_id, etapa_id, posicao) "
                    "VALUES (42, 7, 3, 'b', 1)")
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
print("C) AUDIT-2026-08-WF2 — o abort por dado NAO bloqueia o que independe de dado")
# Antes: `run()` levantava no PRIMEIRO objeto sujo, entao (a) o F5, que e puro
# DDL e nao le uma linha sequer, ficava sem aplicar por causa de uma duplicata
# que nao tem nada a ver com ele, e (b) os indices das tabelas LIMPAS tambem
# morriam junto. Uma duplicata em funnel_entries deixava producao sem os
# DEFAULT do F5 — ou seja, com todo INSERT vindo de psql/n8n/COPY ainda sendo
# rejeitado — e sem os indices de F3/F4.
duplo = TMP / "duplo.db"
cria_banco(duplo, com_duplicata=True, dupe_funnel=True)
r4 = roda(duplo)
saida_c = (r4.stdout or "") + (r4.stderr or "")
check(r4.returncode == 2, f"exit 2 (ha duplicata) — veio {r4.returncode}")
check("F5 server-defaults" in saida_c,
      "F5 (puro DDL) e ALCANCADO mesmo com duplicata em outra tabela")
idx_c = indices(duplo)
check("uq_operational_card_assignees_card_user" in idx_c,
      "F3: indice da tabela LIMPA e criado apesar do abort em outra tabela")
check("uq_operational_card_field_values_card_definition" in idx_c,
      "F4: indice da tabela LIMPA e criado apesar do abort em outra tabela")
check("uq_conversations_whatsapp" not in idx_c and "uq_funnel_entries_lead_funnel" not in idx_c,
      "nenhum indice criado sobre tabela suja")
check("conversations" in saida_c and "funnel_entries" in saida_c,
      "o relatorio lista AS DUAS tabelas sujas numa rodada so")
check(linhas(duplo, "conversations") == 2 and linhas(duplo, "funnel_entries") == 2,
      "continua sem apagar nada")
check("OK" not in saida_c, "nao imprime OK depois de abortar")

print()
print("D) AUDIT-2026-08-WF2 — alvo sem NENHUMA das tabelas: RECUSA, nao 'NO-OP'")
# Um DATABASE_URL apontado para base recem-provisionada / nome errado / replica
# vazia fazia a m011 imprimir "OK — NO-OP (ja estava tudo aplicado)" e sair 0,
# afirmando um estado que ela nunca verificou.
vazio = TMP / "vazio.db"
sqlite3.connect(vazio).close()
r5 = roda(vazio)
saida_d = (r5.stdout or "") + (r5.stderr or "")
check(r5.returncode != 0, f"exit != 0 num alvo sem as tabelas (veio {r5.returncode})")
check("RECUSADO" in saida_d, "diz RECUSADO em vez de fingir NO-OP")
check("OK" not in saida_d, "nao imprime OK sobre um estado que nao verificou")

print()
if falhas:
    print(f"{len(falhas)} FALHA(S)")
    print("--- saida do cenario B ---")
    print(saida[-1500:])
    sys.exit(1)
print("OK: m011 cria os indices, e idempotente, e ABORTA sem destruir dado quando ha duplicata")
