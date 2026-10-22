# -*- coding: utf-8 -*-
"""
CONV-HOTFIX-QUERY-COUNT - regressao de query count do read-repair do
responsavel: `_reparar_responsavel_tolerando_recusa` nao pode reintroduzir o
N+1 de `tags` que `selectinload(Conversation.tags)` existe para evitar.

CONTEXTO (ver o docstring de `_reparar_responsavel_tolerando_recusa` em
conversas/app/routers/conversations.py): cada reparo bem-sucedido faz o seu
PROPRIO `db.commit()` - decisao deliberada, ja provada isoladamente em
tests/test_conversas_repair_responsavel_hotfix.py ("cada reparo commita o
seu proprio", achado do teste de regressao daquele arquivo). `SessionLocal`
(conversas/app/database.py) usa o default `expire_on_commit=True`: SEM a
correcao, esse commit expirava TODOS os objetos da sessao, nao so a linha
reparada - inclusive a colecao `tags` de TODAS as OUTRAS conversas da mesma
pagina, carregada em lote pelo `selectinload(Conversation.tags)` de
`list_conversations` EXATAMENTE para evitar 1 SELECT de tags por linha. Numa
pagina com QUALQUER divergencia, isso reintroduzia o N+1 na hora de
serializar - o mesmo N+1 que aquele eager load existe para evitar.

O QUE ESTE ARQUIVO PROVA: rodando a MESMA pagina de 5 conversas tres vezes -
com 0, depois 2, depois 5 delas precisando de reparo - a contagem de queries
so cresce pelo custo IRREDUTIVEL de 1 UPDATE por reparo de fato aplicado.
Em particular, o numero de SELECTs que tocam `conversation_tag_links` (a
tabela de link do relationship `tags`) fica em UMA so query nas tres
passagens - nunca escala com quantas linhas divergiram (a prova mais direta
de que o N+1 continua eliminado).

ESCOPO: todas as conversas ficam FORA de WAITING_HUMAN/HUMAN_ACTIVE (`state`
"AI_TRIAGE"), onde a CHECK de m015 e inerte - o reparo aqui SEMPRE sucede,
isolando o caminho de SUCESSO (o commit sob teste). O caminho de RECUSA
(CHECK m015 + rollback) ja tem cobertura propria em
test_conversas_repair_responsavel_hotfix.py e nao e o alvo deste arquivo.
Banco proprio e isolado (nenhuma conversa remanescente de outro teste), para
que a contagem de queries nao seja poluida por estado alheio.

CONVERSAS_DIR presente de proposito (marcador de job do CI, ver
.github/workflows/test.yml, que separa os dois jobs por grep -l CONVERSAS_DIR).

Roda standalone:  python tests/test_conversas_repair_query_count.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_repair_query_count_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(CONVERSAS_DIR))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event, text  # noqa: E402

import app.main as main  # noqa: E402
from app.auth import get_current_user, User  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
from app.models.tag import ConversationTag  # noqa: E402

failures = []


def check(cond, msg):
    print(("  PASS: " if cond else "  FAIL: ") + msg)
    if not cond:
        failures.append(msg)


Base.metadata.create_all(bind=engine)

# Tabela CRM-shaped `leads`, MESMO padrao de
# test_conversas_repair_responsavel_hotfix.py. Sem ela, get_leads_responsaveis
# levanta, o except cai em db.rollback() (que TAMBEM expira a sessao inteira,
# por um motivo diferente do bug provado aqui) e a MEDICAO ficaria falseada.
with engine.begin() as _c:
    _c.execute(text(
        "CREATE TABLE leads (id INTEGER PRIMARY KEY, nome VARCHAR(200), "
        "whatsapp VARCHAR(30), responsavel_id INTEGER)"
    ))


def db_exec(sql, params=None):
    s = SessionLocal()
    try:
        s.execute(text(sql), params or {})
        s.commit()
    finally:
        s.close()


DONO = 7            # cache inicial de todas as 5 conversas
OUTRO = 8            # para quem o CRM reatribui 2 leads na passagem B
TERCEIRO = 9         # para quem o CRM reatribui TODOS os leads na passagem C
N = 5
LEAD_IDS = [401, 402, 403, 404, 405]

s = SessionLocal()
s.add_all([
    User(id=1, nome="Tester Local", email="tester@local", hashed_password="x", is_active=True),
    # Usuarios REAIS (nao so ids): get_leads_responsaveis faz LEFT JOIN users
    # ON u.id = l.responsavel_id AND u.is_active. Sem uma linha real aqui, o
    # nome do CRM viria sempre NULL e a passagem A (0 divergencias) teria uma
    # falsa divergencia so no NOME - a pre-condicao de "0 reparos" ficaria
    # invalida antes mesmo de medir a query.
    User(id=DONO, nome="Maria Vendas", email="maria@local", hashed_password="x", is_active=True),
    User(id=OUTRO, nome="Joao Backup", email="joao@local", hashed_password="x", is_active=True),
    User(id=TERCEIRO, nome="Ana Terceiro", email="ana@local", hashed_password="x", is_active=True),
])
tag = ConversationTag(id=1, nome="Quente", cor="#FF0000")
s.add(tag)
s.commit()

# 5 conversas FORA de WAITING_HUMAN/HUMAN_ACTIVE (AI_TRIAGE): a CHECK de m015
# e inerte aqui, entao o reparo NUNCA e recusado nesta suite - isola o
# caminho de SUCESSO (o commit sob teste), nao o de recusa.
convs = [
    Conversation(
        lead_id=lid, whatsapp=f"5511900{i:04d}", nome=f"Conversa {i}",
        status="aberta", unread_count=0, is_bot_active=True,
        responsavel_id=DONO, responsavel_nome="Maria Vendas",
        state="AI_TRIAGE", state_version=1,
    )
    for i, lid in enumerate(LEAD_IDS)
]
s.add_all(convs)
s.commit()
# tags em 2 das 5 (nao em todas): selectinload precisa cobrir os dois casos
# (com e sem tag) numa unica query em lote, nas tres passagens.
convs[0].tags.append(tag)
convs[2].tags.append(tag)
s.commit()
CIDS = [c.id for c in convs]
s.close()

# leads: todos comecam coerentes com o cache (DONO/"Maria Vendas") - 0
# divergencias na passagem A.
for lid in LEAD_IDS:
    db_exec(
        "INSERT INTO leads (id, nome, whatsapp, responsavel_id) VALUES "
        "(:i, 'Lead', '5511900000', :r)",
        {"i": lid, "r": DONO},
    )


class _DummyUser:
    id = 1
    email = "tester@local"
    is_admin = True


main.app.dependency_overrides[get_current_user] = lambda: _DummyUser()
client = TestClient(main.app)


def responsaveis_no_banco():
    s = SessionLocal()
    try:
        rows = (
            s.query(Conversation.id, Conversation.responsavel_id)
            .filter(Conversation.id.in_(CIDS))
            .all()
        )
        return {cid: rid for cid, rid in rows}
    finally:
        s.close()


def query_count():
    """Executa GET /api/conversations contando as queries emitidas pelo
    engine - mesmo padrao de tests/test_perf_query_count.py (`_contar_queries`)
    e da secao 9 de tests/test_conversas_inbox_filters.py (`query_count`):
    event.listen em before_cursor_execute, nunca medir tempo (fragil em CI)."""
    qs = []
    listener = lambda conn, cur, stmt, params, ctx, many: qs.append(stmt)  # noqa: E731
    event.listen(engine, "before_cursor_execute", listener)
    try:
        resp = client.get("/api/conversations?limit=50")
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert resp.status_code == 200, f"/api/conversations -> {resp.status_code}: {resp.text[:200]}"
    n_tags = len([q for q in qs if "conversation_tag_links" in q])
    n_updates = len([q for q in qs if "update conversations set" in q.lower()])
    return len(qs), n_tags, n_updates, resp.json()


print("CONV-HOTFIX-QUERY-COUNT - reparo do responsavel nao reintroduz N+1 de tags")

# ---------------------------------------------------------------------------
# Passagem A - 0 divergencias (cache ja bate com o CRM nas 5 linhas)
# ---------------------------------------------------------------------------
nA, tagsA, updA, bodyA = query_count()
check(len(bodyA["conversations"]) == N,
      f"pre-condicao: pagina tem {N} conversas (achou {len(bodyA['conversations'])})")
check(updA == 0, f"passagem A (0 divergentes): nenhum UPDATE de reparo emitido (achou {updA})")
check(responsaveis_no_banco() == {cid: DONO for cid in CIDS},
      "passagem A: nenhuma linha foi alterada (pre-condicao: nada precisava de reparo)")

# ---------------------------------------------------------------------------
# Passagem B - 2 das 5 divergem (CRM reatribuiu 2 leads para OUTRO)
# ---------------------------------------------------------------------------
for lid in LEAD_IDS[:2]:
    db_exec("UPDATE leads SET responsavel_id = :r WHERE id = :i", {"r": OUTRO, "i": lid})
nB, tagsB, updB, bodyB = query_count()
check(updB == 2, f"passagem B (2 divergentes): exatamente 2 UPDATEs de reparo (achou {updB})")
_resp_b = responsaveis_no_banco()
check(
    all(_resp_b[cid] == OUTRO for cid in CIDS[:2]) and all(_resp_b[cid] == DONO for cid in CIDS[2:]),
    f"passagem B: SO as 2 linhas divergentes foram reparadas ({_resp_b})",
)

# ---------------------------------------------------------------------------
# Passagem C - as 5 divergem (CRM reatribuiu TODOS os leads para TERCEIRO)
# ---------------------------------------------------------------------------
for lid in LEAD_IDS:
    db_exec("UPDATE leads SET responsavel_id = :r WHERE id = :i", {"r": TERCEIRO, "i": lid})
nC, tagsC, updC, bodyC = query_count()
check(updC == 5, f"passagem C (5 divergentes): exatamente 5 UPDATEs de reparo (achou {updC})")
check(all(rid == TERCEIRO for rid in responsaveis_no_banco().values()),
      "passagem C: as 5 linhas foram reparadas (todas convergem para TERCEIRO)")

# ---------------------------------------------------------------------------
# A PROVA: tags em UMA query em TODAS as passagens; custo extra e SO o UPDATE
# ---------------------------------------------------------------------------
print(f"  (bruto: nA={nA} tagsA={tagsA} updA={updA} | nB={nB} tagsB={tagsB} updB={updB} "
      f"| nC={nC} tagsC={tagsC} updC={updC})")
check(
    tagsA == 1 and tagsB == 1 and tagsC == 1,
    f"tags SEMPRE em uma unica query em lote, com 0, 2 ou 5 reparos "
    f"({tagsA}, {tagsB}, {tagsC}) - sem a correcao, o commit por reparo "
    f"reintroduz 1 SELECT de tags por linha da pagina (N+1)",
)
# custo total so pode crescer pelo UPDATE do proprio reparo (1 por linha
# reparada) - nunca por refresh em cascata das OUTRAS linhas da pagina.
# Folga de +1 sobre o minimo teorico (updB/updC) absorve qualquer overhead
# incidental que nao seja o N+1 investigado aqui.
check(nB - nA <= updB + 1,
      f"passagem B custa no maximo {updB + 1} queries a mais que A ({nA} -> {nB})")
check(nC - nA <= updC + 1,
      f"passagem C custa no maximo {updC + 1} queries a mais que A ({nA} -> {nC})")

print()
if failures:
    print(f"FALHAS: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("TODOS OS CHECKS PASSARAM")
