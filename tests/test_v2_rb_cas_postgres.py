# -*- coding: utf-8 -*-
"""
BIA-V2 R-B — prova de CONCORRENCIA REAL do compare-and-swap contra PostgreSQL.

Teste do CRM (Python 3.11 + requirements.txt). NAO roda nos dois jobs comuns do
CI: tem job proprio, com um servico PostgreSQL, em `.github/workflows/test.yml`.

POR QUE ESTE ARQUIVO NAO PODE FAZER SKIP GRACIOSO
-------------------------------------------------
`tests/test_v2_r12_postgres_proof.py` e o check 3e de `tests/test_v2_workers.py`
sao provas OPCIONAIS: sem a env var eles saem 0 e a suite segue. Este arquivo e
o oposto, e a diferenca esta escrita no desenho (`bia-v2-r15-r17-design-v4.md`,
secoes 2.3 e 8):

  * O dialeto SQLite do SQLAlchemy DESCARTA `FOR UPDATE` em silencio — nao
    levanta erro nenhum. Um teste de concorrencia rodado em SQLite passa
    reportando verde sobre exatamente nada.
  * A forma ERRADA do idioma (`.with_for_update().first()` sem
    `populate_existing()`) tambem parece funcionar em qualquer cenario NAO
    concorrente. E isso que torna o bug facil de reintroduzir sem perceber.

Um gate que faz SKIP quando o Postgres falta seria, na pratica, um gate INERTE
— nunca rodaria no CI e nunca protegeria contra a regressao da secao 2.2. Por
isso, sem `DATABASE_URL_RB_CAS_PROOF` alcancavel, este arquivo FALHA (exit 1).
E o desenho quem exige: "o teste de aceitacao dos Casos 4/5 precisa FALHAR (nao
pular) quando PostgreSQL nao estiver disponivel, ou o CI precisa provisionar
PostgreSQL especificamente para esse job". O CI faz a segunda; rodando na mao,
suba o container abaixo.

O QUE ESTE ARQUIVO PROVA
------------------------
  1. Forma CORRETA (`_carregar_lead_sob_lock`, com `populate_existing()`): duas
     operacoes concorrentes no MESMO lead, `event_id` e `target_user_id`
     diferentes — EXATAMENTE UMA aplica; a perdedora recebe 409
     LEAD_ALREADY_ASSIGNED. (Casos 4/5 do desenho; cenario E2E I.)
  2. Forma SEM `populate_existing()` — o TESTE NEGATIVO OBRIGATORIO da secao
     2.3: rodada contra o MESMO cenario, ela FALHA, aplicando as duas operacoes
     e perdendo um update em silencio. Se um dia esta variante parar de falhar,
     o teste positivo do grupo 1 deixou de significar alguma coisa e este
     arquivo grita — nao passa em silencio.
  3. TRES PROCESSOS Python reais (nao threads: backends PostgreSQL distintos,
     engines e pools distintos), mesmo lead — exatamente um vencedor, dois 409.
     (Cenario E2E J.)

Os grupos 1 e 2 diferem em UMA unica variavel: se a leitura sob lock
re-hidrata o objeto do identity map. Tudo o mais — pre-carga, barreira, lead,
usuarios, payload, numero de rodadas — e identico, de proposito: e um
experimento controlado, nao duas medicoes parecidas.

COMO RODAR (Postgres efemero, descartavel)
------------------------------------------
    docker run -d --name bia_v2_rb_cas_proof -p 55434:5432 \
        -e POSTGRES_USER=proof -e POSTGRES_PASSWORD=proof \
        -e POSTGRES_DB=rbcasproof postgres:16-alpine

    # PowerShell
    $env:DATABASE_URL_RB_CAS_PROOF = "postgresql://proof:proof@localhost:55434/rbcasproof"
    python tests/test_v2_rb_cas_postgres.py

    # bash
    DATABASE_URL_RB_CAS_PROOF=postgresql://proof:proof@localhost:55434/rbcasproof \
        python tests/test_v2_rb_cas_postgres.py

    docker stop bia_v2_rb_cas_proof && docker rm bia_v2_rb_cas_proof

ATENCAO: o banco apontado por `DATABASE_URL_RB_CAS_PROOF` e tratado como
DESCARTAVEL — o schema `public` inteiro e DROPADO e recriado a cada execucao.
Nunca aponte esta var para o Postgres de dev compartilhado nem para producao.
"""
import contextlib
import json
import logging
import os
import pathlib
import subprocess
import sys
import threading
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)

