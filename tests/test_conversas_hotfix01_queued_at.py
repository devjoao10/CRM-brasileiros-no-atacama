"""
HOTFIX-01 — preservar queued_at na 1a resposta humana.

Contexto (ver conversas/app/services/atendimento.py e migrations/m008, m012):
`Conversation.queued_at` marca o instante em que a conversa ENTROU na fila de
atendimento humano (estado WAITING_HUMAN). Ate esta correcao, DOIS pontos
zeravam essa coluna assim que `primeira_resposta_humana_at` deixava de ser
NULL:

  - `marcar_atendimento_humano` (chamada por `record_outbound_message` na 1a
    resposta humana bem-sucedida) zerava queued_at na MESMA escrita que grava
    primeira_resposta_humana_at.
  - `aplicar_estado_humano` (usada por claim/assign/release/handoff/PUT) zerava
    queued_at de novo em QUALQUER operacao de fila rodada DEPOIS da 1a
    resposta, sempre que primeira_resposta_humana_at ja nao era NULL.

Isso apagava para sempre o instante de entrada na fila assim que ele passava a
ter valor de metrica: "tempo em fila" e "tempo ate a 1a resposta" (ambos
calculados como primeira_resposta_humana_at - queued_at) ficavam
incalculaveis. Tambem contradizia `conversas/app/v2/estados.py`, que ja EXIGE
queued_at IS NOT NULL para os estados WAITING_HUMAN e HUMAN_ACTIVE — a mesma
regra do CHECK `ck_conversations_estado_humano` (migration m015): uma conversa
V2 recebendo a 1a resposta humana antes de transicionar para HUMAN_ACTIVE
violava esse CHECK no commit.

HOTFIX-01 (pre-autorizada pelo dono do projeto): as duas funcoes acima passam
a PRESERVAR queued_at a partir da 1a resposta humana. Only o release
(`resetar_atendimento=True`) continua zerando-o de proposito, ao devolver a
conversa para o FIM da fila.

Prova que:
  1. queued_at e carimbado ao entrar na fila (handoff BIA->humano).
  2. a 1a resposta humana (marcar_atendimento_humano) NAO apaga queued_at.
  3. primeira_resposta_humana_at e gravado corretamente na 1a resposta humana.
  4. uma operacao POSTERIOR (aplicar_estado_humano, via /assign) tambem
     preserva queued_at depois que a conversa ja foi atendida.
  5. os dois timestamps sobrevivem juntos e em ordem correta (queued_at <=
     primeira_resposta_humana_at) — "tempo em fila" e "tempo ate a 1a
     resposta" continuam calculaveis depois do fluxo.
  6. conversas LEGACY (state=NULL/'LEGACY', pre-V2) tem o MESMO
     comportamento — o fix nao e V2-specific. Reforcado por um cenario
     explicito em estado V2 WAITING_HUMAN: a 1a resposta humana nao viola o
     CHECK `ck_conversations_estado_humano` (a regressao seria um 500/
     IntegrityError, nao so um valor errado).
  7. (unidade) as duas funcoes de app/services/atendimento.py, isoladas — sem
     HTTP, sem banco — confirmam 1-6 diretamente sobre o objeto Python.

CONVERSAS_DIR presente de proposito — discriminador de job do CI (ver
.github/workflows/test.yml).

Roda standalone:  python tests/test_conversas_hotfix01_queued_at.py
"""
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_hotfix01_queued_at_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"
# Deterministico: handoff sempre resolve para o usuario 1 (unico elegivel).
os.environ["ATENDENTES_ELEGIVEIS"] = "1"

