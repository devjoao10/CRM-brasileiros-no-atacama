"""
CONV-CURATION-01 — Curadoria de templates do atendimento.

APPROVED pela Meta != AUTORIZADO PARA USO PELOS ATENDENTES.

A conta real tem 34 templates APPROVED, incluindo operacionais/internos
(`alerta_novo_lead`, `alerta_crm`, `notificacao_crm`, `hello_world`, `teste`).
Nenhum deles pode chegar ao seletor do atendimento so porque a Meta aprovou.

Prova que:
  A. o catalogo do atendimento devolve APPROVED **E** autorizado localmente
  B. autorizacao local NUNCA sobrepoe o status Meta (PENDING/REJECTED continuam fora)
  C. esconder do dropdown nao basta — o BACKEND recusa envio nao autorizado,
     tanto pelo composer quanto por /initiate
  D. ausencia de configuracao => nao oferece nada (fail closed, bootstrap vazio)
  E. o endpoint de catalogo exige autenticacao; a rota de curadoria exige ADMIN
  F. revogar tem efeito IMEDIATO (o cache de 5 min e dos dados da Meta, nao da
     autorizacao)
  G. janela fechada sem nenhum template autorizado => composer segue bloqueado,
     nunca liberando texto livre

Meta API mockada; nenhuma credencial real, nenhuma requisicao de rede.
Roda standalone (processo isolado):

    python tests/test_conversas_template_curation.py
"""
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_curation_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_ACCESS_TOKEN"] = "TESTE_NAO_E_TOKEN_REAL"
os.environ["META_PHONE_NUMBER_ID"] = "0000000000"
os.environ["META_WABA_ID"] = "0000000001"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(CONVERSAS_DIR))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402
from app.models.template import ServiceTemplate  # noqa: E402
from app.services import whatsapp  # noqa: E402
from app.services import meta_templates  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


UTC = timezone.utc

# --- Meta mockada: espelha a conta real (internos + atendimento + nao aprovados)
INTERNOS = ["alerta_novo_lead", "alerta_matinal_leads", "alerta_lead",
            "alerta_crm", "notificacao_crm", "hello_world", "teste",
            "teste_boas_vindas"]

META_RAW = (
    [{"name": n, "language": "pt_BR", "status": "APPROVED", "category": "UTILITY",
      "components": [{"type": "BODY", "text": f"interno {n}"}]} for n in INTERNOS]
    + [
        {"name": "retomada_atendimento", "language": "pt_BR", "status": "APPROVED",
         "category": "UTILITY",
         "components": [{"type": "BODY", "text": "Ola {{1}}! Podemos retomar?"}]},
        {"name": "retomada_atendimento", "language": "en_US", "status": "APPROVED",
         "category": "UTILITY",
         "components": [{"type": "BODY", "text": "Hi {{1}}! Shall we continue?"}]},
        {"name": "promo_pendente", "language": "pt_BR", "status": "PENDING",
         "category": "MARKETING",
         "components": [{"type": "BODY", "text": "Promo"}]},
        {"name": "promo_rejeitada", "language": "pt_BR", "status": "REJECTED",
         "category": "MARKETING",
         "components": [{"type": "BODY", "text": "Promo"}]},
    ]
)

meta_state = {"fail": False}


async def _fake_fetch(base_url, waba_id, headers):
    if meta_state["fail"]:
        import httpx
        raise httpx.TimeoutException("meta indisponivel (simulado)")
    return META_RAW


meta_templates._fetch_meta_templates = _fake_fetch

calls = {"template": 0}
_seq = {"n": 0}


async def _send_template(*a, **k):
    calls["template"] += 1
    _seq["n"] += 1
    return {"messages": [{"id": f"wamid.CUR_{_seq['n']}"}]}


whatsapp.send_template_message = _send_template

# --- Setup ---
Base.metadata.create_all(bind=engine)


class _Admin:
    id = 1
    email = "admin@local"
    role = "admin"
    is_admin = True
    nome = "Admin"


class _Operador:
    id = 2
    email = "op@local"
    role = "atendente"
    is_admin = False
    nome = "Operador"


current = {"user": _Admin()}
main.app.dependency_overrides[get_current_user] = lambda: current["user"]
client = TestClient(main.app)


def as_admin():
    current["user"] = _Admin()


def as_operador():
    current["user"] = _Operador()


def authorize(name, language):
    r = client.put("/api/templates/service-availability",
                   json={"name": name, "language": language, "available": True})
    assert r.status_code == 200, r.text
    return r