PROOF_URL = os.getenv("DATABASE_URL_RB_CAS_PROOF", "").strip()

if not PROOF_URL:
    print(
        "FAIL tests/test_v2_rb_cas_postgres.py — `DATABASE_URL_RB_CAS_PROOF` ausente.\n"
        "     Este gate NAO faz skip gracioso, de proposito (ver o docstring do topo\n"
        "     e o desenho BIA-V2 R-B, secoes 2.3 e 8): em SQLite o SQLAlchemy DESCARTA\n"
        "     `FOR UPDATE` em silencio, entao um skip aqui viraria um gate inerte que\n"
        "     reporta verde sobre nada e deixa a regressao da secao 2.2 passar.\n"
        "     Suba o PostgreSQL efemero das instrucoes do docstring e rode de novo."
    )
    sys.exit(1)

if not PROOF_URL.startswith("postgresql"):
    print(
        f"FAIL DATABASE_URL_RB_CAS_PROOF precisa ser uma URL PostgreSQL (recebido: {PROOF_URL!r}).\n"
        "     R-B e especificamente sobre concorrencia com lock de linha real;\n"
        "     rodar isto em SQLite nao prova nada (e exatamente o ponto do risco)."
    )
    sys.exit(1)

# A engine do repo le DATABASE_URL no import — definimos ANTES de importar
# `app.database`, para que a engine sob prova seja a REAL, com os parametros
# reais de producao (pool, pool_pre_ping e o `isolation_level` READ COMMITTED
# que a secao 2.1.1 do desenho tornou explicito).
os.environ["DATABASE_URL"] = PROOF_URL
os.environ["ENVIRONMENT"] = "development"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["SEED_INITIAL_ADMIN"] = "false"
os.environ["GEMINI_API_KEY"] = ""

sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from app import config  # noqa: E402
from app.auth import hash_api_key  # noqa: E402
from app.database import IS_SQLITE, Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.lead import Lead  # noqa: E402
from app.models.lead_handoff_operation import LeadHandoffOperation  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402
from app.routers import v2_lead_domain as v2_mod  # noqa: E402

# 40 rodadas x 2 requisicoes rendem centenas de linhas de INFO que escondem os
# checks. Silencia so o ruido de transporte e o log de sucesso da rota — nada
# que este teste use como evidencia.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("app.routers.v2_lead_domain").setLevel(logging.WARNING)

failures = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        failures.append(msg)


# ── Bootstrap do schema descartavel ───────────────────────────────────────
# `DROP SCHEMA public CASCADE` em vez de `drop_all()`: nao depende da ordem de
# FK nem de o metadata conhecer tudo o que ficou de uma execucao anterior.
with engine.begin() as conn:
    conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
    conn.execute(text("CREATE SCHEMA public"))
Base.metadata.create_all(bind=engine)

with engine.connect() as _c:
    _pg_version = _c.execute(text("SHOW server_version")).scalar()
    _isolamento = _c.execute(text("SHOW transaction_isolation")).scalar()

print("=" * 72)
print("R-B — compare-and-swap sob concorrencia REAL (PostgreSQL)")
print("=" * 72)
print(f"PostgreSQL server_version : {_pg_version}")
print(f"transaction_isolation     : {_isolamento}")
print(f"URL                       : {PROOF_URL}")
print("-" * 72)

check(IS_SQLITE is False, "engine do repo esta em PostgreSQL (IS_SQLITE=False), nao SQLite")

