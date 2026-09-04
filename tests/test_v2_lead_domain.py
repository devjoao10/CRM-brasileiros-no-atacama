# -*- coding: utf-8 -*-
"""
BIA-V2 Fase 6 — lado CRM do handoff: POST /api/v2/lead-domain/handoff.

Teste do CRM (job Python 3.11 do CI). Nao insere nada no sys.path do Conversas
e nao importa nada de la — o marcador de roteamento do outro job nao aparece
aqui de proposito.

O QUE ESTE ARQUIVO PROVA
------------------------
  1. Sucesso: responsavel aplicado, livro-razao gravado, rastro em
     lead_history, `replayed=false`.
  2. Mesmo event_id duas vezes: `replayed=true`, ZERO efeito duplicado (uma
     linha de livro-razao, um evento de historico).
  3. Replay nao reaplica por cima de mudanca posterior legitima, e continua
     respondendo replay mesmo depois de o alvo ser desativado ou o lead
     apagado — sem deixar de recusar um event_id INEDITO nas mesmas condicoes.
  4. Mesmo event_id com target_user_id diferente: 409, e o lead NAO se move.
  5. lead_id inexistente: 404. target_user_id inexistente: 404.
  6. target_user_id existente mas inativo: 422, e nada e gravado.
  7. Colisao real no SAVEPOINT (a outra requisicao venceu a corrida e ja
     comitou): o segundo nao corrompe a sessao — responde 200 e a aplicacao
     continua utilizavel depois.
  8. `event_id` e canonicalizado: a MESMA chave em maiusculas nao vira uma
     segunda linha (senao o UNIQUE nao pega e o handoff roda duas vezes).
  9. Corpo malformado: event_id vazio / nao-UUID / campo extra / id <= 0 = 422.
 10. Sem credencial ou com API key invalida: 401, e nada e gravado.
 10b. Autenticado mas NAO e o usuario de servico configurado: 403, e nada e
     gravado — o usuario de servico configurado continua funcionando.
 11. Nenhuma resposta 4xx/5xx vaza stack trace, SQL, nome de tabela/coluna ou
     caminho de arquivo.
 12. A migration m017 aplica e reaplica (idempotente) contra SQLite
     descartavel, e o gate pos-DDL recusa tabela sem o UNIQUE.
 13. Falha do SQLAlchemy que NAO e a colisao esperada de event_id (ex.:
     OperationalError dentro do SAVEPOINT) vira HandoffIndeterminado / 500
     com corpo generico, nunca o 500 cru do Starlette.

Nao toca banco real, rede, n8n nem Meta. SQLite descartavel em scratch/.

Rodar:  python tests/test_v2_lead_domain.py
"""
import os
import pathlib
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "v2_lead_domain_test.db"
if DB_FILE.exists():
    try:
        DB_FILE.unlink()
    except PermissionError:
        pass

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["SEED_INITIAL_ADMIN"] = "false"
os.environ["GEMINI_API_KEY"] = ""

sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from unittest.mock import patch  # noqa: E402

from app import config  # noqa: E402
from app.auth import hash_api_key  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models.lead import Lead  # noqa: E402
from app.models.lead_handoff_operation import LeadHandoffOperation  # noqa: E402
from app.models.pipeline import LeadHistory  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402

failures = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        failures.append(msg)


# ── Fixtures ──────────────────────────────────────────────────────────────

Base.metadata.create_all(bind=engine)

API_KEY = "bna_chave-de-teste-do-handoff"
HEADERS = {"X-API-Key": API_KEY}
# Achado 1 (security-reviewer): credencial de OUTRO usuario autenticado, que
# NAO e o usuario de servico do handoff — usada para provar o 403.
OUTRO_API_KEY = "bna_chave-de-outro-usuario-autenticado"
OUTRO_HEADERS = {"X-API-Key": OUTRO_API_KEY}

_db = SessionLocal()
_integrador = User(nome="Integrador", email="integrador@teste.local", hashed_password="x",
                   role=UserRole.USER, is_active=True, email_verified=True,
                   api_key=hash_api_key(API_KEY))
_ana = User(nome="Ana", email="ana@teste.local", hashed_password="x",
            role=UserRole.USER, is_active=True, email_verified=True)
