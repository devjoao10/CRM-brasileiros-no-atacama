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
  3. Replay nao reaplica por cima de mudanca posterior legitima: se o dono
     mudou desde que aquele event_id aplicou, responde 409
     LEAD_ALREADY_ASSIGNED com o dono REAL em vez de afirmar um resultado ja
     superado. Continua respondendo replay (200) enquanto o dono bate — mesmo
     depois de o alvo ser desativado ou o lead apagado —, sem deixar de recusar
     um event_id INEDITO nas mesmas condicoes.
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
 14. Compare-and-swap por `expected_responsavel_id`: campo AUSENTE aplica sem
     checar (compatibilidade), presente-e-batendo aplica, presente-e-divergente
     recusa com 409 sem consumir o event_id, e `null` EXPLICITO significa
     "espero o lead sem dono" (nao "aplica incondicional"). Mais o snapshot em
     `previous_responsavel_id` (coluna da m018).
 15. Release legitimo e reatribuicao (Caso 6): o lead volta a ficar disponivel
     pelo BASELINE, nunca por TTL. Na janela sem dono, o replay do event_id
     antigo recusa com `current_responsavel_id=None` presente — "ninguem" e um
     valor de comparacao, nao um campo ausente — e um baseline obsoleto (o dono
     de antes do release) tambem recusa.
 16. Corpo estruturado dos DOIS 409: `LEAD_ALREADY_ASSIGNED` carrega
     `current_owner_event_id` e `current_owner_applied_at` como STRING ISO-8601
     (sem o `.isoformat()` explicito o `json.dumps` do Starlette estoura dentro
     do proprio handler e degrada para 500); `IDEMPOTENCY_PAYLOAD_CONFLICT`
     carrega os parametros GRAVADOS no livro-razao, nao os do payload novo.
 17. `event_id` duplicado CONCORRENTE responde replay (200), nunca 409: a gemea
     que perde o `FOR UPDATE` rele o lead ja aplicado, o CAS falha contra o
     baseline antigo, e sem a reconsulta do livro-razao ela sairia por
     LEAD_ALREADY_ASSIGNED sobre a propria repeticao. Sem engolir os conflitos
     de verdade: CAS perdido para OUTRO event_id continua 409, e a mesma chave
     com alvo divergente continua IDEMPOTENCY_PAYLOAD_CONFLICT.

A prova de CONCORRENCIA REAL do compare-and-swap nao mora aqui e nao PODE morar:
o dialeto SQLite do SQLAlchemy descarta `FOR UPDATE` em silencio. Ela vive em
`tests/test_v2_rb_cas_postgres.py`, que exige PostgreSQL e falha sem ele.

Todo nao-2xx desta rota responde `{"detail": {"error": "<TOKEN>"}}` — corpo
estruturado, nunca prosa. As excecoes sao os dois erros que nascem FORA deste
router e valem para toda a API do CRM: o 401 de `get_current_user` e o 422 de
schema do proprio FastAPI.

Nao toca banco real, rede, n8n nem Meta. SQLite descartavel em scratch/.