# ── Prova COMPORTAMENTAL de isolation_level="READ COMMITTED" ─────────────────
# Achado da revisao de concorrencia: o `_isolamento` acima (so diagnostico) prova
# o AMBIENTE, nao a DECISAO de codigo — `SHOW transaction_isolation` reporta
# "read committed" COM ou SEM o kwarg em `app/database.py`, porque READ
# COMMITTED ja e o default do PROPRIO PostgreSQL. Se algem remover a linha
# `isolation_level="READ COMMITTED"` de `app/database.py` num refactor futuro,
# aquele check sozinho continuaria verde.
#
# Prova de verdade: muda o DEFAULT DO BANCO para 'repeatable read' e forca o
# engine do repo a abrir uma conexao NOVA (`dispose()` descarta o pool que o
# bootstrap do schema acima ja abriu). Se o kwarg realmente SOBREPOE o default
# do servidor — e nao so coincide com ele — a conexao nova ainda tem de
# reportar 'read committed'.
_dbname = engine.url.database
with engine.begin() as _conn:
    _conn.execute(text(
        f"ALTER DATABASE \"{_dbname}\" SET default_transaction_isolation='repeatable read'"
    ))

# Controle: uma engine SEM o kwarg, contra o MESMO banco, PRECISA ver o novo
# default numa conexao nova — senao o ALTER DATABASE nao surtiu efeito nenhum
# e o check seguinte passaria a toa, nao porque o kwarg funciona.
_engine_controle = create_engine(PROOF_URL, pool_pre_ping=True)
with _engine_controle.connect() as _c:
    _isolamento_controle = _c.execute(text("SHOW transaction_isolation")).scalar()
_engine_controle.dispose()
check(
    _isolamento_controle == "repeatable read",
    f"controle: engine SEM isolation_level ve o NOVO default do banco apos o "
    f"ALTER DATABASE (veio {_isolamento_controle!r}) — confirma que o ALTER "
    f"surtiu efeito e o check seguinte nao passa por coincidencia",
)

engine.dispose()
with engine.connect() as _c:
    _isolamento_pos_alter = _c.execute(text("SHOW transaction_isolation")).scalar()
check(
    _isolamento_pos_alter == "read committed",
    f"engine do repo (isolation_level='READ COMMITTED' em app/database.py) "
    f"SOBREPOE o novo default do banco numa conexao nova pos-ALTER DATABASE — "
    f"prova que o kwarg esta REALMENTE em vigor, nao so coincide com o default "
    f"do servidor (veio {_isolamento_pos_alter!r})",
)

# Restaura o default do banco: descartavel ou nao, nao deixa estado alterado
# vazar para o resto deste arquivo por acidente.
with engine.begin() as _conn:
    _conn.execute(text(f"ALTER DATABASE \"{_dbname}\" RESET default_transaction_isolation"))
engine.dispose()

API_KEY = "bna_chave-de-teste-do-rb-cas"
HEADERS = {"X-API-Key": API_KEY}

_db = SessionLocal()
_integrador = User(nome="Integrador", email="integrador@rbcas.local", hashed_password="x",
                   role=UserRole.USER, is_active=True, email_verified=True,
                   api_key=hash_api_key(API_KEY))
_ana = User(nome="Ana", email="ana@rbcas.local", hashed_password="x",
            role=UserRole.USER, is_active=True, email_verified=True)
_bruno = User(nome="Bruno", email="bruno@rbcas.local", hashed_password="x",
              role=UserRole.USER, is_active=True, email_verified=True)
_carla = User(nome="Carla", email="carla@rbcas.local", hashed_password="x",
              role=UserRole.USER, is_active=True, email_verified=True)
_db.add_all([_integrador, _ana, _bruno, _carla])
_db.commit()
INTEGRADOR_ID, ANA_ID, BRUNO_ID, CARLA_ID = (
    _integrador.id, _ana.id, _bruno.id, _carla.id,
)
_db.close()