_bruno = User(nome="Bruno", email="bruno@teste.local", hashed_password="x",
              role=UserRole.USER, is_active=True, email_verified=True)
_inativo = User(nome="Inativo", email="inativo@teste.local", hashed_password="x",
                role=UserRole.USER, is_active=False, email_verified=True)
_outro = User(nome="Outro", email="outro@teste.local", hashed_password="x",
              role=UserRole.USER, is_active=True, email_verified=True,
              api_key=hash_api_key(OUTRO_API_KEY))
_db.add_all([_integrador, _ana, _bruno, _inativo, _outro])
_db.commit()
INTEGRADOR_ID, ANA_ID, BRUNO_ID, INATIVO_ID, OUTRO_ID = (
    _integrador.id, _ana.id, _bruno.id, _inativo.id, _outro.id,
)
_db.close()

# Achado 1 (security-reviewer): so o usuario de servico configurado pode
# chamar o handoff (app/config.py nao tem default utilizavel). Aponta
# explicitamente para o "Integrador" acima, do MESMO jeito que a producao
# aponta para o id do usuario cuja API key vira BIA_V2_CRM_API_KEY do lado Conversas.
# Sobrescrito PELO MODULO (nao por `from app.config import ...`) porque
# `_conferir_chamador_autorizado` le `config.V2_LEAD_DOMAIN_SERVICE_USER_ID`
# em tempo de chamada — mesmo padrao de `_get_user_from_internal_ai`
# (app/auth.py) — o que deixa o teste livre para usar o id REAL gerado pelo
# banco em vez de supor um valor fixo.
config.V2_LEAD_DOMAIN_SERVICE_USER_ID = INTEGRADOR_ID

client = TestClient(app)


def novo_lead(nome):
    db = SessionLocal()
    try:
        lead = Lead(nome=nome, whatsapp="+56000000001")
        db.add(lead)
        db.commit()
        return lead.id
    finally:
        db.close()


def estado(lead_id):
    """(responsavel_id, nº de linhas no livro-razao, nº de eventos de historico)."""
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        return (
            lead.responsavel_id if lead else None,
            db.query(LeadHandoffOperation)
              .filter(LeadHandoffOperation.lead_id == lead_id).count(),
            db.query(LeadHistory)
              .filter(LeadHistory.lead_id == lead_id,
                      LeadHistory.evento == "responsavel_changed").count(),
        )
    finally:
        db.close()


def linhas_do_evento(event_id):
    """Quantas linhas o livro-razao tem para ESTE event_id.

    Contar por `event_id` e nao por `lead_id` nao e preciosismo: o check 3b
    apaga um lead de proposito (para provar que o replay sobrevive a isso), e o
    SQLite REAPROVEITA o rowid liberado no proximo lead criado. Contar por lead
    faria a linha orfa — que existe justamente porque o livro-razao nao tem FK
    — ser somada ao lead seguinte, e o teste acusaria duplicacao onde nao ha.
    """
    db = SessionLocal()
    try:
        return (db.query(LeadHandoffOperation)
                  .filter(LeadHandoffOperation.event_id == event_id).count())
    finally:
        db.close()


def handoff(event_id, lead_id, target_user_id, headers=HEADERS):
    return client.post(
        "/api/v2/lead-domain/handoff",
        json={"event_id": event_id, "lead_id": lead_id, "target_user_id": target_user_id},
        headers=headers,
    )


def ev():
    return str(uuid.uuid4())


# ── 1. Sucesso simples ────────────────────────────────────────────────────

print("\n1. Sucesso simples")
lead_id = novo_lead("Lead Sucesso")
event_id = ev()
r = handoff(event_id, lead_id, ANA_ID)
check(r.status_code == 200, f"200 na primeira aplicacao (veio {r.status_code}: {r.text[:160]})")
corpo = r.json() if r.status_code == 200 else {}
check(corpo.get("replayed") is False, f"replayed=false na primeira aplicacao (veio {corpo.get('replayed')!r})")
check(corpo.get("lead_id") == lead_id, "corpo ecoa lead_id")
check(corpo.get("target_user_id") == ANA_ID, "corpo ecoa target_user_id")
check(corpo.get("event_id") == event_id, "corpo ecoa event_id")
check(estado(lead_id) == (ANA_ID, 1, 1),
      f"responsavel aplicado + 1 livro-razao + 1 historico (veio {estado(lead_id)})")


