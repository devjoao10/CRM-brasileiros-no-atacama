# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-F2 — o contrato REAL entre o n8n de producao e o CRM.

Os corpos testados nao sao inventados: sao lidos do export de producao em
n8n/workflows/live_exports/20260825_fase2/ e enviados ao ENDPOINT de verdade.

Defeito que motivou o arquivo: `Tool Atualizar Lead` (workflow "Agente
Gerenciador de Leads — BnA") tem `jsonBody` FIXO — manda as doze chaves em TODA
chamada — e o toolDescription instrui "Campos sem informacao devem ficar
vazios". Com `nome: Field(None, min_length=1)`, toda atualizacao sem nome novo
devolvia 422, e o dado que a Bia acabara de coletar era descartado em silencio:
o toolHttpRequest entrega o erro ao modelo, que segue conversando como se
tivesse gravado. Defeito PRE-EXISTENTE — `min_length=1` e identico em
origin/main.

E POR QUE ESTE TESTE BATE NA ROTA, E NAO NO SCHEMA. A primeira versao desta
correcao converteu `""` em `None` no schema e validou com
`model_dump(exclude_none=True)`. O router NAO usa `exclude_none`: usa
`exclude_unset`, que remove o que nao foi ENVIADO — e a tool envia tudo. O
resultado era `setattr(lead, "nome", None)` contra uma coluna `nullable=False`:
500 com a transacao abortada, PIOR que o 422 original. O teste passava verde
sobre um comportamento que nao existia em producao. Testar a rota e o que
impede a repeticao disso.

Rodar:  python tests/test_n8n_contract_lead_update.py
"""
import io
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "n8n_contract_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

sys.path.insert(0, str(ROOT))
os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["SEED_INITIAL_ADMIN"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.models.lead import Lead  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402

Base.metadata.create_all(bind=engine)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover
    pass

falhas = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        falhas.append(msg)


class _Usuario:
    id = 1
    email = "n8n@local"
    nome = "Integracao n8n"
    role = UserRole.ADMIN
    is_active = True


main.app.dependency_overrides[get_current_user] = lambda: _Usuario()
client = TestClient(main.app)


# ─── 0. os corpos vem do EXPORT, nao da minha memoria ────────────────────
print("0) os corpos testados sao os do export de producao")

EXPORT = ROOT / "n8n" / "workflows" / "live_exports" / "20260825_fase2" / "gerenciador_leads.json"
check(EXPORT.exists(), f"export versionado em {EXPORT.relative_to(ROOT).as_posix()}")

wf = json.load(io.open(EXPORT, encoding="utf-8"))
nodes = {n["name"]: n for n in wf["nodes"]}
atualizar = nodes.get("Tool Atualizar Lead")
check(atualizar is not None, "node 'Tool Atualizar Lead' presente no export")

template = atualizar["parameters"]["jsonBody"].lstrip("=")
campos_da_tool = sorted(set(re.findall(r'"(\w+)"\s*:', template)))
check("nome" in campos_da_tool,
      f"a tool manda `nome` em TODA chamada ({len(campos_da_tool)} campos)")

# O corpo EXATO que a tool produz quando nada novo foi coletado: cada
# {placeholder} vira "" (string) ou {} (os dois campos JSON).
CORPO_VAZIO = json.loads(
    re.sub(r'"\{(\w+)\}"', '""', template).replace("{datas_destinos}", "{}")
       .replace("{dias_por_destino}", "{}")
)
check(CORPO_VAZIO.get("nome") == "",
      "o corpo reconstruido do template manda nome vazio")


# ─── 1. um lead de verdade, e o PUT que a tool faria ─────────────────────
print()
print("1) PUT /api/leads/{id} com o corpo real da tool")

s = SessionLocal()
try:
    lead = Lead(nome="Joao Original", whatsapp="5548988711776",
                email="joao@example.com", destinos=["Atacama"],
                status_venda="em_negociacao", is_active=True,
                campos_personalizados={})
    s.add(lead)
    s.commit()
    s.refresh(lead)
    LEAD_ID = lead.id
finally:
    s.close()

r = client.put(f"/api/leads/{LEAD_ID}", json=CORPO_VAZIO)
check(r.status_code == 200,
      f"o corpo real da tool devolve 200 (veio {r.status_code}: {r.text[:180]})")

s = SessionLocal()
try:
    depois = s.query(Lead).filter(Lead.id == LEAD_ID).first()
    check(depois is not None, "o lead continua existindo")
    check(depois.nome == "Joao Original",
          f"o NOME NAO foi apagado (esta {depois.nome!r})")
    check(depois.status_venda == "em_negociacao",
          f"status_venda NOT NULL preservado (esta {depois.status_venda!r})")
    check(depois.is_active is True, "is_active NOT NULL preservado")
    check(depois.campos_personalizados is not None,
          "campos_personalizados NOT NULL preservado")
finally:
    s.close()


# ─── 2. o dado NOVO tem que entrar ───────────────────────────────────────
print()
print("2) o que a Bia coletou de fato e gravado")

corpo = dict(CORPO_VAZIO)
corpo.update({"email": "novo@example.com", "num_viajantes": "3",
              "data_chegada": "2026-09-10", "destinos": "Atacama, Uyuni"})
r = client.put(f"/api/leads/{LEAD_ID}", json=corpo)
check(r.status_code == 200, f"200 no update com dados novos (veio {r.status_code})")

s = SessionLocal()
try:
    d = s.query(Lead).filter(Lead.id == LEAD_ID).first()
    check(d.email == "novo@example.com", f"email gravado (esta {d.email!r})")
    check(d.num_viajantes == 3, f"num_viajantes gravado (esta {d.num_viajantes!r})")
    check(str(d.data_chegada) == "2026-09-10", f"data gravada (esta {d.data_chegada!r})")
    check(d.destinos == ["Atacama", "Uyuni"], f"destinos gravados (esta {d.destinos!r})")
    check(d.nome == "Joao Original", "e o nome continua intacto no mesmo update")
finally:
    s.close()


# ─── 3. nome NOVO de verdade continua sobrescrevendo ─────────────────────
print()
print("3) nome de verdade continua sendo gravado")

corpo = dict(CORPO_VAZIO)
corpo["nome"] = "Joao Pedro Baldo"
r = client.put(f"/api/leads/{LEAD_ID}", json=corpo)
check(r.status_code == 200, "200 quando ha nome novo")

s = SessionLocal()
try:
    d = s.query(Lead).filter(Lead.id == LEAD_ID).first()
    check(d.nome == "Joao Pedro Baldo", f"nome atualizado (esta {d.nome!r})")
finally:
    s.close()


# ─── 4. limpar campo ANULAVEL continua possivel ──────────────────────────
print()
print("4) a correcao nao tirou a capacidade de LIMPAR campo anulavel")

r = client.put(f"/api/leads/{LEAD_ID}", json={"email": None})
check(r.status_code == 200, f"200 ao limpar email (veio {r.status_code})")
s = SessionLocal()
try:
    d = s.query(Lead).filter(Lead.id == LEAD_ID).first()
    check(d.email is None, f"email foi realmente limpo (esta {d.email!r})")
    check(d.nome == "Joao Pedro Baldo", "nome intacto")
finally:
    s.close()


# ─── 5. a guarda e derivada do MODELO, nao de lista escrita a mao ────────
print()
print("5) a guarda acompanha o modelo")

fonte = (ROOT / "app" / "routers" / "leads.py").read_text(encoding="utf-8")
check("Lead.__table__.columns if not c.nullable" in fonte,
      "o conjunto de colunas protegidas e derivado do modelo")
check('"nome"' not in fonte.split("_nao_anulaveis")[1][:400],
      "nenhuma lista de campos escrita a mao dentro da guarda")

nn = {c.name for c in Lead.__table__.columns if not c.nullable}
check({"nome", "status_venda", "is_active", "campos_personalizados"} <= nn,
      f"as colunas NOT NULL do modelo sao {sorted(nn)}")


# ─── 6. POST /api/leads: o outro lado do contrato ────────────────────────
print()
print("6) POST /api/leads aceita o corpo do Gerenciador e o do Formulario")

# Gerenciador: numeros como STRING, destinos como STRING separada por virgula.
r = client.post("/api/leads", json={
    "nome": "Maria", "whatsapp": "5548988711777", "destinos": "Atacama, Uyuni",
    "email": "", "num_viajantes": "2", "num_criancas": "0",
    "idades_criancas": "", "data_chegada": "", "data_partida": "",
    "total_dias": "", "datas_destinos": {}, "dias_por_destino": {}})
check(r.status_code in (200, 201), f"Gerenciador: criado (veio {r.status_code}: {r.text[:150]})")
if r.status_code in (200, 201):
    body = r.json()
    check(body.get("destinos") == ["Atacama", "Uyuni"], "destinos string virou lista")
    check(body.get("num_viajantes") == 2, "num_viajantes string virou int")

# Formulario do site: numeros como NUMERO.
r = client.post("/api/leads", json={
    "nome": "Cliente Site", "whatsapp": "5548988711778", "destinos": "Atacama",
    "email": "site@example.com", "num_viajantes": 2, "num_criancas": 0,
    "data_chegada": "2026-10-01", "data_partida": "2026-10-08",
    "datas_destinos": {}, "dias_por_destino": {}})
check(r.status_code in (200, 201), f"Formulario: criado (veio {r.status_code}: {r.text[:150]})")


print()
main.app.dependency_overrides.clear()
if falhas:
    print(f"{len(falhas)} FALHA(S)")
    sys.exit(1)
print("OK: o CRM aceita e grava o que o n8n de producao manda de fato")