def revoke(name, language):
    return client.put("/api/templates/service-availability",
                      json={"name": name, "language": language, "available": False})


def service_names():
    r = client.get("/api/templates/meta/approved")
    assert r.status_code == 200, r.text
    return [(t["name"], t["language"]) for t in r.json()["templates"]]


def make_conv(numero, ago_hours):
    s = SessionLocal()
    try:
        c = Conversation(lead_id=0, whatsapp=numero, nome="Lead", status="aberta",
                         unread_count=0, atendente_id=1, is_bot_active=False,
                         last_customer_msg_at=datetime.now(UTC) - timedelta(hours=ago_hours))
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id
    finally:
        s.close()


def send_template(cid, name, language="pt_BR", params=None):
    return client.post(f"/api/conversations/{cid}/messages",
                       json={"content": "x", "msg_type": "template",
                             "template_name": name, "template_language": language,
                             "template_params": params or []})


# ===========================================================================
print("D — bootstrap VAZIO: sem configuracao, nada e oferecido (mutation I)")
as_admin()
s = SessionLocal()
check(s.query(ServiceTemplate).count() == 0, "tabela de curadoria nasce vazia")
s.close()
check(service_names() == [], "catalogo do atendimento comeca VAZIO mesmo com 12 APPROVED na Meta")

r = client.get("/api/templates/service-availability")
check(r.status_code == 200, "catalogo admin responde 200")
body = r.json()
APROVADOS = len(INTERNOS) + 2   # 8 internos + retomada pt_BR + retomada en_US
check(body["total"] == APROVADOS and body["available"] == 0,
      f"admin ve os {APROVADOS} APPROVED, 0 liberados (veio total={body['total']}, avail={body['available']})")
check(all(not t["available"] for t in body["templates"]), "nenhum vem marcado por padrao")

# ===========================================================================
print("\nA — APPROVED + autorizado aparece; internos nao (mutations A, B)")
authorize("retomada_atendimento", "pt_BR")
nomes = service_names()
check(nomes == [("retomada_atendimento", "pt_BR")],
      f"so o template autorizado aparece (veio {nomes})")
for n in INTERNOS:
    check((n, "pt_BR") not in nomes, f"interno '{n}' APPROVED nao aparece sem autorizacao")

# name+language: autorizar pt_BR nao libera en_US
check(("retomada_atendimento", "en_US") not in nomes,
      "autorizar (name, pt_BR) NAO libera (name, en_US) — a chave e o par")
authorize("retomada_atendimento", "en_US")
check(("retomada_atendimento", "en_US") in service_names(), "en_US aparece quando autorizado a parte")
revoke("retomada_atendimento", "en_US")

# ===========================================================================
print("\nB — autorizacao local NUNCA sobrepoe o status Meta (mutation F)")
authorize("promo_pendente", "pt_BR")
authorize("promo_rejeitada", "pt_BR")
nomes = service_names()
check(("promo_pendente", "pt_BR") not in nomes, "PENDING + autorizado -> NAO aparece")
check(("promo_rejeitada", "pt_BR") not in nomes, "REJECTED + autorizado -> NAO aparece")
r = client.get("/api/templates/service-availability")
check(all(t["status"] == "APPROVED" for t in r.json()["templates"]),
      "o proprio catalogo admin so lista APPROVED")

cid = make_conv("5511970000001", ago_hours=30)
r = send_template(cid, "promo_pendente")
check(r.status_code == 404, f"envio de PENDING autorizado -> 404 (veio {r.status_code})")
r = send_template(cid, "promo_rejeitada")
check(r.status_code == 404, f"envio de REJECTED autorizado -> 404 (veio {r.status_code})")
revoke("promo_pendente", "pt_BR")
revoke("promo_rejeitada", "pt_BR")

# ===========================================================================
print("\nC — o BACKEND recusa envio nao autorizado (mutations C, G, H)")
t_calls = calls["template"]
for n in INTERNOS:
    r = send_template(cid, n)
    check(r.status_code == 404, f"POST manual de '{n}' (APPROVED, nao autorizado) -> 404")
check(calls["template"] == t_calls, "nenhum template interno chegou a Meta")

# /initiate usa o MESMO builder
r = client.post("/api/conversations/initiate",
                json={"whatsapp": "5511970000002", "template_name": "alerta_novo_lead",
                      "template_language": "pt_BR"})
check(r.status_code == 404, f"/initiate com template interno -> 404 (veio {r.status_code})")
check(calls["template"] == t_calls, "/initiate nao chamou a Meta")