# ── 2. Replay: mesmo event_id, mesmos parametros ──────────────────────────

print("\n2. Replay idempotente (mesmo event_id duas vezes)")
r2 = handoff(event_id, lead_id, ANA_ID)
check(r2.status_code == 200, f"200 no replay (veio {r2.status_code}: {r2.text[:160]})")
check(r2.json().get("replayed") is True, "replayed=true na segunda chamada")
check(r2.json().get("target_user_id") == ANA_ID, "replay devolve o mesmo target_user_id")
check(estado(lead_id) == (ANA_ID, 1, 1),
      f"replay NAO duplicou nada (veio {estado(lead_id)})")


# ── 3. Replay nao sobrescreve mudanca posterior legitima ──────────────────

print("\n3. Replay nao reaplica por cima de mudanca posterior")
_db = SessionLocal()
_db.query(Lead).filter(Lead.id == lead_id).first().responsavel_id = BRUNO_ID
_db.commit()
_db.close()
r3 = handoff(event_id, lead_id, ANA_ID)
check(r3.status_code == 200 and r3.json().get("replayed") is True, "replay continua 200/true")
check(estado(lead_id)[0] == BRUNO_ID,
      f"o responsavel atual (Bruno) foi PRESERVADO (veio {estado(lead_id)[0]})")


# ── 3b. Replay sobrevive a alvo desativado / lead apagado ────────────────
# Se a validacao rodasse ANTES da consulta ao livro-razao, estes dois casos
# responderiam 422 e 404 — o CRM afirmando que falhou um handoff que esta
# aplicado, e os dois sistemas divergindo. Ver o docstring de `aplicar_handoff`.

print("\n3b. Replay depois de o alvo ser desativado / o lead ser apagado")
lead_d = novo_lead("Lead Alvo Desativado")
ev_d = ev()
_alvo = SessionLocal()
_temporario = User(nome="Temp", email="temp@teste.local", hashed_password="x",
                   role=UserRole.USER, is_active=True, email_verified=True)
_alvo.add(_temporario)
_alvo.commit()
TEMP_ID = _temporario.id
_alvo.close()

check(handoff(ev_d, lead_d, TEMP_ID).status_code == 200, "handoff aplicado com o alvo ativo")
_alvo = SessionLocal()
_alvo.query(User).filter(User.id == TEMP_ID).first().is_active = False
_alvo.commit()
_alvo.close()
r3b = handoff(ev_d, lead_d, TEMP_ID)
check(r3b.status_code == 200 and r3b.json().get("replayed") is True,
      f"replay continua 200/true mesmo com o alvo agora inativo (veio {r3b.status_code})")
# Mas um event_id INEDITO para o mesmo alvo inativo continua sendo recusado.
check(handoff(ev(), lead_d, TEMP_ID).status_code == 422,
      "event_id inedito para alvo inativo continua 422")

lead_x = novo_lead("Lead Que Some")
ev_x = ev()
check(handoff(ev_x, lead_x, ANA_ID).status_code == 200, "handoff aplicado antes de apagar o lead")
_alvo = SessionLocal()
_alvo.query(Lead).filter(Lead.id == lead_x).delete()
_alvo.commit()
_alvo.close()
r3c = handoff(ev_x, lead_x, ANA_ID)
check(r3c.status_code == 200 and r3c.json().get("replayed") is True,
      f"replay sobrevive ao lead apagado — o livro-razao nao tem FK (veio {r3c.status_code})")
check(handoff(ev(), lead_x, ANA_ID).status_code == 404,
      "event_id inedito para lead inexistente continua 404")


# ── 4. Mesmo event_id, target_user_id diferente => 409 ────────────────────

print("\n4. event_id repetido com parametros divergentes")
lead_c = novo_lead("Lead Conflito")
ev_c = ev()
check(handoff(ev_c, lead_c, ANA_ID).status_code == 200, "primeira aplicacao OK")
r4 = handoff(ev_c, lead_c, BRUNO_ID)
check(r4.status_code == 409, f"409 com target_user_id diferente (veio {r4.status_code})")
check(estado(lead_c)[0] == ANA_ID, f"o lead NAO se moveu no 409 (veio {estado(lead_c)[0]})")
check(linhas_do_evento(ev_c) == 1,
      f"nenhuma linha nova no livro-razao (veio {linhas_do_evento(ev_c)})")

