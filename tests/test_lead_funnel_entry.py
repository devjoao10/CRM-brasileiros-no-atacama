# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-WB (F-341) + AUDIT-2026-08-WF2 — caminho UNICO de criacao de
lead do CRM, e a escolha do funil/etapa default sem depender de acidente de
id ou de posicao na lista.

F-341 (contexto historico): antes daquela correcao, `POST /api/leads`
(app/routers/leads.py) criava SO a linha em `leads`: nenhuma FunnelEntry,
nenhum LeadHistory, nenhuma tag. Todo lead criado pelo agente n8n
"Gerenciador"/"Bia" ficava FORA do Kanban — `app/routers/pipeline.py` so
renderiza lead que tem FunnelEntry, e `GET /api/pipeline/locate/{lead_id}`
devolvia 404, entao "Ver no Funil" nao fazia nada. `app/services/lead_creation.py`
passou a ser o UNICO lugar do CRM (app/) que decide o que acontece quando um
lead nasce: funil default, entrada no funil, evento no historico e tag de
origem.

WF2 (esta reescrita): a resolucao do funil/etapa default deixou de ter
QUALQUER fallback por ordem de id ou por posicao na lista:

  - `resolver_funil_padrao` NAO cai mais no "funil ATIVO de MENOR id". A
    precedencia agora e `funnel_id` explicito > `DEFAULT_FUNNEL_ID` (falha
    ALTO se configurado errado — nunca cai adiante) > funil ATIVO cujo NOME
    normaliza igual a `DEFAULT_FUNNEL_NOME` ("Vendas: Principal"). Nome
    ambiguo (dois funis ativos normalizando igual) tambem falha alto — nunca
    escolhe um dos dois.
  - `resolver_etapa_inicial` NAO cai mais em `etapas[0]` por padrao. Agora
    procura a etapa "Sem Contato" (por `id` OU `nome`) antes de recorrer a
    primeira posicao da lista.