# autorizado continua funcionando, inclusive fora da janela de 24h
r = send_template(cid, "retomada_atendimento", params=["Ana"])
check(r.status_code == 200, f"template AUTORIZADO envia fora da janela (veio {r.status_code})")
check(calls["template"] == t_calls + 1, "template autorizado chamou a Meta 1x")

r = client.post("/api/conversations/initiate",
                json={"whatsapp": "5511970000003", "template_name": "retomada_atendimento",
                      "template_language": "pt_BR", "template_params": ["Ana"]})
check(r.status_code == 200 and r.json().get("message_sent") is True,
      "/initiate com template autorizado funciona")

# ===========================================================================
print("\nF — revogar tem efeito IMEDIATO, sem esperar o cache da Meta (mutation J)")
check(("retomada_atendimento", "pt_BR") in service_names(), "autorizado antes da revogacao")
revoke("retomada_atendimento", "pt_BR")
check(service_names() == [], "revogado some do catalogo NA HORA (sem esperar 5 min)")
t_calls = calls["template"]
r = send_template(cid, "retomada_atendimento", params=["Ana"])
check(r.status_code == 404, f"envio do revogado -> 404 imediatamente (veio {r.status_code})")
check(calls["template"] == t_calls, "revogado nao chega a Meta")
authorize("retomada_atendimento", "pt_BR")

# ===========================================================================
print("\nE — autenticacao e autorizacao (mutations D, E)")
main.app.dependency_overrides.pop(get_current_user)
try:
    r = client.get("/api/templates/meta/approved")
    check(r.status_code in (401, 403), f"catalogo do atendimento SEM auth -> {r.status_code}")
    r = client.get("/api/templates/service-availability")
    check(r.status_code in (401, 403), f"catalogo admin SEM auth -> {r.status_code}")
    r = client.put("/api/templates/service-availability",
                   json={"name": "x", "language": "pt_BR", "available": True})
    check(r.status_code in (401, 403), f"PUT de curadoria SEM auth -> {r.status_code}")
finally:
    main.app.dependency_overrides[get_current_user] = lambda: current["user"]

as_operador()
r = client.get("/api/templates/meta/approved")
check(r.status_code == 200, "operador autenticado LE o catalogo do atendimento")
r = client.get("/api/templates/service-availability")
check(r.status_code == 403, f"operador NAO le o catalogo admin (veio {r.status_code})")
r = client.put("/api/templates/service-availability",
               json={"name": "alerta_crm", "language": "pt_BR", "available": True})
check(r.status_code == 403, f"operador NAO altera curadoria (veio {r.status_code})")
s = SessionLocal()
check(s.query(ServiceTemplate).filter(ServiceTemplate.name == "alerta_crm").count() == 0,
      "a tentativa do operador nao gravou nada")
s.close()
as_admin()

# ===========================================================================
print("\nG — fail closed: Meta fora do ar e curadoria vazia nunca liberam texto")
meta_templates.invalidate_catalog_cache()
meta_state["fail"] = True
r = client.get("/api/templates/meta/approved")
check(r.status_code == 503, f"Meta indisponivel -> 503, sem lista parcial (veio {r.status_code})")
r = send_template(cid, "retomada_atendimento", params=["Ana"])
check(r.status_code == 404, "com a Meta fora do ar, nem o autorizado envia (fail closed)")
r = client.post(f"/api/conversations/{cid}/messages",
                json={"content": "texto livre", "msg_type": "text"})
d = r.json().get("detail")
check(r.status_code == 409 and isinstance(d, dict) and d.get("code") == "WINDOW_CLOSED",
      "texto livre CONTINUA bloqueado com a Meta fora do ar")
meta_state["fail"] = False
meta_templates.invalidate_catalog_cache()

# janela fechada + zero autorizados => composer segue bloqueado
revoke("retomada_atendimento", "pt_BR")
check(service_names() == [], "zero templates disponiveis")
r = client.post(f"/api/conversations/{cid}/messages",
                json={"content": "texto livre", "msg_type": "text"})
d = r.json().get("detail")
check(r.status_code == 409 and isinstance(d, dict) and d.get("code") == "WINDOW_CLOSED",
      "sem template disponivel, o composer NAO libera texto livre")
authorize("retomada_atendimento", "pt_BR")

# mudanca de autorizacao nao mexe na janela
s = SessionLocal()
conv = s.query(Conversation).filter(Conversation.id == cid).first()
check(conv.service_window_open is False, "curadoria nao reabre a janela de 24h")
s.close()