r4b = handoff(ev_c, novo_lead("Lead Outro"), ANA_ID)
check(r4b.status_code == 409, f"409 tambem com lead_id diferente (veio {r4b.status_code})")


# ── 5. Alvos inexistentes => 404 ──────────────────────────────────────────

print("\n5. lead_id / target_user_id inexistentes")
r5 = handoff(ev(), 99999999, ANA_ID)
check(r5.status_code == 404, f"404 para lead_id inexistente (veio {r5.status_code})")
r5b = handoff(ev(), novo_lead("Lead User Fantasma"), 99999999)
check(r5b.status_code == 404, f"404 para target_user_id inexistente (veio {r5b.status_code})")


# ── 6. Usuario alvo inativo => 422 ────────────────────────────────────────

print("\n6. target_user_id inativo")
lead_i = novo_lead("Lead Inativo")
ev_i = ev()
r6 = handoff(ev_i, lead_i, INATIVO_ID)
check(r6.status_code == 422, f"422 para usuario inativo (veio {r6.status_code})")
check(estado(lead_i) == (None, 0, 0), f"nada foi gravado (veio {estado(lead_i)})")
# O event_id recusado NAO foi consumido: pode ser reusado com um alvo valido.
check(handoff(ev_i, lead_i, ANA_ID).status_code == 200,
      "o event_id recusado no 422 nao foi consumido")


# ── 7. Colisao real no SAVEPOINT (a outra requisicao ja comitou) ──────────

print("\n7. Colisao concorrente no SAVEPOINT")
lead_r = novo_lead("Lead Corrida")
ev_r = ev()
# Simula a requisicao concorrente que VENCEU: grava e comita o livro-razao por
# fora, numa sessao propria, sem passar pela rota. Quando a rota rodar, o
# INSERT dentro do begin_nested() vai bater no UNIQUE de verdade.
_db = SessionLocal()
_db.add(LeadHandoffOperation(event_id=ev_r, lead_id=lead_r, target_user_id=ANA_ID))
_db.commit()
_db.close()

r7 = handoff(ev_r, lead_r, ANA_ID)
check(r7.status_code == 200, f"200 apos colidir no SAVEPOINT (veio {r7.status_code}: {r7.text[:160]})")
check(r7.json().get("replayed") is True, "a colisao virou replay, nao erro")
check(linhas_do_evento(ev_r) == 1,
      f"uma unica linha no livro-razao (veio {linhas_do_evento(ev_r)})")
# A sessao/aplicacao continua sa depois da colisao — este e o ponto do check.
r7b = handoff(ev(), novo_lead("Lead Pos-Colisao"), BRUNO_ID)
check(r7b.status_code == 200 and r7b.json().get("replayed") is False,
      f"a aplicacao continua utilizavel depois da colisao (veio {r7b.status_code})")

# Divergente e ja comitado por outro => 409, mesmo caminho de colisao.
lead_r2 = novo_lead("Lead Corrida Divergente")
ev_r2 = ev()
_db = SessionLocal()
_db.add(LeadHandoffOperation(event_id=ev_r2, lead_id=lead_r2, target_user_id=BRUNO_ID))
_db.commit()
_db.close()
check(handoff(ev_r2, lead_r2, ANA_ID).status_code == 409,
      "colisao com parametros divergentes vira 409")


# ── 8. Canonicalizacao do event_id ────────────────────────────────────────

print("\n8. event_id canonicalizado (maiuscula = mesma chave)")
lead_u = novo_lead("Lead Canonico")
ev_u = ev()
check(handoff(ev_u, lead_u, ANA_ID).status_code == 200, "primeira aplicacao OK")
r8 = handoff(ev_u.upper(), lead_u, ANA_ID)
check(r8.status_code == 200 and r8.json().get("replayed") is True,
      f"o MESMO uuid em maiusculas e replay, nao aplicacao nova (veio {r8.status_code}/"
      f"{r8.json().get('replayed') if r8.status_code == 200 else '-'})")