# Mesmo padrao do teste SQLite irmao: sobrescreve PELO MODULO, porque
# `_conferir_chamador_autorizado` le o valor em tempo de chamada.
config.V2_LEAD_DOMAIN_SERVICE_USER_ID = INTEGRADOR_ID


def novo_lead(nome):
    db = SessionLocal()
    try:
        lead = Lead(nome=nome, whatsapp="+56000000001")
        db.add(lead)
        db.commit()
        return lead.id
    finally:
        db.close()


def dono_e_linhas(lead_id):
    """(responsavel_id atual, nº de linhas do livro-razao para este lead)."""
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        return (
            lead.responsavel_id if lead else None,
            db.query(LeadHandoffOperation)
              .filter(LeadHandoffOperation.lead_id == lead_id).count(),
        )
    finally:
        db.close()


# ── As duas formas do idioma de leitura sob lock ──────────────────────────
# A UNICA diferenca entre elas e `populate_existing()`. Pre-carga e barreira
# sao identicas nas duas, para que o experimento seja controlado.

_carregar_real = v2_mod._carregar_lead_sob_lock

# Registra, por leitura sob lock, se o objeto devolvido E o MESMO objeto Python
# da pre-carga. Se isto vier tudo False, o identity map esvaziou e o experimento
# do grupo 2 estaria medindo outra coisa — ver o docstring de `_com_barreira`.
_identidades = []


def _com_barreira(barreira, quebrada):
    """Fabrica o substituto de `_carregar_lead_sob_lock` para uma rodada.

    A pre-carga existe nas DUAS formas de proposito: ela e o que deixa o `Lead`
    no identity map do SQLAlchemy, que e a condicao sob a qual a forma sem
    `populate_existing()` devolve o objeto OBSOLETO mesmo tendo adquirido o
    lock de verdade. Provar a correcao COM o identity map sujo e a prova forte;
    provar sem ele nao provaria nada, porque nao haveria o que re-hidratar.

    A REFERENCIA da pre-carga precisa ficar VIVA ate depois da leitura sob lock,
    e isso nao e detalhe de estilo: o identity map guarda referencia FRACA. Um
    `db.query(...).first()` cujo resultado e descartado na hora e coletado pelo
    refcount do CPython antes da segunda query, o identity map esvazia, e a
    forma quebrada carrega valores FRESCOS — o experimento reportaria que a
    forma errada funciona. No codigo real pre-correcao a referencia ficava viva
    naturalmente, porque `_carregar_lead` DEVOLVIA o lead e o handler o
    guardava numa variavel local. Nao troque `precarga` por uma chamada solta.

    A barreira dispara DEPOIS da pre-carga e ANTES da leitura sob lock: e o que
    garante que os dois lados ja carregaram o snapshot obsoleto antes de
    disputar o lock, a mesma janela de colisao que a re-verificacao dedicada do
    desenho (secao 11.2) usou.
    """

    def _substituto(db, lead_id):
        precarga = db.query(Lead).filter(Lead.id == lead_id).first()
        barreira.wait(timeout=30)
        if quebrada:
            # A forma NATURAL e ERRADA: "so adicionar FOR UPDATE na query que ja
            # existe". O lock de linha E adquirido de verdade no PostgreSQL, mas
            # o SQLAlchemy devolve o objeto ja em memoria, com `responsavel_id`
            # obsoleto — e o compare-and-swap compara contra lixo.
            lead = (
                db.query(Lead)
                .filter(Lead.id == lead_id)
                .with_for_update()
                .first()
            )
            _identidades.append(lead is precarga)
            return lead
        # A forma real do modulo, sem copia: se `_carregar_lead_sob_lock` mudar,
        # este braco do experimento muda junto.
        lead = _carregar_real(db, lead_id)
        _identidades.append(lead is precarga)
        return lead

    return _substituto


@contextlib.contextmanager
def _variante_de_leitura(quebrada, barreira):
    """Troca `_carregar_lead_sob_lock` pela variante da rodada, e devolve depois."""
    v2_mod._carregar_lead_sob_lock = _com_barreira(barreira, quebrada)
    try:
        yield
    finally:
        v2_mod._carregar_lead_sob_lock = _carregar_real