Rodar:  python tests/test_v2_lead_domain.py
"""
import os
import pathlib
import sys
import uuid
from datetime import datetime

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
# BIA-V2 R-B, Caso 2b: ate esta versao o replay respondia 200/replayed=true aqui.
# Dizer "ja apliquei, era a Ana" e verdade sobre o passado e MENTIRA sobre o
# presente — o Conversas aplicaria a Ana localmente enquanto o CRM ja tem o
# Bruno, e os dois lados relatariam sucesso divergindo em silencio. O replay
# agora compara o dono FRESCO contra o livro-razao e recusa.
check(r3.status_code == 409,
      f"409 quando o dono mudou desde que este event_id aplicou (veio {r3.status_code})")
_c3 = r3.json().get("detail", {}) if r3.status_code == 409 else {}
check(_c3.get("error") == "LEAD_ALREADY_ASSIGNED",
      f"token LEAD_ALREADY_ASSIGNED no corpo (veio {_c3.get('error')!r})")
check(_c3.get("current_responsavel_id") == BRUNO_ID,
      f"o corpo carrega o dono REAL (veio {_c3.get('current_responsavel_id')!r})")
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
# fora, numa sessao propria, sem passar pela rota.
_db = SessionLocal()
_db.add(LeadHandoffOperation(event_id=ev_r, lead_id=lead_r, target_user_id=ANA_ID))
_db.commit()
_db.close()


# A linha ja comitada acima, sozinha, NAO faz a rota chegar ao SAVEPOINT: a
# etapa 1 (`_buscar_registro`, sem lock) encontra o registro e responde pelo
# caminho rapido de replay — era assim antes desta versao tambem, apesar do que
# o comentario original deste bloco afirmava; este teste nunca tinha exercitado
# a colisao. Para exercitar de verdade e preciso reproduzir a JANELA real: a
# requisicao concorrente commita DEPOIS da etapa 1 e ANTES do INSERT. Fazer a
# primeira consulta ao livro-razao (e so a primeira) devolver None e a forma
# deterministica de abrir essa janela sem thread nem escrita concorrente em
# SQLite. A reconsulta de `_resolver_colisao` usa a funcao real.
from app.routers import v2_lead_domain as _v2_mod  # noqa: E402

_chamadas_buscar_registro = []
_buscar_registro_real = _v2_mod._buscar_registro


def _buscar_registro_com_janela(db, event_id):
    """Primeira chamada mente 'inedito'; da segunda em diante, a funcao real."""
    _chamadas_buscar_registro.append(event_id)
    if len(_chamadas_buscar_registro) == 1:
        return None
    return _buscar_registro_real(db, event_id)


with patch.object(_v2_mod, "_buscar_registro", _buscar_registro_com_janela):
    r7 = handoff(ev_r, lead_r, ANA_ID)
check(len(_chamadas_buscar_registro) == 2,
      f"a rota chegou mesmo a reconsulta pos-IntegrityError "
      f"(chamadas: {len(_chamadas_buscar_registro)})")
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
check(r13.json().get("detail") == {"error": "HANDOFF_NAO_APLICADO"},
      f"corpo do handler dedicado e o token estruturado, nao stack cru (veio {r13.text[:160]})")
check(estado(lead_op) == (None, 0, 0),
      f"nada foi gravado quando o SAVEPOINT falha por outro motivo (veio {estado(lead_op)})")
# A aplicacao continua sa depois — mesma garantia que o teste 7 prova para a
# colisao esperada do UNIQUE, agora para o caminho HandoffIndeterminado.
r13b = handoff(ev(), novo_lead("Lead Pos-Erro-Operacional"), BRUNO_ID)
check(r13b.status_code == 200 and r13b.json().get("replayed") is False,
      f"a aplicacao continua utilizavel depois da falha (veio {r13b.status_code})")


# ── 14. Compare-and-swap por expected_responsavel_id ──────────────────────
# BIA-V2 R-B: o CAS e o que impede duas operacoes concorrentes de aplicarem as
# duas. A prova de CONCORRENCIA de verdade exige PostgreSQL (o dialeto SQLite do
# SQLAlchemy DESCARTA a clausula FOR UPDATE em silencio) e vive no teste de
# aceitacao dos Casos 4/5. O que da para provar aqui — e que nao pode quebrar em
# silencio — sao os tres ramos da decisao e a distincao ausente-vs-null.

print("\n14. Compare-and-swap (expected_responsavel_id)")
_AUSENTE = object()


def handoff_cas(event_id, lead_id, target_user_id, expected=_AUSENTE):
    corpo = {"event_id": event_id, "lead_id": lead_id, "target_user_id": target_user_id}
    if expected is not _AUSENTE:
        corpo["expected_responsavel_id"] = expected
    return client.post("/api/v2/lead-domain/handoff", json=corpo, headers=HEADERS)


def registro_do_evento(event_id):
    db = SessionLocal()
    try:
        return (db.query(LeadHandoffOperation)
                  .filter(LeadHandoffOperation.event_id == event_id).first())
    finally:
        db.close()


# 14a. Campo AUSENTE = chamador anterior a esta versao: aplica sem checar, mesmo
# sobre lead que JA TEM dono. E o cenario "cliente que volta", e tratar ausente
# como `null` faria todo handoff desses falhar 409 durante a janela de deploy
# em que o CRM ja subiu e o Conversas ainda nao.
lead_cas = novo_lead("Lead CAS Ausente")
check(handoff_cas(ev(), lead_cas, ANA_ID).status_code == 200, "sem CAS: primeira aplicacao OK")
r14a = handoff_cas(ev(), lead_cas, BRUNO_ID)
check(r14a.status_code == 200 and estado(lead_cas)[0] == BRUNO_ID,
      f"campo AUSENTE aplica sobre lead que ja tem dono (veio {r14a.status_code})")

# 14b. Campo presente e BATENDO com o dono atual: aplica.
r14b = handoff_cas(ev(), lead_cas, ANA_ID, expected=BRUNO_ID)
check(r14b.status_code == 200 and estado(lead_cas)[0] == ANA_ID,
      f"CAS correto aplica (veio {r14b.status_code})")
check(r14b.json().get("current_responsavel_id") == ANA_ID,
      f"200 devolve current_responsavel_id (veio {r14b.json().get('current_responsavel_id')!r})")

# 14c. Campo presente e NAO batendo: 409, lead intocado, event_id nao consumido.
ev_cas = ev()
r14c = handoff_cas(ev_cas, lead_cas, BRUNO_ID, expected=99999999)
check(r14c.status_code == 409, f"CAS errado recusa com 409 (veio {r14c.status_code})")
_c14 = r14c.json().get("detail", {}) if r14c.status_code == 409 else {}
check(_c14.get("error") == "LEAD_ALREADY_ASSIGNED",
      f"token LEAD_ALREADY_ASSIGNED (veio {_c14.get('error')!r})")
check(_c14.get("current_responsavel_id") == ANA_ID and _c14.get("expected_responsavel_id") == 99999999,
      f"corpo carrega dono real e a expectativa recusada (veio {_c14})")
check(estado(lead_cas)[0] == ANA_ID, f"o lead NAO se moveu no 409 de CAS (veio {estado(lead_cas)[0]})")
check(linhas_do_evento(ev_cas) == 0, "o event_id recusado no CAS nao foi consumido")

# 14d. `null` EXPLICITO significa "espero o lead SEM dono" — nao "aplica
# incondicional". A leitura oposta reabriria a corrida que o CAS existe para
# fechar: quem nao manda expectativa venceria sempre.
r14d = handoff_cas(ev(), lead_cas, BRUNO_ID, expected=None)
check(r14d.status_code == 409,
      f"`null` explicito sobre lead COM dono recusa (veio {r14d.status_code})")

# 14e. `null` explicito sobre lead SEM dono: aplica.
lead_livre = novo_lead("Lead CAS Livre")
ev_livre = ev()
r14e = handoff_cas(ev_livre, lead_livre, ANA_ID, expected=None)
check(r14e.status_code == 200 and estado(lead_livre)[0] == ANA_ID,
      f"`null` explicito sobre lead SEM dono aplica (veio {r14e.status_code})")

# 14f. `previous_responsavel_id` (coluna da m018) guarda o dono ANTERIOR — o
# unico lugar onde esse valor sobrevive ao UPDATE.
check(registro_do_evento(ev_livre).previous_responsavel_id is None,
      "previous_responsavel_id = None quando o lead nao tinha dono")
ev_prev = ev()
check(handoff_cas(ev_prev, lead_livre, BRUNO_ID, expected=ANA_ID).status_code == 200,
      "aplicacao com dono anterior OK")
check(registro_do_evento(ev_prev).previous_responsavel_id == ANA_ID,
      f"previous_responsavel_id guarda o dono anterior (veio "
      f"{registro_do_evento(ev_prev).previous_responsavel_id!r})")


# ── 15. Release legitimo e reatribuicao (Caso 6 do desenho R-B) ───────────
# O Caso 6 e o que derrubou a hipotese do indice unico parcial (secao 1 do
# desenho): sem uma saida para o lead liberado, o claim travaria o lead para
# sempre. Aqui a saida e o BASELINE, nao o tempo — nao ha TTL em lugar nenhum.
# O check 14e prova `null` sobre um lead que NUNCA teve dono; este prova a
# transicao real teve-dono -> liberado -> reatribuido, que e outro caminho.

print("\n15. Release legitimo, replay na janela sem dono, e reatribuicao")
lead_rel = novo_lead("Lead Release Legitimo")
ev_rel = ev()
check(handoff_cas(ev_rel, lead_rel, ANA_ID, expected=None).status_code == 200,
      "atribuicao inicial ao primeiro responsavel OK")

_db = SessionLocal()
_db.query(Lead).filter(Lead.id == lead_rel).first().responsavel_id = None
_db.commit()
_db.close()
check(estado(lead_rel)[0] is None, "release zerou leads.responsavel_id")

# 15a. Sub-caso G5 do desenho (secao 2.1.1): na janela entre o release e a
# reatribuicao, `lead.responsavel_id` e None enquanto `registro.target_user_id`
# nunca e (coluna NOT NULL). O replay do event_id antigo SEMPRE acusa
# "diferente" aqui — e recusa. O nome do token engana ("ja atribuido" com valor
# None), mas o que ele comunica e "o valor que voce esperava nao e mais
# verdade", e None e um VALOR de comparacao legitimo, nao ausencia de dado.
r15a = handoff(ev_rel, lead_rel, ANA_ID)
check(r15a.status_code == 409,
      f"replay do event_id antigo na janela SEM dono recusa (veio {r15a.status_code})")
_c15a = r15a.json().get("detail", {}) if r15a.status_code == 409 else {}
check(_c15a.get("error") == "LEAD_ALREADY_ASSIGNED",
      f"token LEAD_ALREADY_ASSIGNED tambem no lead liberado (veio {_c15a.get('error')!r})")
check("current_responsavel_id" in _c15a and _c15a.get("current_responsavel_id") is None,
      f"`current_responsavel_id` PRESENTE e None — 'ninguem' e um valor, nao um "
      f"campo faltando (corpo: {_c15a})")

# 15b. Baseline OBSOLETO: quem ainda acha que o lead e da Ana (dono de ANTES do
# release) e recusado. O CAS decide por baseline, nao por quem chegou primeiro.
r15b = handoff_cas(ev(), lead_rel, BRUNO_ID, expected=ANA_ID)
check(r15b.status_code == 409,
      f"baseline obsoleto (dono de antes do release) recusa (veio {r15b.status_code})")
check(estado(lead_rel)[0] is None, "o lead continua sem dono apos o baseline obsoleto")

# 15c. Caso 6 propriamente dito: com o baseline CERTO (`null` = sem dono), a
# reatribuicao e aceita. Sem TTL, sem espera, sem intervencao manual.
ev_rel2 = ev()
r15c = handoff_cas(ev_rel2, lead_rel, BRUNO_ID, expected=None)
check(r15c.status_code == 200 and estado(lead_rel)[0] == BRUNO_ID,
      f"reatribuicao apos release aceita pelo baseline correto (veio {r15c.status_code})")
check(registro_do_evento(ev_rel2).previous_responsavel_id is None,
      "previous_responsavel_id registra que o lead saiu de 'ninguem'")


# ── 16. Corpo estruturado dos dois 409 (contrato da secao 7 do desenho) ───
# O 409 e o unico status desta rota que o Conversas discrimina por
# `detail.error`, e o unico cujo corpo carrega campos alem do token. Os checks 3
# e 14c ja provam o token; o que falta e o resto do contrato — em especial
# `current_owner_applied_at`, que vem de um `DateTime(timezone=True)` hidratado
# como objeto `datetime` VIVO. Sem o `.isoformat()` explicito de
# `_erro_lead_ja_atribuido`, o `json.dumps` do JSONResponse estoura `TypeError`
# DENTRO do handler de excecao, que ninguem recaptura: a resposta degrada para
# um 500 generico e o corpo estruturado some justamente no caso que mais
# importa. Nenhum check anterior tocava esse campo, entao a linha nunca era
# exercitada com assercao.

print("\n16. Corpo estruturado dos dois 409 (inclui data serializavel)")
lead_dt = novo_lead("Lead Corpo 409")
ev_dono = ev()
check(handoff_cas(ev_dono, lead_dt, ANA_ID, expected=None).status_code == 200,
      "dono estabelecido por uma operacao V2 (deixa rastro no livro-razao)")

r16 = handoff_cas(ev(), lead_dt, BRUNO_ID, expected=99999999)
check(r16.status_code == 409,
      f"409 de CAS, nao 500 — a data do corpo serializa (veio {r16.status_code}: "
      f"{r16.text[:160]})")
_c16 = r16.json().get("detail", {}) if r16.status_code == 409 else {}
check(_c16.get("current_owner_event_id") == ev_dono,
      f"`current_owner_event_id` aponta a operacao que pos o dono atual "
      f"(veio {_c16.get('current_owner_event_id')!r})")
_aplicado_em = _c16.get("current_owner_applied_at")
check(isinstance(_aplicado_em, str),
      f"`current_owner_applied_at` e STRING, nunca um datetime cru "
      f"(veio {type(_aplicado_em).__name__}: {_aplicado_em!r})")
_data_ok = False
if isinstance(_aplicado_em, str):
    try:
        datetime.fromisoformat(_aplicado_em)
        _data_ok = True
    except ValueError:
        _data_ok = False
check(_data_ok, f"`current_owner_applied_at` e ISO-8601 parseavel (veio {_aplicado_em!r})")

# O outro 409: `IDEMPOTENCY_PAYLOAD_CONFLICT` responde "com que parametros este
# event_id foi de fato aplicado?" — e por isso carrega os valores GRAVADOS, nao
# os recebidos. O check 4 so olhava o status code.
r16b = handoff(ev_dono, lead_dt, BRUNO_ID)
check(r16b.status_code == 409, f"409 de payload divergente (veio {r16b.status_code})")
_c16b = r16b.json().get("detail", {}) if r16b.status_code == 409 else {}
check(_c16b.get("error") == "IDEMPOTENCY_PAYLOAD_CONFLICT",
      f"token IDEMPOTENCY_PAYLOAD_CONFLICT (veio {_c16b.get('error')!r})")
check(_c16b.get("registered_lead_id") == lead_dt
      and _c16b.get("registered_target_user_id") == ANA_ID,
      f"o corpo devolve os parametros GRAVADOS, nao os do payload novo (veio {_c16b})")
check(_c16b.get("current_responsavel_id") == ANA_ID,
      f"`current_responsavel_id` presente tambem neste 409 (veio "
      f"{_c16b.get('current_responsavel_id')!r})")


# ── 17. event_id duplicado CONCORRENTE nao pode virar 409 ─────────────────
# Regressao de IDEMPOTENCIA introduzida pelo CAS, provada empiricamente contra
# PostgreSQL: duas requisicoes com o MESMO `event_id` (duas instancias do worker
# R16 podem legitimamente emitir as duas em paralelo) veem o livro-razao vazio na
# etapa 1 e se serializam no `FOR UPDATE`. A perdedora rele o lead JA aplicado
# pela gemea, o CAS falha contra o baseline antigo, e ela saia por 409
# LEAD_ALREADY_ASSIGNED — sem NUNCA alcancar `_resolver_colisao`, o unico codigo
# que reconheceria "mesmo event_id, mesmos parametros => replay". O Conversas
# recebia CRM_LEAD_JA_ATRIBUIDO sobre a propria repeticao e podia empurrar a
# operacao para o diagnostico errado de R15.
#
# Em SQLite nao ha `FOR UPDATE` (o dialeto descarta em silencio), entao a
# JANELA e reproduzida do mesmo jeito deterministico do check 7: a primeira
# consulta ao livro-razao mente "inedito", e o resto da requisicao encontra o
# mundo que a gemea ja deixou aplicado (livro-razao gravado + lead com o dono
# novo). A prova sob concorrencia REAL exige PostgreSQL e nao cabe aqui.

print("\n17. event_id duplicado CONCORRENTE responde replay, nunca 409")


def _com_janela_de_corrida(fn):
    """Roda `fn()` com a PRIMEIRA consulta ao livro-razao devolvendo None.

    E a janela em que a gemea concorrente ainda nao havia commitado quando a
    etapa 1 rodou. Da segunda consulta em diante vale a funcao real — inclusive
    a reconsulta do ramo de CAS, que e justamente o que esta sob teste.
    """
    chamadas = []

    def _janela(db, event_id):
        chamadas.append(event_id)
        return None if len(chamadas) == 1 else _buscar_registro_real(db, event_id)

    with patch.object(_v2_mod, "_buscar_registro", _janela):
        return fn(), len(chamadas)


lead_g = novo_lead("Lead Gemea Concorrente")
ev_g = ev()
# A gemea que VENCEU a corrida: grava o livro-razao E aplica o responsavel, o
# mesmo par que o handler commita junto.
_db = SessionLocal()
_db.add(LeadHandoffOperation(event_id=ev_g, lead_id=lead_g, target_user_id=ANA_ID,
                             previous_responsavel_id=None))
_db.query(Lead).filter(Lead.id == lead_g).first().responsavel_id = ANA_ID
_db.commit()
_db.close()

# A perdedora: MESMO event_id, MESMOS parametros, MESMA expectativa (`null` =
# "espero o lead sem dono", que era verdade quando ela comecou).
r17, _n17 = _com_janela_de_corrida(
    lambda: handoff_cas(ev_g, lead_g, ANA_ID, expected=None)
)
check(_n17 == 2, f"a rota chegou mesmo a reconsulta do ramo de CAS (consultas: {_n17})")
check(r17.status_code == 200,
      f"200, nao 409, para o mesmo event_id que a gemea concorrente aplicou "
      f"(veio {r17.status_code}: {r17.text[:160]})")
check(r17.status_code == 200 and r17.json().get("replayed") is True,
      "a gemea perdedora recebe replayed=true, nao LEAD_ALREADY_ASSIGNED")
check(linhas_do_evento(ev_g) == 1,
      f"nenhuma segunda linha no livro-razao (veio {linhas_do_evento(ev_g)})")
check(estado(lead_g)[0] == ANA_ID, f"o lead nao se moveu (veio {estado(lead_g)[0]})")

# A correcao NAO pode engolir o conflito de CAS de verdade: um event_id INEDITO
# perdendo o baseline continua 409 — e o check que impede a "correcao" de virar
# um `if not cas_ok: pass`.
r17b = handoff_cas(ev(), lead_g, BRUNO_ID, expected=None)
check(r17b.status_code == 409,
      f"CAS perdido para OUTRO event_id continua 409 (veio {r17b.status_code})")
_c17b = r17b.json().get("detail", {}) if r17b.status_code == 409 else {}
check(_c17b.get("error") == "LEAD_ALREADY_ASSIGNED",
      f"e continua sendo o token LEAD_ALREADY_ASSIGNED (veio {_c17b.get('error')!r})")
check(estado(lead_g)[0] == ANA_ID, "o lead nao se moveu no 409 real de CAS")

# E a reconsulta continua conferindo parametros: mesma chave com OUTRO alvo e
# adulteracao, nao replay — o token muda para IDEMPOTENCY_PAYLOAD_CONFLICT.
r17c, _ = _com_janela_de_corrida(
    lambda: handoff_cas(ev_g, lead_g, BRUNO_ID, expected=None)
)
check(r17c.status_code == 409,
      f"mesmo event_id com alvo divergente na janela continua 409 (veio {r17c.status_code})")
_c17c = r17c.json().get("detail", {}) if r17c.status_code == 409 else {}
check(_c17c.get("error") == "IDEMPOTENCY_PAYLOAD_CONFLICT",
      f"token IDEMPOTENCY_PAYLOAD_CONFLICT na reconsulta do ramo de CAS "
      f"(veio {_c17c.get('error')!r})")
check(estado(lead_g)[0] == ANA_ID, "o lead nao se moveu no 409 de payload divergente")


# ── Resultado ─────────────────────────────────────────────────────────────

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} CHECK(S) FALHARAM:")
    for f in failures:
        print(f"  - {f}")
else:
    print("TODOS OS CHECKS DO HANDOFF V2 (LADO CRM) PASSARAM")
sys.exit(1 if failures else 0)