check(linhas_do_evento(ev_u) == 1,
      f"nao virou uma segunda linha no livro-razao (veio {linhas_do_evento(ev_u)})")
check(r8.json().get("event_id") == ev_u, "resposta devolve a forma canonica (minuscula)")


# ── 9. Corpo malformado => 422 ────────────────────────────────────────────

print("\n9. Corpo malformado")
lead_m = novo_lead("Lead Malformado")
for rotulo, corpo_ruim in [
    ("event_id vazio", {"event_id": "", "lead_id": lead_m, "target_user_id": ANA_ID}),
    ("event_id so espacos", {"event_id": "   ", "lead_id": lead_m, "target_user_id": ANA_ID}),
    ("event_id nao-UUID", {"event_id": "nao-e-uuid", "lead_id": lead_m, "target_user_id": ANA_ID}),
    ("event_id ausente", {"lead_id": lead_m, "target_user_id": ANA_ID}),
    ("lead_id ausente", {"event_id": str(uuid.uuid4()), "target_user_id": ANA_ID}),
    ("lead_id <= 0", {"event_id": str(uuid.uuid4()), "lead_id": 0, "target_user_id": ANA_ID}),
    ("campo extra", {"event_id": str(uuid.uuid4()), "lead_id": lead_m,
                     "target_user_id": ANA_ID, "reason": "x"}),
]:
    resp = client.post("/api/v2/lead-domain/handoff", json=corpo_ruim, headers=HEADERS)
    check(resp.status_code == 422, f"422 para {rotulo} (veio {resp.status_code})")
check(estado(lead_m) == (None, 0, 0), f"nenhum corpo malformado gravou nada (veio {estado(lead_m)})")


# ── 10. Autenticacao ──────────────────────────────────────────────────────

print("\n10. Autenticacao obrigatoria")
lead_a = novo_lead("Lead Sem Auth")
r10 = client.post("/api/v2/lead-domain/handoff",
                  json={"event_id": ev(), "lead_id": lead_a, "target_user_id": ANA_ID})
check(r10.status_code == 401, f"401 sem credencial (veio {r10.status_code})")
r10b = handoff(ev(), lead_a, ANA_ID, headers={"X-API-Key": "bna_chave-errada"})
check(r10b.status_code == 401, f"401 com API key invalida (veio {r10b.status_code})")
check(estado(lead_a) == (None, 0, 0), f"nada gravado sem auth valida (veio {estado(lead_a)})")


# ── 10b. Autorizacao: so o usuario de servico configurado pode chamar ─────
# Achado 1 (security-reviewer): antes desta correcao, QUALQUER sessao
# autenticada (nao so o usuario de servico do Conversas) podia trocar
# `responsavel_id` sem passar pela ponte que `PUT /api/leads/{id}/responsavel`
# bloqueia de proposito. Ver a secao AUTORIZACAO do docstring do router.

print("\n10b. Autorizacao: usuario autenticado que NAO e o de servico leva 403")
lead_na = novo_lead("Lead Autorizacao Errada")
r10c = handoff(ev(), lead_na, ANA_ID, headers=OUTRO_HEADERS)
check(r10c.status_code == 403,
      f"403 para usuario autenticado que nao e o de servico (veio {r10c.status_code})")
check(estado(lead_na) == (None, 0, 0),
      f"nada gravado quando o chamador nao e autorizado (veio {estado(lead_na)})")
r10d = handoff(ev(), lead_na, ANA_ID, headers=HEADERS)
check(r10d.status_code == 200 and r10d.json().get("replayed") is False,
      f"o usuario de servico configurado continua funcionando (veio {r10d.status_code})")


# ── 11. Nenhum vazamento em erro ──────────────────────────────────────────

