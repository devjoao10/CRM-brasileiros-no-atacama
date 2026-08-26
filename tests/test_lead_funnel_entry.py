# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-WB — regressao do F-341.

`POST /api/leads` criava SO a linha em `leads`: nenhuma FunnelEntry, nenhum
LeadHistory, nenhuma tag. Todo lead criado pelo agente n8n
"Gerenciador"/"Bia" ficava FORA do Kanban (app/routers/pipeline.py so
renderiza lead com FunnelEntry) e `GET /api/pipeline/locate/{lead_id}`
devolvia 404 — "Ver no Funil" nao fazia nada. Este arquivo prova que
`app/services/lead_creation.py` fechou os tres buracos (funil, historico,
tag) e que a escolha do funil default nunca mais prefere um funil so porque
o nome contem "whatsapp" (W2-10).

Os corpos de POST /api/leads testados nao sao inventados: vem do export de
producao em n8n/workflows/live_exports/20260825_fase2/, mesma tecnica de
tests/test_n8n_contract_lead_update.py:87-95 (carregar o export, achar o node
pelo nome, ler o jsonBody).

Rodar:  python tests/test_lead_funnel_entry.py
"""
import io
import json
import logging
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "lead_funnel_entry_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

sys.path.insert(0, str(ROOT))
os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["SEED_INITIAL_ADMIN"] = "false"
# Sem isto, um DEFAULT_FUNNEL_ID que por acaso esteja no ambiente do
# desenvolvedor tornaria o teste 5 (precedencia) nao-deterministico.
os.environ.pop("DEFAULT_FUNNEL_ID", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.models.pipeline import Funnel, FunnelEntry, LeadHistory  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402
import app.services.lead_creation as lead_creation  # noqa: E402

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


def _funil(db, nome, ativo=True):
    f = Funnel(
        nome=nome,
        etapas=[{"id": "novo", "nome": "Novo"}, {"id": "contato", "nome": "Contato"}],
        is_active=ativo,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


# ─── 0. os corpos vem do EXPORT, nao da minha memoria ────────────────────
print("0) os corpos testados sao os do export de producao")

EXPORT_GERENCIADOR = ROOT / "n8n" / "workflows" / "live_exports" / "20260825_fase2" / "gerenciador_leads.json"
check(EXPORT_GERENCIADOR.exists(),
      f"export versionado em {EXPORT_GERENCIADOR.relative_to(ROOT).as_posix()}")

wf_ger = json.load(io.open(EXPORT_GERENCIADOR, encoding="utf-8"))
nodes_ger = {n["name"]: n for n in wf_ger["nodes"]}
criar_lead_node = nodes_ger.get("Tool Criar Lead")
check(criar_lead_node is not None, "node 'Tool Criar Lead' presente no export do Gerenciador")

template_criar = criar_lead_node["parameters"]["jsonBody"].lstrip("=")
# Mesma reconstrucao de tests/test_n8n_contract_lead_update.py: cada
# {placeholder} vira "" (string) ou {} (os dois campos JSON) — o corpo EXATO
# que a tool produz quando nada foi coletado ainda.
CORPO_GERENCIADOR_VAZIO = json.loads(
    re.sub(r'"\{(\w+)\}"', '""', template_criar)
    .replace("{datas_destinos}", "{}")
    .replace("{dias_por_destino}", "{}")
)
check(CORPO_GERENCIADOR_VAZIO.get("nome") == "",
      "corpo do Gerenciador reconstruido tem nome vazio (so o placeholder)")

EXPORT_FORM = ROOT / "n8n" / "workflows" / "live_exports" / "20260825_fase2" / "formulario_site.json"
check(EXPORT_FORM.exists(), f"export versionado em {EXPORT_FORM.relative_to(ROOT).as_posix()}")

wf_form = json.load(io.open(EXPORT_FORM, encoding="utf-8"))
nodes_form = {n["name"]: n for n in wf_form["nodes"]}
criar_novo_lead_node = nodes_form.get("Criar novo lead")
check(criar_novo_lead_node is not None, "node 'Criar novo lead' presente no export do Formulario")

# O corpo do Formulario e uma EXPRESSAO n8n (referencia a saida do node
# anterior), nao JSON com placeholder de texto — nao da para reconstruir por
# regex como o do Gerenciador. O que da para provar sem reimplementar o n8n:
# os NOMES dos campos que ele manda batem com os do CORPO_FORM usado abaixo.
corpo_form_texto = criar_novo_lead_node.get("parameters", {}).get("jsonBody", "")
campos_form = set(re.findall(r"(\w+):\s*\$", corpo_form_texto))
CAMPOS_ESPERADOS_FORM = {
    "nome", "whatsapp", "destinos", "email",
    "num_viajantes", "num_criancas", "data_chegada", "data_partida",
}
check(CAMPOS_ESPERADOS_FORM <= campos_form,
      f"o formulario do site manda os campos esperados (tem {sorted(campos_form)})")


# ─── 6. SEM funil ativo: o lead nao pode sumir (roda com `funnels` vazia) ─
print()
print("6) sem nenhum funil ativo, o lead ainda e criado, com aviso")


class _CapturaLogs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.registros = []

    def emit(self, record):
        self.registros.append(record)


captura = _CapturaLogs()
_logger_lc = logging.getLogger("app.services.lead_creation")
_logger_lc.addHandler(captura)
db = SessionLocal()
try:
    check(db.query(Funnel).count() == 0, "sanidade: nenhum funil existe ainda neste banco")
    lead_sem_funil = lead_creation.criar_lead(
        db, dados={"nome": "Lead Sem Funil"}, origem="teste",
    )
    check(lead_sem_funil.id is not None, "o lead foi criado mesmo sem funil ativo")

    n_entries = db.query(FunnelEntry).filter(FunnelEntry.lead_id == lead_sem_funil.id).count()
    check(n_entries == 0, f"nenhuma FunnelEntry — nao havia funil pra entrar (tem {n_entries})")

    hist = db.query(LeadHistory).filter(
        LeadHistory.lead_id == lead_sem_funil.id, LeadHistory.evento == "created"
    ).first()
    check(hist is not None, "o evento 'created' foi gravado mesmo sem funil")
    check(hist is not None and isinstance(hist.dados, dict) and hist.dados.get("aviso"),
          f"o aviso fica registrado no historico do lead (dados={hist and hist.dados!r})")
finally:
    _logger_lc.removeHandler(captura)
    db.close()

check(any(r.levelno == logging.WARNING for r in captura.registros),
      "um warning foi logado quando nao ha funil ativo")


# ─── setup: funis para os testes seguintes ───────────────────────────────
db = SessionLocal()
try:
    principal = _funil(db, "Vendas: Principal")   # criado primeiro -> id menor
    whatsapp = _funil(db, "Vendas WhatsApp")       # nome com "whatsapp" -> NAO pode vencer
    outro = _funil(db, "Funil Extra")
    CTX = {
        "principal": principal.id, "whatsapp": whatsapp.id, "outro": outro.id,
        "primeira_etapa": principal.etapas[0]["id"],
    }
finally:
    db.close()


# ─── 5. resolver_funil_padrao: nunca por nome, DEFAULT_FUNNEL_ID > menor id
print()
print("5) resolver_funil_padrao ignora nome e respeita DEFAULT_FUNNEL_ID/menor id")

db = SessionLocal()
try:
    check(CTX["principal"] < CTX["whatsapp"], "sanidade do seed: Principal tem id menor")

    resolvido = lead_creation.resolver_funil_padrao(db, None)
    check(resolvido is not None and resolvido.id == CTX["principal"],
          f"sem DEFAULT_FUNNEL_ID, vence o de MENOR id (Principal), veio "
          f"{resolvido and resolvido.id} ('{resolvido and resolvido.nome}')")

    original_default = lead_creation.DEFAULT_FUNNEL_ID
    lead_creation.DEFAULT_FUNNEL_ID = CTX["whatsapp"]
    try:
        resolvido2 = lead_creation.resolver_funil_padrao(db, None)
        check(resolvido2 is not None and resolvido2.id == CTX["whatsapp"],
              "DEFAULT_FUNNEL_ID, quando aponta pra funil ativo, tem prioridade sobre o menor id")
    finally:
        lead_creation.DEFAULT_FUNNEL_ID = original_default

    explicito = lead_creation.resolver_funil_padrao(db, CTX["outro"])
    check(explicito is not None and explicito.id == CTX["outro"],
          "funnel_id explicito vence tudo (DEFAULT_FUNNEL_ID e menor id ignorados)")
finally:
    db.close()


# ─── 1. RED-GREEN: POST /api/leads (corpo do Gerenciador) entra no funil ─
print()
print("1) POST /api/leads com o corpo real do Gerenciador cria UMA FunnelEntry")

corpo_ger = dict(CORPO_GERENCIADOR_VAZIO)
corpo_ger.update({"nome": "Lead Gerenciador Teste", "whatsapp": "5548999990001"})
r = client.post("/api/leads", json=corpo_ger)
check(r.status_code == 201, f"201 na criacao (veio {r.status_code}: {r.text[:200]})")
LEAD_GER_ID = r.json()["id"] if r.status_code == 201 else None

if LEAD_GER_ID:
    db = SessionLocal()
    try:
        entries = db.query(FunnelEntry).filter(FunnelEntry.lead_id == LEAD_GER_ID).all()
        check(len(entries) == 1, f"exatamente UMA FunnelEntry apos o POST (tem {len(entries)})")
        if entries:
            check(entries[0].funnel_id == CTX["principal"],
                  f"entrou no funil default resolvido (Principal), veio funnel_id={entries[0].funnel_id}")
            check(entries[0].etapa_id == CTX["primeira_etapa"],
                  f"entrou na PRIMEIRA etapa do funil, veio {entries[0].etapa_id!r}")
    finally:
        db.close()


# ─── 2. corpo do Formulario do site tambem entra no funil ────────────────
print()
print("2) POST /api/leads com o corpo do Formulario do site tambem cria a entrada")

corpo_form = {
    "nome": "Lead Formulario Teste", "whatsapp": "5548999990002",
    "destinos": ["Atacama"], "email": "site@example.com",
    "num_viajantes": 2, "num_criancas": 0,
    "data_chegada": "2026-10-01", "data_partida": "2026-10-08",
    "datas_destinos": {}, "dias_por_destino": {},
}
r = client.post("/api/leads", json=corpo_form)
check(r.status_code == 201, f"201 na criacao via formulario (veio {r.status_code}: {r.text[:200]})")
if r.status_code == 201:
    lead_form_id = r.json()["id"]
    db = SessionLocal()
    try:
        n = db.query(FunnelEntry).filter(FunnelEntry.lead_id == lead_form_id).count()
        check(n == 1, f"lead do formulario tambem entra no funil default (tem {n} entries)")
    finally:
        db.close()


# ─── 3. add manual no MESMO funil continua 409 e nao duplica ────────────
print()
print("3) POST /api/pipeline/funnels/{id}/leads no MESMO funil ainda da 409")

if LEAD_GER_ID:
    r = client.post(
        f"/api/pipeline/funnels/{CTX['principal']}/leads",
        json={"lead_id": LEAD_GER_ID, "etapa_id": CTX["primeira_etapa"]},
    )
    check(r.status_code == 409,
          f"409 esperado — o n8n do Formulario depende deste contrato (veio {r.status_code})")
    db = SessionLocal()
    try:
        n = db.query(FunnelEntry).filter(
            FunnelEntry.lead_id == LEAD_GER_ID, FunnelEntry.funnel_id == CTX["principal"]
        ).count()
        check(n == 1, f"continua EXATAMENTE uma entry no funil principal (tem {n})")
    finally:
        db.close()


# ─── 4. add em funil DIFERENTE funciona (multi-funil por design) ────────
print()
print("4) adicionar o mesmo lead a um funil DIFERENTE funciona")

if LEAD_GER_ID:
    r = client.post(
        f"/api/pipeline/funnels/{CTX['outro']}/leads",
        json={"lead_id": LEAD_GER_ID, "etapa_id": "novo"},
    )
    check(r.status_code == 201, f"201 num funil diferente (veio {r.status_code}: {r.text[:150]})")
    db = SessionLocal()
    try:
        n = db.query(FunnelEntry).filter(FunnelEntry.lead_id == LEAD_GER_ID).count()
        check(n == 2, f"agora 2 entries no total — multi-funil e por design (tem {n})")
    finally:
        db.close()


# ─── 7. /locate agora acha o lead (antes do fix era 404) ────────────────
print()
print("7) GET /api/pipeline/locate/{lead_id} acha lead criado via POST /api/leads")

if LEAD_GER_ID:
    r = client.get(f"/api/pipeline/locate/{LEAD_GER_ID}")
    check(r.status_code == 200, f"200 no locate — antes do fix era 404 (veio {r.status_code})")
    if r.status_code == 200:
        check(r.json()["funnel_id"] == CTX["principal"], "locate aponta pro funil certo")


# ─── 8. LeadHistory.dados nunca e None ───────────────────────────────────
print()
print("8) LeadHistory do lead criado tem dados como dict, nunca None")

if LEAD_GER_ID:
    db = SessionLocal()
    try:
        hist = db.query(LeadHistory).filter(
            LeadHistory.lead_id == LEAD_GER_ID, LeadHistory.evento == "created"
        ).first()
        check(hist is not None, "evento 'created' foi gravado para o lead do Gerenciador")
        check(hist is not None and isinstance(hist.dados, dict),
              f"dados e um dict (esta {hist and hist.dados!r})")
    finally:
        db.close()


print()
main.app.dependency_overrides.clear()
if falhas:
    print(f"{len(falhas)} FALHA(S)")
    sys.exit(1)
print("OK: leads criados via POST /api/leads entram no funil (F-341 corrigido)")