sys.path.insert(0, str(CONVERSAS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.auth import get_current_user, User  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
from app.services.atendimento import (  # noqa: E402
    aplicar_estado_humano,
    marcar_atendimento_humano,
)

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


Base.metadata.create_all(bind=engine)

# Usuarios reais (assign/handoff validam is_active na tabela compartilhada).
_db = SessionLocal()
for uid, nome, email in ((1, "Julia", "julia@local"), (2, "Joao", "joao@local")):
    if not _db.query(User).filter(User.id == uid).first():
        _db.add(User(id=uid, nome=nome, email=email,
                     hashed_password="x", role="user", is_active=True))
_db.commit()
_db.close()


class _U1:
    id = 1
    nome = "Julia"
    email = "julia@local"
    role = "user"
    is_active = True


main.app.dependency_overrides[get_current_user] = lambda: _U1()
client = TestClient(main.app)


def state(conv_id):
    """Le o estado operacional direto do banco (nao confia na serializacao)."""
    db = SessionLocal()
    try:
        c = db.query(Conversation).filter(Conversation.id == conv_id).first()
        return (c.is_bot_active, c.atendente_id, c.queued_at,
                c.primeira_resposta_humana_at, c.status, c.state)
    finally:
        db.close()


# ============================================================================
# CENARIO 1 — fluxo completo: fila -> 1a resposta -> operacao posterior
# ============================================================================
print("1 — conversa nasce em BIA (fora da fila), LEGACY por default de schema")
db = SessionLocal()
conv1 = Conversation(
    lead_id=1, whatsapp="5511900088001", status="aberta", unread_count=0,
    is_bot_active=True, last_customer_msg_at=datetime.now(timezone.utc),
)
db.add(conv1)
db.commit()
cid1 = conv1.id
db.close()

bot, at, q, prh, st, v2state = state(cid1)
check(bot is True and at is None and q is None and prh is None,
      "pre-condicao: conversa nasce fora da fila (bot=True, sem dono, sem queued/prh)")
check(v2state == "LEGACY",
      "pre-condicao P6: state=LEGACY por server_default — nunca tocou o pipeline V2")

print("2 — handoff BIA->humano: carimba queued_at (entra na fila)")
r = client.post(f"/api/conversations/{cid1}/handoff")
check(r.status_code == 200, f"handoff responde 200 (got {r.status_code})")
_, at_h, q_h, prh_h, _, _ = state(cid1)
check(q_h is not None, "P1: queued_at carimbado ao entrar na fila (handoff)")
check(prh_h is None, "pre-condicao: ainda sem resposta humana")
check(at_h == 1, "pre-condicao: handoff resolveu o atendente elegivel (id=1)")

print("3 — 1a resposta humana: prh gravado, queued_at PRESERVADO")
r = client.post(f"/api/conversations/{cid1}/messages",
                json={"content": "Oi! Ja estou vendo seu caso."})
check(r.status_code == 200, f"1o envio humano responde 200 (got {r.status_code})")
bot2, at2, q2, prh2, _, _ = state(cid1)
check(prh2 is not None, "P3: primeira_resposta_humana_at gravado na 1a resposta humana")
check(q2 == q_h, "P2 (HOTFIX-01): a 1a resposta humana NAO apaga/reseta queued_at")
check(bot2 is False and at2 == 1, "pre-condicao: bot desligado, mesmo atendente")

print("4 — operacao POSTERIOR (assign) apos a 1a resposta: queued_at continua preservado")
r = client.post(f"/api/conversations/{cid1}/assign", json={"user_id": 2})
check(r.status_code == 200, f"assign responde 200 (got {r.status_code})")
_, at3, q3, prh3, _, _ = state(cid1)
check(at3 == 2, "pre-condicao: assign realmente trocou o dono")
check(q3 == q_h, "P4 (HOTFIX-01): aplicar_estado_humano, chamada DEPOIS da 1a resposta "
                  "(via /assign), tambem preserva queued_at")
check(prh3 == prh2, "pre-condicao: assign nao mexe em primeira_resposta_humana_at")

print("5 — os dois timestamps sobrevivem juntos e em ordem correta")
check(q3 is not None and prh3 is not None and prh3 >= q3,
      "P5: queued_at e primeira_resposta_humana_at presentes e em ordem -> "
      "'tempo em fila' e 'tempo ate a 1a resposta' continuam calculaveis")

print("6 — a conversa inteira nunca saiu de LEGACY (fix nao e V2-specific)")
check(state(cid1)[5] == "LEGACY",
      "P6: state continua LEGACY do inicio ao fim do fluxo — nada aqui depende da V2")


# ============================================================================
# CENARIO 2 — conversa V2 em WAITING_HUMAN: a regressao seria um CRASH, nao so
# um valor errado. app/v2/estados.py EXIGE queued_at IS NOT NULL para
# WAITING_HUMAN/HUMAN_ACTIVE (mesma regra do CHECK ck_conversations_estado_humano,
# migration m015). Com o bug antigo, marcar_atendimento_humano zerava queued_at
# na 1a resposta SEM tocar `state` — violando o CHECK no commit.
# ============================================================================
print("7 — fixture V2 em WAITING_HUMAN satisfaz o CHECK ck_conversations_estado_humano")
db = SessionLocal()
queued_v2 = datetime.now(timezone.utc) - timedelta(minutes=7)
conv2 = Conversation(
    lead_id=2, whatsapp="5511900088002", status="aberta", unread_count=0,
    is_bot_active=False, atendente_id=1, responsavel_id=1,
    queued_at=queued_v2, primeira_resposta_humana_at=None,
    last_customer_msg_at=datetime.now(timezone.utc),
    state="WAITING_HUMAN", state_version=1,
)
db.add(conv2)
db.commit()  # levantaria IntegrityError aqui mesmo se a fixture nao satisfizesse o CHECK
cid2 = conv2.id
db.close()
antes2 = state(cid2)
check(antes2[2] is not None and antes2[5] == "WAITING_HUMAN",
      "pre-condicao: fixture nasce em WAITING_HUMAN com queued_at preenchido")

print("8 — 1a resposta humana em WAITING_HUMAN NAO viola o CHECK do banco")
r = client.post(f"/api/conversations/{cid2}/messages",
                json={"content": "Oi! Ja estou vendo seu caso."})
check(r.status_code == 200,
      f"P2/P6 (regressao do CHECK ck_conversations_estado_humano): 1a resposta humana "
      f"em conversa V2 WAITING_HUMAN nao levanta IntegrityError (got {r.status_code})")
depois2 = state(cid2)
check(depois2[2] == antes2[2], "queued_at continua intacto (mesmo timestamp da fixture)")
check(depois2[3] is not None, "primeira_resposta_humana_at gravado")
# ATUALIZADO PELA BARREIRA R8/R10 (`conversas/app/v2/compat.py`).
#
# Ate a barreira existir, esta linha afirmava que a chamada NAO mexia em `state`
# — e era verdade: nenhum writer V1 escreve as colunas da V2, e nada mais
# observava a escrita. Agora um listener `before_flush` observa, e uma 1a
# resposta humana numa conversa `WAITING_HUMAN` e exatamente a transicao
# `WAITING_HUMAN -> HUMAN_ACTIVE`: aresta legal do grafo, e a UNICA diferenca
# entre os dois estados na tabela de invariantes de `estados.py` e justamente
# `primeira_resposta_humana_at`, que esta chamada acabou de gravar.
#
# A afirmacao fica mais FORTE, nao mais fraca: antes so se sabia que o estado
# nao andava; agora se sabe que ele anda para o lugar CERTO, no MESMO commit da
# escrita operacional. O que o HOTFIX-01 guarda (queued_at preservado) continua
# provado pelas duas checagens acima, intactas.
check(depois2[5] == "HUMAN_ACTIVE",
      f"R8/R10 (barreira): a 1a resposta humana pela V1 leva a conversa de "
      f"WAITING_HUMAN para HUMAN_ACTIVE no MESMO commit — pela rota HTTP real, "
      f"nao so em teste de unidade (obteve {depois2[5]!r})")


# ============================================================================
# UNIDADE — aplicar_estado_humano / marcar_atendimento_humano isoladas, sem
# HTTP nem banco (objeto Python simples, nunca commitado).
# ============================================================================
print("9 — unidade: aplicar_estado_humano carimba queued_at ao entrar na fila")
c = Conversation(status="aberta", is_bot_active=True, state="LEGACY")
aplicar_estado_humano(c, 42)
check(c.queued_at is not None and c.atendente_id == 42 and c.is_bot_active is False,
      "U1: aplicar_estado_humano entra na fila (dono e bot atualizados)")
antigo = c.queued_at

print("10 — unidade: marcar_atendimento_humano preserva queued_at na 1a resposta")
ok = marcar_atendimento_humano(c, 42)
check(ok is True and c.primeira_resposta_humana_at is not None,
      "U2: marcar_atendimento_humano registra a 1a resposta (devolve True)")
check(c.queued_at == antigo,
      "U2 (HOTFIX-01): marcar_atendimento_humano NAO altera queued_at")

print("11 — unidade: aplicar_estado_humano depois da 1a resposta continua preservando")
aplicar_estado_humano(c, 7)  # reassign pos-resposta (ex.: /assign)
check(c.atendente_id == 7, "U3: reassign muda o dono")
check(c.queued_at == antigo,
      "U3 (HOTFIX-01): aplicar_estado_humano, chamada DEPOIS da 1a resposta, "
      "tambem preserva queued_at")
check(c.primeira_resposta_humana_at is not None,
      "U3: aplicar_estado_humano nao mexe em primeira_resposta_humana_at")

print("12 — unidade: os dois timestamps sobrevivem juntos e em ordem")
check(c.queued_at is not None and c.primeira_resposta_humana_at is not None
      and c.primeira_resposta_humana_at >= c.queued_at,
      "U4 (P5): os dois timestamps presentes e em ordem correta")

print("13 — unidade: 2a resposta humana e no-op (idempotencia inalterada)")
ok2 = marcar_atendimento_humano(c, 7)
check(ok2 is False, "U5: marcar_atendimento_humano so conta a PRIMEIRA resposta")
check(c.queued_at == antigo, "U5: 2a chamada tambem nao mexe em queued_at (nem precisaria)")

print("14 — unidade: release (resetar_atendimento) continua zerando queued_at de proposito")
# Nota: nao comparamos o novo queued_at contra `antigo` por valor (!=) — as
# chamadas anteriores rodam em sequencia tao rapida que dois datetime.now()
# podem cair na MESMA resolucao de relogio (flaky em Windows). O que este
# HOTFIX muda e SE release ainda re-carimba (sim, sempre) — nao a distancia
# em microssegundos entre dois carimbos.
aplicar_estado_humano(c, None, resetar_atendimento=True)
check(c.primeira_resposta_humana_at is None,
      "U6: release limpa primeira_resposta_humana_at (volta a esperar)")
check(c.queued_at is not None,
      "U6: release CARIMBA queued_at de novo (fim da fila) — comportamento "
      "inalterado pelo HOTFIX-01, so a resposta humana passou a preservar")

print("15 — unidade: nenhuma das duas funcoes referencia `state` (fix nao e V2-specific)")
c_legacy = Conversation(status="aberta", is_bot_active=True, state=None)
aplicar_estado_humano(c_legacy, 1)
antigo_legacy = c_legacy.queued_at
marcar_atendimento_humano(c_legacy, 1)
check(c_legacy.queued_at == antigo_legacy,
      "P6/U7: com state=None (LEGACY antes mesmo do server_default), o mesmo "
      "comportamento vale — as duas funcoes nunca leem/escrevem `state`")


print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