print("\n11. Respostas de erro nao vazam interno")
_PROIBIDO = [
    "traceback", "sqlalchemy", "integrityerror", "unique constraint",
    "lead_handoff_operations", "uq_lead_handoff_operations", "select ", "insert into",
    "hashed_password", "api_key", str(ROOT).lower(),
]
_amostras = [
    ("404 lead", handoff(ev(), 99999999, ANA_ID)),
    ("404 user", handoff(ev(), novo_lead("Lead Vaza 1"), 99999999)),
    ("422 inativo", handoff(ev(), novo_lead("Lead Vaza 2"), INATIVO_ID)),
    ("409", handoff(ev_c, lead_c, BRUNO_ID)),
    ("403", handoff(ev(), lead_c, ANA_ID, headers=OUTRO_HEADERS)),
    ("401", client.post("/api/v2/lead-domain/handoff",
                        json={"event_id": ev(), "lead_id": lead_c, "target_user_id": ANA_ID})),
    ("422 corpo", client.post("/api/v2/lead-domain/handoff",
                              json={"event_id": "x", "lead_id": lead_c,
                                    "target_user_id": ANA_ID}, headers=HEADERS)),
]
for rotulo, resp in _amostras:
    texto = resp.text.lower()
    vazou = [t for t in _PROIBIDO if t in texto]
    check(not vazou, f"{rotulo} nao vaza interno (encontrado: {vazou}) — corpo: {resp.text[:120]}")

# O 500 de `HandoffIndeterminado` tem handler na instancia RAIZ e corpo generico.
from app.routers import v2_lead_domain  # noqa: E402

check(any(issubclass(k, Exception) and k is v2_lead_domain.HandoffIndeterminado
          for k in app.exception_handlers),
      "HandoffIndeterminado tem handler registrado na instancia FastAPI raiz")
_handler_500 = app.exception_handlers[v2_lead_domain.HandoffIndeterminado]


async def _corpo_do_500():
    class _Req:
        url = type("U", (), {"path": "/api/v2/lead-domain/handoff"})()
    return await _handler_500(_Req(), v2_lead_domain.HandoffIndeterminado(
        "IntegrityError: UNIQUE constraint failed: lead_handoff_operations.event_id"
    ))


import asyncio  # noqa: E402

_resp_500 = asyncio.run(_corpo_do_500())
_corpo_500 = _resp_500.body.decode().lower()
check(_resp_500.status_code == 500, f"HandoffIndeterminado vira 500 (veio {_resp_500.status_code})")
check(not [t for t in _PROIBIDO if t in _corpo_500],
      f"o corpo do 500 e generico, sem detalhe do banco (veio {_corpo_500[:160]})")


# ── 12. Migration m017 contra SQLite descartavel ──────────────────────────

print("\n12. Migration m017 (SQLite descartavel, nunca banco real)")
sys.path.insert(0, str(ROOT / "migrations"))
import m017_lead_handoff_operations as m017  # noqa: E402

MIG_DB = SCRATCH / "v2_lead_domain_m017.db"
if MIG_DB.exists():
    try:
        MIG_DB.unlink()
    except PermissionError:
        pass
mig_engine = create_engine(f"sqlite:///{MIG_DB.as_posix()}")
with mig_engine.begin() as conn:
    conn.execute(text("CREATE TABLE leads (id INTEGER PRIMARY KEY)"))

acoes1 = m017.run(mig_engine)
check(any("created" in a for a in acoes1), f"m017 cria a tabela (acoes: {acoes1})")
acoes2 = m017.run(mig_engine)
check(any("already-present" in a for a in acoes2),
      f"m017 e idempotente na segunda rodada (acoes: {acoes2})")

_insp = inspect(mig_engine)
_cols = {c["name"] for c in _insp.get_columns("lead_handoff_operations")}
check({"id", "event_id", "lead_id", "target_user_id", "created_at"} <= _cols,
      f"todas as colunas presentes (veio {sorted(_cols)})")
check(any(ix.get("unique") and ix.get("column_names") == ["event_id"]
          for ix in _insp.get_indexes("lead_handoff_operations")),
      "UNIQUE em event_id presente")

# O DDL do create_all() e o da migration produzem o MESMO objeto: o nome do
# indice unico tem de bater, senao os dois donos de schema divergem.
_insp_app = inspect(engine)
_nomes_app = {ix["name"] for ix in _insp_app.get_indexes("lead_handoff_operations")
              if ix.get("unique")}
check("uq_lead_handoff_operations_event_id" in _nomes_app,
      f"create_all() emite o MESMO nome de indice que a m017 (veio {sorted(_nomes_app)})")