def _disputar(lead_id, quebrada):
    """Duas requisicoes HTTP reais e concorrentes para o MESMO lead.

    Devolve (nº de aplicacoes com sucesso, lista de status, lista de tokens).
    """
    barreira = threading.Barrier(2)
    resultados = [None, None]
    alvos = (ANA_ID, BRUNO_ID)

    def _requisitar(indice):
        # Um TestClient por thread: cada `post` abre seu proprio portal anyio,
        # e nao ha estado compartilhado para embaralhar entre as duas.
        cliente = TestClient(app)
        resposta = cliente.post(
            "/api/v2/lead-domain/handoff",
            json={
                "event_id": str(uuid.uuid4()),
                "lead_id": lead_id,
                "target_user_id": alvos[indice],
                # `null` EXPLICITO: "espero o lead sem dono". As duas mandam a
                # MESMA expectativa — e o CAS que precisa reprovar a segunda.
                "expected_responsavel_id": None,
            },
            headers=HEADERS,
        )
        resultados[indice] = resposta

    with _variante_de_leitura(quebrada, barreira):
        threads = [threading.Thread(target=_requisitar, args=(i,)) for i in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

    status = [r.status_code if r is not None else None for r in resultados]
    aplicacoes = sum(
        1 for r in resultados
        if r is not None and r.status_code == 200 and r.json().get("replayed") is False
    )
    tokens = []
    for r in resultados:
        if r is not None and r.status_code == 409:
            corpo = r.json().get("detail")
            tokens.append(corpo.get("error") if isinstance(corpo, dict) else corpo)
    return aplicacoes, status, tokens


RODADAS = 20


# ══════════════════════════════════════════════════════════════════════════
# GRUPO 1 — forma CORRETA: exatamente uma vence (Casos 4/5, E2E I)
# ══════════════════════════════════════════════════════════════════════════

print(f"\n1. Forma CORRETA (com populate_existing) — {RODADAS} rodadas, 2 threads")
_identidades.clear()
_vencedores_ok = []
_tokens_ok = []
for _r in range(RODADAS):
    _lead = novo_lead(f"Lead CAS OK {_r}")
    _aplicacoes, _status, _tokens = _disputar(_lead, quebrada=False)
    _vencedores_ok.append(_aplicacoes)
    _tokens_ok.extend(_tokens)
    _dono, _linhas = dono_e_linhas(_lead)
    if _aplicacoes != 1 or _linhas != 1:
        print(f"     rodada {_r}: aplicacoes={_aplicacoes} status={_status} "
              f"dono={_dono} linhas_livro_razao={_linhas}")

check(
    all(v == 1 for v in _vencedores_ok),
    f"exatamente UMA operacao aplica em cada uma das {RODADAS} rodadas "
    f"(distribuicao observada: {sorted(set(_vencedores_ok))})",
)
check(
    _tokens_ok and all(t == "LEAD_ALREADY_ASSIGNED" for t in _tokens_ok),
    f"toda perdedora recebe 409 LEAD_ALREADY_ASSIGNED "
    f"(tokens observados: {sorted(set(_tokens_ok))})",
)
# Sem isto o grupo 1 seria a versao FRACA da afirmacao: "funciona quando nao ha
# nada obsoleto em memoria". A versao forte e "funciona MESMO com o identity map
# sujo", e e ela que o `populate_existing()` sustenta.
check(
    _identidades and all(_identidades),
    f"a leitura sob lock reencontrou o MESMO objeto Python da pre-carga em "
    f"todas as {len(_identidades)} leituras — o identity map estava sujo e "
    f"`populate_existing()` re-hidratou (identidades: {sorted(set(_identidades))})",
)


# ══════════════════════════════════════════════════════════════════════════
# GRUPO 2 — TESTE NEGATIVO OBRIGATORIO (secao 2.3 do desenho)
# ══════════════════════════════════════════════════════════════════════════
# Nao basta provar que a forma certa funciona: a forma errada tambem "funciona"
# em qualquer teste nao concorrente. Este grupo roda a variante SEM
# `populate_existing()` contra o MESMO cenario e exige que ela FALHE.

print(f"\n2. Forma SEM populate_existing — {RODADAS} rodadas, MESMO cenario")
_identidades.clear()
_vencedores_quebrado = []
for _r in range(RODADAS):
    _lead = novo_lead(f"Lead CAS QUEBRADO {_r}")
    _aplicacoes, _status, _tokens = _disputar(_lead, quebrada=True)
    _vencedores_quebrado.append(_aplicacoes)

_rodadas_perdendo_update = sum(1 for v in _vencedores_quebrado if v > 1)
print(f"     rodadas com update perdido: {_rodadas_perdendo_update}/{RODADAS} "
      f"(distribuicao: {sorted(set(_vencedores_quebrado))})")

check(
    _identidades and all(_identidades),
    f"a leitura sob lock devolveu o objeto OBSOLETO da pre-carga em todas as "
    f"{len(_identidades)} leituras — e este o mecanismo do bug, nao ausencia "
    f"de lock (identidades: {sorted(set(_identidades))})",
)
check(
    _rodadas_perdendo_update > 0,
    f"a forma SEM populate_existing FALHA sob concorrencia real — as duas "
    f"operacoes aplicam ({_rodadas_perdendo_update}/{RODADAS} rodadas). "
    f"Se este check falhar, o teste NEGATIVO perdeu o poder de deteccao e o "
    f"grupo 1 deixou de significar alguma coisa: investigue a montagem do "
    f"experimento ANTES de confiar no positivo",
)
check(
    _rodadas_perdendo_update >= RODADAS // 2,
    f"a falha da forma errada e sistematica, nao um acaso raro "
    f"({_rodadas_perdendo_update}/{RODADAS} rodadas; esperado >= {RODADAS // 2})",
)
check(
    all(v == 1 for v in _vencedores_ok) and _rodadas_perdendo_update > 0,
    "`populate_existing()` e a UNICA variavel que muda o desfecho — mesmo "
    "cenario, mesma pre-carga, mesma barreira, resultados opostos",
)


# ══════════════════════════════════════════════════════════════════════════
# GRUPO 3 — TRES PROCESSOS Python reais (cenario E2E J)
# ══════════════════════════════════════════════════════════════════════════
# Threads compartilham engine e pool. Processos separados tem engine propria,
# pool proprio e backend PostgreSQL proprio — e a unica forma de provar que a
# exclusao vem do lock de linha do banco, nao de qualquer serializacao acidental
# dentro do processo Python.

print("\n3. TRES PROCESSOS Python reais disputando o mesmo lead")

_WORKER = r'''
import json, os, sys, time
os.environ["DATABASE_URL"] = sys.argv[1]
os.environ["ENVIRONMENT"] = "development"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["SEED_INITIAL_ADMIN"] = "false"
os.environ["GEMINI_API_KEY"] = ""
sys.path.insert(0, sys.argv[2])

from fastapi.testclient import TestClient
from sqlalchemy import text
from app import config
from app.database import SessionLocal
from app.main import app

lead_id = int(sys.argv[3])
target_user_id = int(sys.argv[4])
event_id = sys.argv[5]
servico_id = int(sys.argv[6])
config.V2_LEAD_DOMAIN_SERVICE_USER_ID = servico_id

# Barreira ENTRE PROCESSOS pelo proprio banco: cada um anuncia presenca e espera
# os tres. Sem isto, o primeiro processo terminaria antes de o terceiro subir e
# nao haveria disputa nenhuma para observar.
db = SessionLocal()
db.execute(text("INSERT INTO rb_cas_barreira (quem) VALUES (:q)"), {"q": os.getpid()})
db.commit()
limite = time.time() + 60
while time.time() < limite:
    if db.execute(text("SELECT count(*) FROM rb_cas_barreira")).scalar() >= 3:
        break
    db.rollback()  # encerra a transacao para o proximo SELECT ver snapshot novo
    time.sleep(0.02)
db.close()

resposta = TestClient(app).post(
    "/api/v2/lead-domain/handoff",
    json={"event_id": event_id, "lead_id": lead_id,
          "target_user_id": target_user_id, "expected_responsavel_id": None},
    headers={"X-API-Key": "bna_chave-de-teste-do-rb-cas"},
)
corpo = {}
try:
    corpo = resposta.json()
except Exception:
    pass
print("RESULTADO " + json.dumps({"status": resposta.status_code, "corpo": corpo}))
'''

_worker_path = SCRATCH / "rb_cas_worker.py"
_worker_path.write_text(_WORKER, encoding="utf-8")

with engine.begin() as conn:
    conn.execute(text("DROP TABLE IF EXISTS rb_cas_barreira"))
    conn.execute(text("CREATE TABLE rb_cas_barreira (id SERIAL PRIMARY KEY, quem INTEGER)"))

_lead_proc = novo_lead("Lead CAS 3 Processos")
_processos = []
for _alvo in (ANA_ID, BRUNO_ID, CARLA_ID):
    _processos.append(subprocess.Popen(
        [sys.executable, str(_worker_path), PROOF_URL, str(ROOT),
         str(_lead_proc), str(_alvo), str(uuid.uuid4()), str(INTEGRADOR_ID)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ))

_saidas = []
try:
    for _p in _processos:
        _out, _err = _p.communicate(timeout=180)
        _linha = next((l for l in _out.splitlines() if l.startswith("RESULTADO ")), None)
        if _linha is None:
            print(f"     processo sem RESULTADO — stderr: {_err[-400:]}")
        _saidas.append(json.loads(_linha[len("RESULTADO "):]) if _linha else None)
except subprocess.TimeoutExpired:
    # Um _p.communicate(timeout=180) que estoura NAO mata o processo (doc do
    # stdlib): sem isto, o processo que travou e qualquer outro ainda vivo em
    # `_processos` ficariam orfaos ao repropagar a excecao.
    for _p in _processos:
        if _p.poll() is None:
            _p.kill()
            _p.wait()
    raise

check(
    all(s is not None for s in _saidas),
    f"os 3 processos responderam (saidas: {[s['status'] if s else None for s in _saidas]})",
)
_aplicaram = [s for s in _saidas if s and s["status"] == 200 and s["corpo"].get("replayed") is False]
_recusados = [s for s in _saidas if s and s["status"] == 409]
_dono_proc, _linhas_proc = dono_e_linhas(_lead_proc)

check(
    len(_aplicaram) == 1,
    f"EXATAMENTE UM dos 3 processos aplica (aplicaram: {len(_aplicaram)}, "
    f"status: {[s['status'] if s else None for s in _saidas]})",
)
check(
    len(_recusados) == 2,
    f"os outros 2 recebem 409 (recusados: {len(_recusados)})",
)
check(
    all(isinstance(s["corpo"].get("detail"), dict)
        and s["corpo"]["detail"].get("error") == "LEAD_ALREADY_ASSIGNED"
        for s in _recusados),
    "os 409 dos 3 processos trazem o token estruturado LEAD_ALREADY_ASSIGNED",
)
check(
    _linhas_proc == 1,
    f"o livro-razao tem UMA unica linha para o lead disputado (veio {_linhas_proc})",
)
check(
    _dono_proc == (_aplicaram[0]["corpo"]["target_user_id"] if _aplicaram else None),
    f"o dono final e o alvo do vencedor, nao um terceiro valor (dono={_dono_proc})",
)

# ── Resultado ─────────────────────────────────────────────────────────────

print("\n" + "=" * 72)
if failures:
    print(f"{len(failures)} CHECK(S) FALHARAM:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("R-B: CAS provado sob concorrencia real (2 threads, forma quebrada, 3 processos).")
sys.exit(0)
