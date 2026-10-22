"""
CONV-HOTFIX-LOCK-03 - `sync_responsavel_to_crm` trava a linha do lead (PostgreSQL).

POR QUE ESTE ARQUIVO EXISTE SEPARADO DA SUITE PADRAO
------------------------------------------------------
A correcao e um `FOR UPDATE` em SQL CRU, e o ramo que o emite so existe fora do
SQLite (`_TRAVA_LINHA_SQL` vira string vazia quando `IS_SQLITE`). A suite padrao
roda em SQLite, entao ela NAO exercita uma unica vez o SQL que vai para
producao: nem a sintaxe, nem o bloqueio. Um teste em SQLite aqui reportaria
verde sobre nada.

Mesmo padrao (e mesma justificativa) de `tests/test_v2_r12_postgres_proof.py`:
sem `DATABASE_URL_LOCK_PROOF` este arquivo faz SKIP GRACIOSO (exit 0), sem
falhar a suite padrao.

    docker run -d --name bia_v2_lock_proof -p 55441:5432 \\
        -e POSTGRES_USER=proof -e POSTGRES_PASSWORD=proof \\
        -e POSTGRES_DB=lockproof postgres:16-alpine

    PowerShell:
    $env:DATABASE_URL_LOCK_PROOF = "postgresql://proof:proof@localhost:55441/lockproof"
    python tests/test_conversas_sync_responsavel_lock_postgres.py

ATENCAO: o banco apontado por `DATABASE_URL_LOCK_PROOF` e tratado como
DESCARTAVEL - este arquivo cria e apaga tabelas nele. Nunca aponte para
producao.

O QUE E PROVADO
----------------
  1. O SQL emitido carrega mesmo `FOR UPDATE` fora do SQLite (e nao carrega
     dentro dele).
  2. SINTAXE: o statement executa no PostgreSQL real, sem erro.
  3. BLOQUEIO: uma segunda transacao chamando a MESMA funcao no MESMO lead fica
     BLOQUEADA ate a primeira commitar.
  4. O `lead_history` da segunda escrita parte do valor que a primeira gravou,
     e nao do anterior a ela.
  5. TESTE NEGATIVO com poder de deteccao: com a trava desligada, o mesmo
     cenario produz o historico ERRADO.

O QUE EXATAMENTE O `FOR UPDATE` PROTEGE AQUI - MEDIDO, NAO SUPOSTO
-------------------------------------------------------------------
Nao e a escrita da coluna: o PostgreSQL ja serializa `UPDATE` contra `UPDATE`
por conta propria, entao mesmo SEM `FOR UPDATE` a segunda transacao acaba
esperando - so que no `UPDATE`, e nao no `SELECT`. A essa altura ela JA LEU
`old_resp` obsoleto, e o `lead_history` que ela grava descreve uma transicao
que nunca existiu ("de 'Agente IA' para '22'" quando o dono ja era '11').

Ou seja: o que a falta da trava corrompe e o CHECK-THEN-ACT - a leitura que
decide o que registrar -, nao o valor final da coluna. Por isso o discriminador
deste teste e o CONTEUDO DO HISTORICO (checks 4 e 5), e nao o tempo de espera:
a espera acontece nos DOIS casos e nao distingue nada.

CONVERSAS_DIR presente de proposito (marcador de job do CI, ver
.github/workflows/test.yml, que separa os dois jobs por grep -l CONVERSAS_DIR).

Roda standalone:  python tests/test_conversas_sync_responsavel_lock_postgres.py
"""
import asyncio
import os
import pathlib
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"

PROOF_URL = os.getenv("DATABASE_URL_LOCK_PROOF", "").strip()
if not PROOF_URL:
    print(
        "SKIP tests/test_conversas_sync_responsavel_lock_postgres.py - prova de "
        "infraestrutura opcional.\n"
        "     `DATABASE_URL_LOCK_PROOF` nao esta definida, entao nao ha PostgreSQL\n"
        "     efemero para provar o FOR UPDATE contra. Isto NAO e uma falha: a suite\n"
        "     padrao roda em SQLite, onde este ramo de SQL nem e emitido.\n"
        "     Para rodar de verdade, ver as instrucoes no docstring do topo."
    )
    sys.exit(0)