# Alvo errado (sem `leads`) e recusado, nunca reportado como NO-OP.
ALVO_ERRADO = SCRATCH / "v2_lead_domain_m017_alvo_errado.db"
if ALVO_ERRADO.exists():
    try:
        ALVO_ERRADO.unlink()
    except PermissionError:
        pass
_errado = create_engine(f"sqlite:///{ALVO_ERRADO.as_posix()}")
with _errado.begin() as conn:
    conn.execute(text("CREATE TABLE nada_a_ver (id INTEGER PRIMARY KEY)"))
try:
    m017.run(_errado)
    check(False, "m017 deveria RECUSAR um alvo sem a tabela leads")
except m017.WrongTargetError:
    check(True, "m017 recusa alvo sem a tabela leads em vez de dizer NO-OP")

# Gate pos-DDL pega tabela pre-existente SEM o UNIQUE (o caso que o
# `CREATE ... IF NOT EXISTS` transforma em NO-OP silencioso).
PARCIAL = SCRATCH / "v2_lead_domain_m017_parcial.db"
if PARCIAL.exists():
    try:
        PARCIAL.unlink()
    except PermissionError:
        pass
_parcial = create_engine(f"sqlite:///{PARCIAL.as_posix()}")
with _parcial.begin() as conn:
    conn.execute(text(
        "CREATE TABLE lead_handoff_operations ("
        "id INTEGER PRIMARY KEY, event_id VARCHAR(36), lead_id INTEGER,"
        " target_user_id INTEGER, created_at TIMESTAMP)"
    ))
try:
    m017._verificar_pos_ddl(_parcial, [])
    check(False, "o gate deveria recusar tabela sem o UNIQUE em event_id")
except RuntimeError as exc:
    check("UNIQUE" in str(exc), f"o gate recusa tabela sem o UNIQUE (msg: {str(exc)[:80]})")


# ── 13. Falha SQLAlchemy nao-IntegrityError no SAVEPOINT ──────────────────
# Achado 3 (fastapi-reviewer): so `IntegrityError` era capturado ao redor do
# `begin_nested()`. Qualquer OUTRA falha do SQLAlchemy (ex.: `OperationalError`
# por timeout de lock ou conexao derrubada) fugia do handler dedicado e do log
# estruturado, virando o 500 cru do Starlette. Simula a falha fazendo
# `Session.begin_nested` levantar `OperationalError` — mesmo mecanismo que a
# rota usa (`with db.begin_nested(): ...`), sem tocar banco real.

print("\n13. Falha SQLAlchemy nao-IntegrityError no SAVEPOINT vira HandoffIndeterminado")
lead_op = novo_lead("Lead Erro Operacional")
ev_op = ev()


def _begin_nested_com_falha(self, *args, **kwargs):
    raise OperationalError("SAVEPOINT sp_simulado", {}, Exception("simulated lock timeout"))


with patch.object(Session, "begin_nested", _begin_nested_com_falha):
    r13 = handoff(ev_op, lead_op, ANA_ID)

check(r13.status_code == 500,
      f"500 via handler dedicado para falha SQLAlchemy nao-IntegrityError (veio {r13.status_code})")
check(r13.json().get("detail") == "Nao foi possivel concluir a operacao de handoff.",
      f"corpo generico do handler dedicado, nao stack cru (veio {r13.text[:160]})")
check(estado(lead_op) == (None, 0, 0),
      f"nada foi gravado quando o SAVEPOINT falha por outro motivo (veio {estado(lead_op)})")
# A aplicacao continua sa depois — mesma garantia que o teste 7 prova para a
# colisao esperada do UNIQUE, agora para o caminho HandoffIndeterminado.
r13b = handoff(ev(), novo_lead("Lead Pos-Erro-Operacional"), BRUNO_ID)
check(r13b.status_code == 200 and r13b.json().get("replayed") is False,
      f"a aplicacao continua utilizavel depois da falha (veio {r13b.status_code})")


# ── Resultado ─────────────────────────────────────────────────────────────

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} CHECK(S) FALHARAM:")
    for f in failures:
        print(f"  - {f}")
else:
    print("TODOS OS CHECKS DO HANDOFF V2 (LADO CRM) PASSARAM")
sys.exit(1 if failures else 0)