# ===========================================================================
print("\nH — idempotencia e integridade da curadoria")
authorize("retomada_atendimento", "pt_BR")
authorize("retomada_atendimento", "pt_BR")
s = SessionLocal()
check(s.query(ServiceTemplate).filter(
    ServiceTemplate.name == "retomada_atendimento",
    ServiceTemplate.language == "pt_BR").count() == 1,
    "autorizar duas vezes nao duplica linha (UNIQUE name+language)")
s.close()
r = revoke("nao_existe_em_lugar_nenhum", "pt_BR")
check(r.status_code == 200, "revogar algo nunca autorizado e no-op (idempotente)")

# ===========================================================================
print("\nI — o filtro e do BACKEND; nenhum nome vira REGRA no codigo")
# Um nome citado em COMENTARIO e documentacao ("nao usar prefixo alerta_"), nao
# regra. O que nao pode existir e o nome como STRING EXECUTAVEL — allowlist,
# denylist ou comparacao. Por isso comparamos contra os literais, nao contra o
# arquivo inteiro: `"teste" in src` casaria dentro da palavra "testes".
import ast as _ast  # noqa: E402
import re as _re  # noqa: E402


def py_string_literals(path):
    tree = _ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    docs = set()
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Module, _ast.ClassDef, _ast.FunctionDef, _ast.AsyncFunctionDef)):
            d = _ast.get_docstring(node, clean=False)
            if d:
                docs.add(d)
    return [
        n.value for n in _ast.walk(tree)
        if isinstance(n, _ast.Constant) and isinstance(n.value, str) and n.value not in docs
    ]


def js_without_comments(path):
    src = pathlib.Path(path).read_text(encoding="utf-8")
    src = _re.sub(r"/\*.*?\*/", "", src, flags=_re.S)
    return "\n".join(_re.sub(r"//.*$", "", line) for line in src.splitlines())


BACKEND = [CONVERSAS_DIR / "app" / "services" / "meta_templates.py",
           CONVERSAS_DIR / "app" / "routers" / "templates.py",
           CONVERSAS_DIR / "app" / "routers" / "conversations.py",
           CONVERSAS_DIR / "app" / "models" / "template.py"]
literais = [lit for f in BACKEND for lit in py_string_literals(f)]
js_code = js_without_comments(ROOT / "conversas" / "static" / "js" / "conversas.js")
tjs_code = js_without_comments(ROOT / "conversas" / "static" / "js" / "templates.js")

for n in INTERNOS:
    check(not any(n in lit for lit in literais), f"'{n}' nao e string executavel no backend")
    check(n not in js_code, f"'{n}' nao e codigo no JS do atendimento")
    check(n not in tjs_code, f"'{n}' nao e codigo no JS de administracao")

svc_py = (CONVERSAS_DIR / "app" / "services" / "meta_templates.py").read_text(encoding="utf-8")
check("startswith(" not in svc_py, "nenhuma regra por prefixo de nome no servico")
check("available" not in js_code.split("function templateItemEl")[1].split("function")[0],
      "o seletor nao filtra por autorizacao no cliente (ja vem filtrado)")

# ===========================================================================
print("\nJ — migration m009 idempotente e sem autorizar ninguem")
sys.path.insert(0, str(ROOT / "migrations"))
import m009_conversas_service_templates as m009  # noqa: E402

a1 = m009.run(engine)
a2 = m009.run(engine)
check(any("already-present" in x for x in a1), f"m009 detecta tabela existente ({a1})")
check(a1 == a2 or all("created" not in x for x in a2), "m009 e idempotente na 2a execucao")
check(any("bootstrap vazio" in x for x in a2), "m009 relata o bootstrap vazio explicitamente")
# DDL/DML por regex de comando SQL — `sys.path.insert` contem "insert" e faria
# um `"INSERT" in src` falhar sem nenhuma insercao existir.
src_m009 = (ROOT / "migrations" / "m009_conversas_service_templates.py").read_text(encoding="utf-8")
for cmd in ("INSERT INTO", "DELETE FROM", "DROP TABLE", "UPDATE "):
    check(not _re.search(cmd, src_m009, _re.I), f"m009 nao executa {cmd.strip()}")
s = SessionLocal()
check(s.query(ServiceTemplate).count() > 0, "as autorizacoes deste teste sobreviveram as duas execucoes de m009")
s.close()

# ===========================================================================
print()
if failures:
    print(f"FALHAS: {len(failures)}")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("TODOS OS INVARIANTES DE CURADORIA PASSARAM")