if not PROOF_URL.startswith(("postgresql://", "postgresql+psycopg2://", "postgres://")):
    print(f"FAIL DATABASE_URL_LOCK_PROOF precisa ser uma URL PostgreSQL (recebido: {PROOF_URL!r}).")
    sys.exit(1)

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = PROOF_URL
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(CONVERSAS_DIR))

from sqlalchemy import text  # noqa: E402

from app.database import IS_SQLITE, SessionLocal, engine  # noqa: E402
from app.services import crm as crm_service  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


LEAD = 4242
DONO_A = 11
DONO_B = 22


def montar_schema():
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS lead_history"))
        conn.execute(text("DROP TABLE IF EXISTS leads"))
        conn.execute(text(
            "CREATE TABLE leads (id INTEGER PRIMARY KEY, nome VARCHAR(200), "
            "responsavel_id INTEGER)"
        ))
        conn.execute(text(
            "CREATE TABLE lead_history (id SERIAL PRIMARY KEY, lead_id INTEGER, "
            "evento VARCHAR(64), descricao TEXT, dados TEXT, created_at TIMESTAMP)"
        ))


def semear():
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM lead_history"))
        conn.execute(text("DELETE FROM leads"))
        conn.execute(text(
            "INSERT INTO leads (id, nome, responsavel_id) VALUES (:i, 'Lead Disputado', NULL)"
        ), {"i": LEAD})


montar_schema()

# ---------------------------------------------------------------------------
# 1-2. O SQL emitido, e a sintaxe contra o PostgreSQL real
# ---------------------------------------------------------------------------
print("\n--- 1-2: o statement e o esperado, e o PostgreSQL o aceita ---")

check(not IS_SQLITE, f"pre-condicao: este processo esta em PostgreSQL (IS_SQLITE={IS_SQLITE})")
check(
    crm_service._TRAVA_LINHA_SQL == " FOR UPDATE",
    f"1. fora do SQLite o sufixo de trava e ' FOR UPDATE' (obteve "
    f"{crm_service._TRAVA_LINHA_SQL!r})",
)

semear()
s = SessionLocal()
try:
    linha = s.execute(
        text(f"SELECT responsavel_id FROM leads WHERE id = :lid{crm_service._TRAVA_LINHA_SQL}"),
        {"lid": LEAD},
    ).fetchone()
    _ok_sintaxe = linha is not None
    s.rollback()
finally:
    s.close()
check(_ok_sintaxe, "2. o SELECT ... FOR UPDATE executa no PostgreSQL real (sintaxe valida)")


# ---------------------------------------------------------------------------
# 3-4. O BLOQUEIO, com duas transacoes de verdade
# ---------------------------------------------------------------------------
def cenario(com_trava: bool):
    """
    Duas transacoes chamam `sync_responsavel_to_crm` no MESMO lead.

    A ordem e forcada: B so COMECA depois de A ter feito a sua leitura+UPDATE
    (e antes do commit de A). Com a trava, B fica parado no SELECT ate A
    commitar; sem ela, B le o valor obsoleto e segue.
    """
    semear()
    original = crm_service._TRAVA_LINHA_SQL
    crm_service._TRAVA_LINHA_SQL = original if com_trava else ""

    a_escreveu = threading.Event()
    b_terminou = threading.Event()
    resultado = {}

    def tarefa_b():
        db_b = SessionLocal()
        try:
            a_escreveu.wait(10)
            inicio = time.monotonic()
            asyncio.run(crm_service.sync_responsavel_to_crm(LEAD, DONO_B, db_b))
            resultado["espera_b"] = time.monotonic() - inicio
            db_b.commit()
        finally:
            db_b.close()
            b_terminou.set()

    t = threading.Thread(target=tarefa_b, daemon=True)
    t.start()

    db_a = SessionLocal()
    try:
        asyncio.run(crm_service.sync_responsavel_to_crm(LEAD, DONO_A, db_a))
        a_escreveu.set()
        # B esta bloqueado (ou nao) neste intervalo. 1.5s e folgado o bastante
        # para distinguir "esperou pela trava" de "passou direto".
        time.sleep(1.5)
        resultado["b_terminou_antes_do_commit_de_a"] = b_terminou.is_set()
        db_a.commit()
    finally:
        db_a.close()

    b_terminou.wait(20)
    t.join(20)
    crm_service._TRAVA_LINHA_SQL = original

    with engine.begin() as conn:
        resultado["dono_final"] = conn.execute(
            text("SELECT responsavel_id FROM leads WHERE id = :i"), {"i": LEAD}
        ).scalar()
        resultado["historico"] = [
            r[0] for r in conn.execute(
                text("SELECT descricao FROM lead_history WHERE lead_id = :i ORDER BY id"),
                {"i": LEAD},
            ).fetchall()
        ]
    return resultado