Este arquivo derruba de proposito as duas premissas que a versao anterior do
teste assumia como verdade estavel: a antiga secao "5" ("sem
DEFAULT_FUNNEL_ID, vence o de MENOR id") semeava "Vendas: Principal" DE
PROPOSITO PRIMEIRO "para ter id menor" — e a antiga secao "1" verificava que
o lead entrava na PRIMEIRA etapa da lista. As duas eram exatamente a
fragilidade que esta auditoria remove: regra de negocio amarrada a um
acidente de historico (quem foi criado primeiro / em que posicao esta na
lista), nao ao nome real do funil nem da etapa. As secoes novas abaixo
(marcadas AUDIT-2026-08-WF2) invertem a premissa: semeiam os funis
distratores PRIMEIRO (id menor) e "Vendas: Principal" por ULTIMO (id maior,
com uma etapa ANTES de "Sem Contato" na lista) — e provam que ele ainda
vence, porque agora a resolucao e por NOME de funil e por NOME/id de etapa,
nunca por posicao.

Os corpos de POST /api/leads testados nao sao inventados: vem do export de
producao em n8n/workflows/live_exports/20260826_wa/ (mesma tecnica de
tests/test_n8n_contract_lead_update.py — carregar o export, achar o node
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
# Determinismo: nenhuma das 3 pode vir do .env do desenvolvedor. As duas
# ultimas nao sao usadas por nenhum teste aqui (sempre confiamos no default
# "Vendas: Principal" / "Sem Contato"), mas um valor customizado vazando do
# ambiente local tornaria a resolucao por nome nao-deterministica.
os.environ.pop("DEFAULT_FUNNEL_ID", None)
os.environ.pop("DEFAULT_FUNNEL_NOME", None)
os.environ.pop("DEFAULT_ETAPA_NOME", None)

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.models.lead import Lead  # noqa: E402
from app.models.pipeline import Funnel, FunnelEntry, LeadHistory  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402
import app.services.lead_creation as lead_creation  # noqa: E402
from app.services import ai_tools  # noqa: E402

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
    """Override HTTP de get_current_user — admin, usado por todas as chamadas via `client`."""
    id = 1
    email = "n8n@local"
    nome = "Integracao n8n"
    role = UserRole.ADMIN
    is_active = True


class _UsuarioIA:
    """Contexto separado (contextvars) para as chamadas DIRETAS a app/services/ai_tools.py —
    nao tem nada a ver com o override de get_current_user acima. Mesmo formato de
    tests/test_ai_tool_hardening.py:_FakeUser (role como string simples: _require_ai_user_context
    so confere user_id, nunca o valor de role)."""
    id = 4321
    email = "perpetua-teste@local"
    role = "agent"


main.app.dependency_overrides[get_current_user] = lambda: _Usuario()
client = TestClient(main.app)


def _funil(db, nome, etapas, ativo=True):
    f = Funnel(nome=nome, etapas=etapas, is_active=ativo)
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


def _assert_uma_entrada(db, lead_id, funnel_id_esperado, etapa_id_esperado, rotulo):
    """Confere exatamente 1 FunnelEntry para `lead_id`, no funil e etapa esperados."""
    entries = db.query(FunnelEntry).filter(FunnelEntry.lead_id == lead_id).all()
    check(len(entries) == 1, f"{rotulo}: exatamente UMA FunnelEntry (tem {len(entries)})")
    if entries:
        check(entries[0].funnel_id == funnel_id_esperado,
              f"{rotulo}: funnel_id esperado {funnel_id_esperado}, veio {entries[0].funnel_id}")
        check(entries[0].etapa_id == etapa_id_esperado,
              f"{rotulo}: etapa_id esperado {etapa_id_esperado!r}, veio {entries[0].etapa_id!r}")
    return entries


# ─── 0. os corpos vem do EXPORT, nao da minha memoria ────────────────────
print("0) os corpos testados sao os do export de producao")

EXPORT_GERENCIADOR = ROOT / "n8n" / "workflows" / "live_exports" / "20260826_wa" / "gerenciador_leads.json"
check(EXPORT_GERENCIADOR.exists(),
      f"export versionado em {EXPORT_GERENCIADOR.relative_to(ROOT).as_posix()}")

wf_ger = json.load(io.open(EXPORT_GERENCIADOR, encoding="utf-8"))
nodes_ger = {n["name"]: n for n in wf_ger["nodes"]}
criar_lead_node = nodes_ger.get("Tool Criar Lead")
check(criar_lead_node is not None, "node 'Tool Criar Lead' presente no export do Gerenciador")

template_criar = criar_lead_node["parameters"]["jsonBody"].lstrip("=")
# Cada {placeholder} vira "" (string) ou {} (os dois campos JSON) — o corpo EXATO
# que a tool produz quando nada foi coletado ainda.
CORPO_GERENCIADOR_VAZIO = json.loads(
    re.sub(r'"\{(\w+)\}"', '""', template_criar)
    .replace("{datas_destinos}", "{}")
    .replace("{dias_por_destino}", "{}")
)
check(CORPO_GERENCIADOR_VAZIO.get("nome") == "",
      "corpo do Gerenciador reconstruido tem nome vazio (so o placeholder)")

EXPORT_FORM = ROOT / "n8n" / "workflows" / "live_exports" / "20260826_wa" / "formulario_site.json"
check(EXPORT_FORM.exists(), f"export versionado em {EXPORT_FORM.relative_to(ROOT).as_posix()}")

wf_form = json.load(io.open(EXPORT_FORM, encoding="utf-8"))
nodes_form = {n["name"]: n for n in wf_form["nodes"]}
criar_novo_lead_node = nodes_form.get("Criar novo lead")
check(criar_novo_lead_node is not None, "node 'Criar novo lead' presente no export do Formulario")

# O corpo do Formulario e uma EXPRESSAO n8n (referencia a saida do node
# anterior), nao JSON com placeholder de texto — nao da para reconstruir por
# regex como o do Gerenciador. O que da para provar sem reimplementar o n8n:
# os NOMES dos campos que ele manda batem com os do CORPO_FORM usado abaixo.
# AUDIT-2026-08-WF2 — o node ainda NAO manda `funnel_nome`/`etapa_id` (ver
# secao 6): confirma que o export atual e o estado PRE-M11.
corpo_form_texto = criar_novo_lead_node.get("parameters", {}).get("jsonBody", "")
campos_form = set(re.findall(r"(\w+):\s*\$", corpo_form_texto))
CAMPOS_ESPERADOS_FORM = {
    "nome", "whatsapp", "destinos", "email",
    "num_viajantes", "num_criancas", "data_chegada", "data_partida",
}
check(CAMPOS_ESPERADOS_FORM <= campos_form,
      f"o formulario do site manda os campos esperados (tem {sorted(campos_form)})")
check("funnel_nome" not in criar_novo_lead_node.get("parameters", {}).get("url", "")
      and "qs" not in criar_novo_lead_node.get("parameters", {}),
      "o node do Formulario ainda NAO manda funnel_nome/etapa_id — e o estado PRE-M11 (secao 6)")


# ─── 1. SEM funil ativo: o lead nao pode sumir (roda com `funnels` vazia) ─
# Ainda valido sob o novo contrato: resolver_funil_padrao tambem devolve
# None+ERROR quando nao ha candidato algum. Roda ANTES de qualquer funil
# existir porque testa exatamente essa precondicao.
print()
print("1) sem nenhum funil ativo, o lead ainda e criado, com aviso")


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


# ─── 2. ORDEM DE CRIACAO ADVERSA — a prova principal da reescrita ────────
# AUDIT-2026-08-WF2. Antes: a antiga secao "5" criava "Vendas: Principal"
# PRIMEIRO "para ter id menor" e a antiga secao "1" checava que o lead caia
# em `etapas[0]`. As duas premissas eram exatamente o acidente de historico
# que este audit remove. Aqui a ordem e DELIBERADAMENTE adversa: os 3 funis
# distratores nascem primeiro (id MENOR), "Vendas: Principal" nasce por
# ULTIMO (id MAIOR) e tem uma etapa ANTES de "Sem Contato" na lista (entao
# `etapas[0]` tambem daria resposta errada). Se o lead ainda cai em
# Principal/Sem Contato, so pode ser por NOME — nunca por id nem por posicao.
print()
print("2) ordem de criacao ADVERSA: 'Vendas: Principal' vence por NOME mesmo sendo o funil de MAIOR id")

db = SessionLocal()
try:
    # Formulario tem que existir com este nome exato e com a etapa
    # 'nova_oportunidade' tambem pela secao 6 (M11) — reaproveitado dali pra
    # ca, ele nao muda mais pelo resto do arquivo.
    formulario = _funil(db, "Vendas: Formulário", [
        {"id": "nova_oportunidade", "nome": "Nova Oportunidade"},
        {"id": "convertido", "nome": "Convertido"},
    ])
    # nome CONTEM "whatsapp" — precisa continuar perdendo (_normalizar e
    # igualdade, nunca substring).
    whatsapp = _funil(db, "Vendas WhatsApp", [{"id": "novo", "nome": "Novo"}])
    extra = _funil(db, "Funil Extra", [{"id": "novo", "nome": "Novo"}])

    CTX = {"formulario": formulario.id, "whatsapp": whatsapp.id, "extra": extra.id}
    check(CTX["formulario"] < CTX["whatsapp"] < CTX["extra"],
          f"sanidade do seed: Formulario({CTX['formulario']}) < WhatsApp({CTX['whatsapp']}) "
          f"< Extra({CTX['extra']}) em id")

    # 'Sem Contato' NAO e etapas[0] de proposito — 'triagem' vem antes dela.
    principal = _funil(db, "Vendas: Principal", [
        {"id": "triagem", "nome": "Triagem"},
        {"id": "sem_contato", "nome": "Sem Contato"},
        {"id": "qualificado", "nome": "Qualificado"},
    ])
    CTX["principal"] = principal.id
    check(CTX["principal"] > CTX["extra"] > CTX["whatsapp"] > CTX["formulario"],
          f"sanidade do seed: Principal (id {CTX['principal']}) e o de MAIOR id, criado por ULTIMO — "
          f"Formulario={CTX['formulario']}, WhatsApp={CTX['whatsapp']}, Extra={CTX['extra']}")
finally:
    db.close()

# As duas grafias da etapa "Sem Contato" que realmente aparecem no dominio
# (schemas/pipeline.py aceita as duas — o etapa_id real de producao NAO e
# conhecivel a partir deste repo). Reaplicadas na MESMA "Vendas: Principal"
# (id fixo, ja e o maior — reatribuir `etapas` a uma lista NOVA e o bastante
# pro SQLAlchemy detectar a mudanca na Column(JSON), sem precisar de
# flag_modified).
VARIANTES_SEM_CONTATO = [
    {"id": "sem_contato", "nome": "Sem Contato"},
    {"id": "Sem Contato", "nome": "Sem Contato"},
]

leads_headline = []
for i, etapa_sem_contato in enumerate(VARIANTES_SEM_CONTATO, start=1):
    print(f"   -- variante {i}: etapa gravada como {etapa_sem_contato!r}")
    db = SessionLocal()
    try:
        principal = db.query(Funnel).filter(Funnel.id == CTX["principal"]).first()
        principal.etapas = [
            {"id": "triagem", "nome": "Triagem"},
            etapa_sem_contato,
            {"id": "qualificado", "nome": "Qualificado"},
        ]
        db.commit()
    finally:
        db.close()

    corpo = dict(CORPO_GERENCIADOR_VAZIO)
    corpo.update({"nome": f"Lead Headline {i}", "whatsapp": f"554899999000{i}"})
    r = client.post("/api/leads", json=corpo)
    check(r.status_code == 201, f"variante {i}: 201 na criacao (veio {r.status_code}: {r.text[:200]})")
    lead_id = r.json()["id"] if r.status_code == 201 else None
    leads_headline.append(lead_id)

    if lead_id:
        db = SessionLocal()
        try:
            entries = db.query(FunnelEntry).filter(FunnelEntry.lead_id == lead_id).all()
            check(len(entries) == 1, f"variante {i}: exatamente UMA FunnelEntry (tem {len(entries)})")
            if entries:
                check(entries[0].funnel_id == CTX["principal"],
                      f"variante {i}: entrou em 'Vendas: Principal' (id {CTX['principal']}, o MAIOR) — "
                      f"NAO em Formulario (id {CTX['formulario']}, o menor) nem em WhatsApp "
                      f"(id {CTX['whatsapp']}, nome contem 'whatsapp') — veio funnel_id={entries[0].funnel_id}")
                check(entries[0].etapa_id == etapa_sem_contato["id"],
                      f"variante {i}: etapa resolvida e a PROPRIA id da etapa 'Sem Contato' gravada "
                      f"(nao 'triagem' == etapas[0]) — esperado {etapa_sem_contato['id']!r}, "
                      f"veio {entries[0].etapa_id!r}")
        finally:
            db.close()

LEAD_GER_ID = leads_headline[-1]
# etapa 'Sem Contato' que fica valendo dai em diante (ultima variante do loop).
ETAPA_SEM_CONTATO_ATUAL = VARIANTES_SEM_CONTATO[-1]["id"]


# ─── 3. resolver_funil_padrao: precedencia de DEFAULT_FUNNEL_ID ──────────
# AUDIT-2026-08-WF2. 3a e a unica sub-prova que tambem existia no arquivo
# antigo ("funnel_id explicito vence tudo") — continua verdadeira e por isso
# fica. 3b em diante e novo: antes a comparacao era "DEFAULT_FUNNEL_ID vence
# o MENOR id"; agora nao ha mais "menor id" nenhum — a comparacao certa e
# "DEFAULT_FUNNEL_ID vence a resolucao por NOME" (Principal).
print()
print("3) DEFAULT_FUNNEL_ID: vence tudo quando ativo; id inexistente ou inativo -> None (nunca cai noutro funil)")

db = SessionLocal()
try:
    original_default = lead_creation.DEFAULT_FUNNEL_ID

    # 3a. funnel_id EXPLICITO vence tudo — mesmo sem DEFAULT_FUNNEL_ID e com
    # 'Vendas: Principal' perfeitamente resolvivel por nome.
    explicito = lead_creation.resolver_funil_padrao(db, CTX["extra"])
    check(explicito is not None and explicito.id == CTX["extra"],
          f"funnel_id explicito vence tudo (veio {explicito and explicito.id})")

    # 3b. DEFAULT_FUNNEL_ID ATIVO vence a resolucao por NOME — aponta pra
    # 'Funil Extra', que NAO e o nome-alvo, e ainda assim vence Principal.
    lead_creation.DEFAULT_FUNNEL_ID = CTX["extra"]
    try:
        resolvido = lead_creation.resolver_funil_padrao(db, None)
        check(resolvido is not None and resolvido.id == CTX["extra"],
              f"DEFAULT_FUNNEL_ID ativo vence a resolucao por nome (Principal seria o nome-match) — "
              f"veio {resolvido and resolvido.id} ('{resolvido and resolvido.nome}')")
    finally:
        lead_creation.DEFAULT_FUNNEL_ID = original_default

    # 3c. DEFAULT_FUNNEL_ID apontando pra id que NAO EXISTE
    id_inexistente = 10_000_000  # bem acima de qualquer id gerado neste teste
    lead_creation.DEFAULT_FUNNEL_ID = id_inexistente
    try:
        resolvido = lead_creation.resolver_funil_padrao(db, None)
        check(resolvido is None, f"DEFAULT_FUNNEL_ID inexistente -> None (veio {resolvido})")
    finally:
        lead_creation.DEFAULT_FUNNEL_ID = original_default

    # 3d. DEFAULT_FUNNEL_ID apontando pra funil INATIVO
    inativo = _funil(db, "Funil Inativo WF2", [{"id": "x", "nome": "X"}], ativo=False)
    lead_creation.DEFAULT_FUNNEL_ID = inativo.id
    try:
        resolvido = lead_creation.resolver_funil_padrao(db, None)
        check(resolvido is None, f"DEFAULT_FUNNEL_ID apontando pra funil INATIVO -> None (veio {resolvido})")
    finally:
        lead_creation.DEFAULT_FUNNEL_ID = original_default

    # 3e. O CASO CRUCIAL: com DEFAULT_FUNNEL_ID invalido, o lead criado NAO
    # pode cair silenciosamente em NENHUM outro funil (nem em Principal por
    # nome). So checar "nao esta em Principal" passaria com um fallback
    # silencioso pra outro funil qualquer — por isso a asserção é sobre o
    # TOTAL de entries do lead, nao so sobre Principal.
    lead_creation.DEFAULT_FUNNEL_ID = id_inexistente
    try:
        lead_invalido = lead_creation.criar_lead(
            db, dados={"nome": "Lead DEFAULT_FUNNEL_ID Invalido", "whatsapp": "5548999990003"},
            origem="teste",
        )
        n_entries = db.query(FunnelEntry).filter(FunnelEntry.lead_id == lead_invalido.id).count()
        check(n_entries == 0,
              f"DEFAULT_FUNNEL_ID invalido: o lead NAO esta em NENHUM funil, nem em Principal por "
              f"nome (tem {n_entries} entries no total)")
    finally:
        lead_creation.DEFAULT_FUNNEL_ID = original_default
finally:
    db.close()


# ─── 4. nome ambiguo: dois funis ATIVOS que normalizam igual ────────────
print()
print("4) nome ambiguo: dois funis ATIVOS que normalizam igual -> nenhum resolve (ambiguidade e recusada)")

db = SessionLocal()
try:
    antes = lead_creation.resolver_funil_padrao(db, None)
    check(antes is not None and antes.id == CTX["principal"],
          "sanidade: antes do gemeo, 'Vendas: Principal' resolve sozinho")

    # funnels.nome e UNIQUE (app/models/pipeline.py:15) — os dois nomes tem
    # que diferir de verdade como STRING (senao o proprio INSERT falharia
    # pela constraint), so colidem DEPOIS de normalizados (_normalizar troca
    # '_' por espaco, colapsa espacos e faz lower() — o ':' fica intacto).
    gemeo = _funil(db, "vendas:  principal", [{"id": "x", "nome": "X"}])
    check(lead_creation._normalizar(gemeo.nome) == lead_creation._normalizar("Vendas: Principal"),
          "sanidade: o gemeo normaliza igual a 'Vendas: Principal' apesar de ser uma string diferente")

    ambiguo = lead_creation.resolver_funil_padrao(db, None)
    check(ambiguo is None, f"com o gemeo ativo, resolver_funil_padrao devolve None (veio {ambiguo})")

    lead_ambiguo = lead_creation.criar_lead(
        db, dados={"nome": "Lead Nome Ambiguo", "whatsapp": "5548999990004"}, origem="teste",
    )
    n_entries = db.query(FunnelEntry).filter(FunnelEntry.lead_id == lead_ambiguo.id).count()
    check(n_entries == 0, f"lead criado durante a ambiguidade fica sem FunnelEntry (tem {n_entries})")

    # Restaura a resolucao singular pro resto do arquivo (secoes 5 e 6
    # dependem de 'Vendas: Principal' resolver sozinha outra vez).
    gemeo.is_active = False
    db.commit()
    restaurado = lead_creation.resolver_funil_padrao(db, None)
    check(restaurado is not None and restaurado.id == CTX["principal"],
          f"sanidade: apos desativar o gemeo, 'Vendas: Principal' volta a resolver sozinha "
          f"(veio {restaurado and restaurado.id})")
finally:
    db.close()


# ─── 5. cada origem comercial cai em Principal/Sem Contato ───────────────
# A ordem adversa da secao 2 continua valendo (Principal e o funil de MAIOR
# id do banco, com uma etapa antes de 'Sem Contato').
print()
print("5) cada origem comercial cai em 'Vendas: Principal' / 'Sem Contato'")

# 5a. POST /api/leads (agente n8n Gerenciador/Bia) — corpo real do export.
corpo_ger2 = dict(CORPO_GERENCIADOR_VAZIO)
corpo_ger2.update({"nome": "Lead Origem API", "whatsapp": "5548999990005"})
r = client.post("/api/leads", json=corpo_ger2)
check(r.status_code == 201, f"POST /api/leads: 201 (veio {r.status_code}: {r.text[:200]})")
if r.status_code == 201:
    db = SessionLocal()
    try:
        _assert_uma_entrada(db, r.json()["id"], CTX["principal"], ETAPA_SEM_CONTATO_ATUAL,
                             "POST /api/leads (Gerenciador)")
    finally:
        db.close()

# 5b. app/services/ai_tools.py::create_lead — regressao do fix "Fix
# ai_tools.py create_lead missing funnel entry" (Claude App). A ferramenta
# delega pra criar_lead(...) desde a correcao; esta e a prova de que
# continua delegando (e nao voltou a montar um Lead() cru sem funil).
ai_tools.set_ai_user_context(_UsuarioIA())
try:
    resultado_ia = json.loads(ai_tools.create_lead(nome="Lead Origem IA", whatsapp="5548999990006"))
finally:
    ai_tools.clear_ai_user_context()

check(resultado_ia.get("success") is True, f"ai_tools.create_lead reporta sucesso (veio {resultado_ia})")
if resultado_ia.get("success"):
    db = SessionLocal()
    try:
        _assert_uma_entrada(db, resultado_ia["lead_id"], CTX["principal"], ETAPA_SEM_CONTATO_ATUAL,
                             "ai_tools.create_lead")
    finally:
        db.close()

# 5c. POST /api/leads/import (CSV) — mesmo caminho (criar_lead por linha).
csv_bytes = "nome,whatsapp\r\nLead Origem Import,5548999990007\r\n".encode("utf-8")
r = client.post("/api/leads/import", files={"file": ("leads.csv", csv_bytes, "text/csv")})
check(r.status_code == 200, f"POST /api/leads/import: 200 (veio {r.status_code}: {r.text[:200]})")
if r.status_code == 200:
    body = r.json()
    check(body.get("importados") == 1, f"1 linha importada (veio {body})")
    db = SessionLocal()
    try:
        lead_import = db.query(Lead).filter(Lead.whatsapp == "5548999990007").first()
        check(lead_import is not None, "o lead importado existe na tabela leads")
        if lead_import:
            _assert_uma_entrada(db, lead_import.id, CTX["principal"], ETAPA_SEM_CONTATO_ATUAL,
                                 "POST /api/leads/import")
    finally:
        db.close()


# ─── 6. Formulario entra no PROPRIO funil quando pede por nome (M11) ─────
print()
print("6) Formulario entra no PROPRIO funil quando o workflow pede por nome (M11) — e SO nele")

# Corpo equivalente ao que o node "Criar novo lead" do export do Formulario
# produz (a expressao n8n nao da pra reconstruir por regex — ver secao 0;
# os NOMES dos campos ja foram conferidos la).
corpo_form = {
    "nome": "Lead Formulario M11", "whatsapp": "5548999990008",
    "destinos": ["Atacama"], "email": "form-m11@example.com",
    "num_viajantes": 2, "num_criancas": 0,
    "data_chegada": "2026-10-01", "data_partida": "2026-10-08",
    "datas_destinos": {}, "dias_por_destino": {},
}

# 6a. COM funnel_nome + etapa_id — o estado DEPOIS de aplicar M11
# (docs/audit/N8N_MANUAL_CHANGES.md): a entry nasce SO no funil do
# Formulario, nunca em Principal.
r = client.post(
    "/api/leads",
    params={"funnel_nome": "Vendas: Formulário", "etapa_id": "nova_oportunidade"},
    json=corpo_form,
)
check(r.status_code == 201, f"201 com funnel_nome='Vendas: Formulário' (veio {r.status_code}: {r.text[:200]})")
if r.status_code == 201:
    db = SessionLocal()
    try:
        _assert_uma_entrada(db, r.json()["id"], CTX["formulario"], "nova_oportunidade",
                             "POST /api/leads?funnel_nome=Vendas: Formulário")
    finally:
        db.close()

# 6b. SEM os dois parametros — o estado ATUAL do workflow, ANTES do M11 ser
# aplicado a mao. Documenta o defeito que o M11 existe pra corrigir: o mesmo
# corpo cai em Principal (o padrao), nao em Formulario — e se o workflow
# tambem chamasse POST /api/pipeline/funnels/{id}/leads em seguida (como
# fazia antes do F-341), o lead ganharia DUAS entradas ate o M11 ser
# aplicado (ver M11 em docs/audit/N8N_MANUAL_CHANGES.md).
corpo_form_sem_params = dict(corpo_form)
corpo_form_sem_params.update({"nome": "Lead Formulario Pre-M11", "whatsapp": "5548999990009"})
r = client.post("/api/leads", json=corpo_form_sem_params)
check(r.status_code == 201, f"201 sem funnel_nome (veio {r.status_code}: {r.text[:200]})")
if r.status_code == 201:
    db = SessionLocal()
    try:
        _assert_uma_entrada(db, r.json()["id"], CTX["principal"], ETAPA_SEM_CONTATO_ATUAL,
                             "POST /api/leads sem funnel_nome (pre-M11) — cai em Principal, nao em Formulario")
    finally:
        db.close()

# 6c. funnel_nome que NAO existe -> 404 e o lead NAO e criado.
db = SessionLocal()
total_antes = db.query(Lead).count()
db.close()

r = client.post(
    "/api/leads",
    params={"funnel_nome": "Funil Que Nao Existe XPTO"},
    json={"nome": "Lead Nao Deve Existir", "whatsapp": "5548999990010"},
)
check(r.status_code == 404, f"404 quando funnel_nome nao existe (veio {r.status_code}: {r.text[:200]})")

db = SessionLocal()
total_depois = db.query(Lead).count()
db.close()
check(total_depois == total_antes,
      f"o lead NAO foi criado (contagem de leads antes={total_antes}, depois={total_depois})")


# ─── 7. resolver_etapa_inicial: explicito > Sem Contato > etapas[0] > fallback
# Unitario, sobre objetos Funnel NAO persistidos — a funcao so le
# funnel.etapas/nome/id, nao toca banco.
print()
print("7) resolver_etapa_inicial: explicito > Sem Contato > etapas[0] > 'nova_oportunidade'")

funil_ad_hoc = Funnel(
    id=999001, nome="Funil Ad Hoc (nao persistido)",
    etapas=[
        {"id": "novo", "nome": "Novo"},
        {"id": "sem_contato", "nome": "Sem Contato"},
        {"id": "convertido", "nome": "Convertido"},
    ],
)

resolvido = lead_creation.resolver_etapa_inicial(funil_ad_hoc, etapa_id="convertido")
check(resolvido == "convertido", f"etapa_id explicito EXISTENTE vence Sem Contato (veio {resolvido!r})")

resolvido = lead_creation.resolver_etapa_inicial(funil_ad_hoc, etapa_id="id-que-nao-existe")
check(resolvido == "sem_contato",
      f"etapa_id explicito INEXISTENTE cai pra Sem Contato, nao pro id invalido (veio {resolvido!r})")

funil_sem_sem_contato = Funnel(
    id=999002, nome="Funil Sem Sem Contato (nao persistido)",
    etapas=[{"id": "primeiro", "nome": "Primeiro"}, {"id": "segundo", "nome": "Segundo"}],
)
resolvido = lead_creation.resolver_etapa_inicial(funil_sem_sem_contato)
check(resolvido == "primeiro",
      f"funil SEM etapa Sem Contato cai pra etapas[0] — fallback degradado, mas deliberado (veio {resolvido!r})")

funil_vazio = Funnel(id=999003, nome="Funil Vazio (nao persistido)", etapas=[])
resolvido = lead_creation.resolver_etapa_inicial(funil_vazio)
check(resolvido == "nova_oportunidade",
      f"funil com etapas VAZIA cai pro fallback fixo 'nova_oportunidade' (veio {resolvido!r})")


# ─── 8. add manual no MESMO funil continua 409 e nao duplica ────────────
print()
print("8) POST /api/pipeline/funnels/{id}/leads no MESMO funil ainda da 409")

if LEAD_GER_ID:
    r = client.post(
        f"/api/pipeline/funnels/{CTX['principal']}/leads",
        json={"lead_id": LEAD_GER_ID, "etapa_id": "triagem"},
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


# ─── 9. add em funil DIFERENTE funciona (multi-funil por design) ────────
print()
print("9) adicionar o mesmo lead a um funil DIFERENTE funciona")

if LEAD_GER_ID:
    r = client.post(
        f"/api/pipeline/funnels/{CTX['extra']}/leads",
        json={"lead_id": LEAD_GER_ID, "etapa_id": "novo"},
    )
    check(r.status_code == 201, f"201 num funil diferente (veio {r.status_code}: {r.text[:150]})")
    db = SessionLocal()
    try:
        n = db.query(FunnelEntry).filter(FunnelEntry.lead_id == LEAD_GER_ID).count()
        check(n == 2, f"agora 2 entries no total — multi-funil e por design (tem {n})")
    finally:
        db.close()


# ─── 10. /locate agora acha o lead (antes do fix era 404) ────────────────
print()
print("10) GET /api/pipeline/locate/{lead_id} acha lead criado via POST /api/leads")

if LEAD_GER_ID:
    r = client.get(f"/api/pipeline/locate/{LEAD_GER_ID}")
    check(r.status_code == 200, f"200 no locate — antes do fix era 404 (veio {r.status_code})")
    if r.status_code == 200:
        check(r.json()["funnel_id"] == CTX["principal"],
              "locate aponta pro funil certo (Principal, a entry mais antiga)")


# ─── 11. LeadHistory.dados nunca e None ──────────────────────────────────
print()
print("11) LeadHistory do lead criado tem dados como dict, nunca None")

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


# ─── 12. funnel_nome roteia de verdade (fecha cobertura falsa da 6) ──────
print()
print("12) funnel_nome roteia de verdade — nao e o funil de menor id se disfarcando")

# A secao 6 pede "Vendas: Formulário", que POR ACASO e o funil de MENOR id
# deste seed. Se o parametro fosse ignorado, o lead cairia nele do mesmo jeito
# pela regra antiga — o check passaria sem provar nada. Aqui o alvo e
# "Funil Extra", que NAO e o menor nem o default: so acerta quem realmente
# resolve pelo nome.
r = client.post(
    "/api/leads",
    params={"funnel_nome": "Funil Extra"},
    json={"nome": "Lead Roteado Por Nome", "whatsapp": "5548999990012"},
)
check(r.status_code == 201, f"201 com funnel_nome='Funil Extra' (veio {r.status_code}: {r.text[:150]})")
if r.status_code == 201:
    _id = r.json()["id"]
    db = SessionLocal()
    try:
        ents = [(e.funnel_id, e.etapa_id) for e in
                db.query(FunnelEntry).filter(FunnelEntry.lead_id == _id).all()]
    finally:
        db.close()
    check(ents == [(CTX["extra"], "novo")],
          f"entrou SO em Funil Extra (id {CTX['extra']}), nao no menor ({CTX['formulario']}) "
          f"nem no default ({CTX['principal']}) — obteve {ents}")


# ─── 13. funnel_id invalido recusa igual a funnel_nome invalido ─────────
print()
print("13) funnel_id inexistente ou inativo recusa, e NAO cria o lead")

# AUDIT-2026-08-WF2 (revisao adversarial): `funnel_nome` inexistente ja dava
# 404 sem criar o lead, mas `funnel_id` inexistente dava 201 e criava o lead
# SEM funil nenhum — fora do Kanban, /locate em 404, "Ver no Funil" morto. O
# sintoma F-341 de volta, pelo parametro que a propria docstring desaconselha.
db = SessionLocal()
try:
    inativo_13 = _funil(db, "Funil Inativo 13", [{"id": "x", "nome": "X"}], ativo=False)
    ID_INATIVO_13 = inativo_13.id
    antes_13 = db.query(Lead).count()
finally:
    db.close()

for rotulo, params in (
    ("id inexistente", {"funnel_id": 99999999}),
    ("id de funil INATIVO", {"funnel_id": ID_INATIVO_13}),
    ("nome inexistente", {"funnel_nome": "Funil Que Nao Existe 13"}),
):
    r = client.post(
        "/api/leads", params=params,
        json={"nome": f"Recusa {rotulo}", "whatsapp": "5548999990013"},
    )
    check(r.status_code == 404, f"{rotulo} -> 404 (veio {r.status_code}: {r.text[:120]})")

db = SessionLocal()
try:
    depois_13 = db.query(Lead).count()
finally:
    db.close()
check(antes_13 == depois_13,
      f"nenhum lead foi criado pelas tres recusas (antes {antes_13}, depois {depois_13})")


# ─── 14. etapa: `id` vence `nome`, e a POSICAO nunca decide ──────────────
print()
print("14) resolucao de etapa e deterministica — reordenar o funil nao muda nada")

from app.services.lead_creation import resolver_etapa_inicial  # noqa: E402


class _FunilFalso:
    """resolver_etapa_inicial le so nome, id e etapas — nao precisa de banco."""

    def __init__(self, etapas):
        self.nome, self.id, self.etapas = "Funil Falso 14", 999014, etapas


# (a) um `etapa_id` que EXISTE como id nao pode ser respondido com outra etapa
# so porque ela se chama assim. Antes: pedir "triagem" devolvia "novo".
f = _FunilFalso([{"id": "novo", "nome": "Triagem"}, {"id": "triagem", "nome": "Novo"}])
got = resolver_etapa_inicial(f, "triagem")
check(got == "triagem", f"`id` explicito vence um `nome` homonimo (obteve {got!r})")

# (b) mesmas etapas, ordem trocada, mesmo resultado.
a = _FunilFalso([{"id": "primeiro_toque", "nome": "Sem Contato"},
                 {"id": "sem_contato", "nome": "Triagem"}])
b = _FunilFalso(list(reversed(a.etapas)))
ra, rb = resolver_etapa_inicial(a), resolver_etapa_inicial(b)
check(ra == rb == "sem_contato",
      f"reordenar as etapas NAO muda onde o lead nasce (obteve {ra!r} e {rb!r})")

# (c) empate real (dois ids que normalizam igual): desempata pelo id, nao pela
# posicao. Funil ambiguo continua sendo funil mal configurado — mas para de ser
# imprevisivel.
c1 = _FunilFalso([{"id": "Sem_Contato", "nome": "X"}, {"id": "sem contato", "nome": "Y"}])
c2 = _FunilFalso(list(reversed(c1.etapas)))
r1, r2 = resolver_etapa_inicial(c1), resolver_etapa_inicial(c2)
check(r1 == r2, f"empate resolve igual nas duas ordens (obteve {r1!r} e {r2!r})")

# (d) etapa que casa por `nome` mas nao tem `id` derrubava com KeyError: 500 no
# CRM enquanto o espelho do Conversas seguia normal. Mesmo dado, um lado perde
# o lead e o outro nao.
d = _FunilFalso([{"nome": "Sem Contato"}, {"id": "z", "nome": "Z"}])
try:
    got = resolver_etapa_inicial(d)
    check(got == "z", f"etapa sem `id` e descartada, nao derruba (obteve {got!r})")
except KeyError as exc:
    check(False, f"KeyError {exc} — etapa sem `id` ainda derruba a criacao de lead")


# ─── 15. `etapa_id` ignorado deixa rastro; NFC nao inventa 404 ──────────
print()
print("15) pedido ignorado avisa, e acento composto/decomposto casa")

import logging  # noqa: E402
from app.services.lead_creation import _normalizar  # noqa: E402

_avisos = []


class _Coletor(logging.Handler):
    def emit(self, registro):
        _avisos.append(registro.getMessage())


_log_lc = logging.getLogger("app.services.lead_creation")
_log_lc.addHandler(_Coletor())

# O aviso so existia no ramo `etapas[0]` — ou seja, justamente no caso NORMAL
# (funil COM "Sem Contato") o pedido descartado nao deixava rastro nenhum.
f15 = _FunilFalso([{"id": "sem_contato", "nome": "Sem Contato"},
                   {"id": "outra", "nome": "Outra"}])
got = resolver_etapa_inicial(f15, "etapa-que-nao-existe")
check(got == "sem_contato", f"cai na etapa default (obteve {got!r})")
check(any("IGNORADO" in a for a in _avisos),
      f"e AVISA que o pedido foi ignorado (avisos: {_avisos})")

import unicodedata  # noqa: E402

_nfc = unicodedata.normalize("NFC", "Vendas: Formulário")
_nfd = unicodedata.normalize("NFD", "Vendas: Formulário")
check(_nfc.encode() != _nfd.encode(), "premissa: os dois textos SAO bytes diferentes")
check(_normalizar(_nfc) == _normalizar(_nfd),
      "mesmo nome visivel casa em NFC e NFD — antes o NFD devolvia um 404 "
      "impossivel de acreditar")
check(_normalizar("Vendas WhatsApp") != _normalizar("Vendas: Principal"),
      "e nomes de verdade diferentes continuam diferentes (nao e substring)")

r = client.post(
    "/api/leads",
    params={"funnel_nome": _nfd},
    json={"nome": "Lead NFD", "whatsapp": "5548999990015"},
)
check(r.status_code == 201,
      f"funnel_nome em NFD acha o funil gravado em NFC (veio {r.status_code}: {r.text[:150]})")
if r.status_code == 201:
    _id = r.json()["id"]
    db = SessionLocal()
    try:
        ents = [e.funnel_id for e in
                db.query(FunnelEntry).filter(FunnelEntry.lead_id == _id).all()]
    finally:
        db.close()
    check(ents == [CTX["formulario"]], f"e entrou no funil de Formulario (obteve {ents})")


print()
main.app.dependency_overrides.clear()
if falhas:
    print(f"{len(falhas)} FALHA(S)")
    sys.exit(1)
print("OK: funil default resolvido por NOME (nunca por id nem por posicao) — F-341 e AUDIT-2026-08-WF2 cobertos")