print("\n--- 3-4: COM a trava (o comportamento corrigido) ---")
com = cenario(com_trava=True)
check(
    com["b_terminou_antes_do_commit_de_a"] is False,
    f"3. a 2a transacao fica BLOQUEADA ate a 1a commitar "
    f"(terminou antes do commit? {com['b_terminou_antes_do_commit_de_a']}; "
    f"espera medida: {com.get('espera_b', 0):.2f}s). NOTA: sozinho isto NAO "
    f"discrimina - ver 'O QUE EXATAMENTE O FOR UPDATE PROTEGE' no topo; o "
    f"discriminador e o check 5",
)
check(
    com["dono_final"] == DONO_B,
    f"3b. e depois de liberada ela aplica normalmente (dono final={com['dono_final']})",
)
_hist_b = [h for h in com["historico"] if f"para '{DONO_B}'" in h]
check(
    len(_hist_b) == 1 and f"de '{DONO_A}'" in _hist_b[0],
    f"4. o historico da 2a escrita parte do valor que a 1a gravou "
    f"('de {DONO_A} para {DONO_B}'), nao do estado anterior a ela "
    f"({com['historico']})",
)

# ---------------------------------------------------------------------------
# 5. TESTE NEGATIVO - sem a trava, a corrida acontece de verdade
# ---------------------------------------------------------------------------
print("\n--- 5: SEM a trava (poder de deteccao do teste) ---")
sem = cenario(com_trava=False)
_hist_b_sem = [h for h in sem["historico"] if f"para '{DONO_B}'" in h]
check(
    len(_hist_b_sem) == 1 and "de 'Agente IA'" in _hist_b_sem[0],
    f"5. sem FOR UPDATE o historico da 2a escrita MENTE: registra a transicao "
    f"partindo do dono ANTERIOR a 1a escrita, que ela nunca chegou a ver "
    f"({sem['historico']}) - o `SELECT` sem trava leu `old_resp` obsoleto",
)
check(
    sem["dono_final"] == DONO_B,
    f"5b. (e o VALOR final da coluna esta certo mesmo sem a trava: quem o "
    f"protege e o proprio lock de UPDATE do PostgreSQL, nao esta correcao - "
    f"dono={sem['dono_final']})",
)
check(
    com["historico"] != sem["historico"],
    f"5c. PODER DE DETECCAO: a trava e a UNICA variavel que muda, e os dois "
    f"cenarios produzem historicos DIFERENTES. Se este check parar de falhar "
    f"com a trava desligada, o teste virou decorativo e 3/4 deixaram de "
    f"significar alguma coisa. COM={com['historico']} SEM={sem['historico']}",
)

with engine.begin() as conn:
    conn.execute(text("DROP TABLE IF EXISTS lead_history"))
    conn.execute(text("DROP TABLE IF EXISTS leads"))

print()
if failures:
    print(f"FALHAS: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("CONV-HOTFIX-LOCK-03: FOR UPDATE provado contra PostgreSQL real.")
