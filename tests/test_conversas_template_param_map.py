"""
CONV-TPLMAP-01 — mapeamento persistente {{n}} -> @VARIAVEL nos templates.

Tres conceitos DIFERENTES convivem e esta suite existe para que nao se
misturem:
  1. `{{1}}`                 -> parametro posicional oficial da Meta
  2. `@PRIMEIRONOMECLIENTE`  -> variavel interna do Conversas (CONV-VAR-01)
  3. `"Joao"`                -> exemplo que a Meta exige para APROVAR
O exemplo (3) e material de aprovacao e NUNCA pode virar valor de envio.

Prova que:
  D. o BODY dita as posicoes: {{1}} gera uma, {{1}}+{{2}} geram duas ordenadas;
  P. o mapeamento persiste por (name, language) e sobrevive a nova sessao;
  V. mapeamento invalido e recusado (token inexistente, desativado, posicao
     fora da faixa, posicao nao numerica, template desconhecido);
  K. name+language e a chave: o mesmo nome em outro idioma tem outro mapeamento;
  E. no ENVIO, a posicao mapeada e resolvida pelo resolver do PR #36 e a Meta
     recebe o valor real — nunca o token, nunca `{{n}}`, nunca o exemplo;
  O. dois parametros preservam a ordem posicional da Meta;
  F. contexto ausente FALHA ANTES da Meta (fail closed, reusando render_strict);
  H. Message.content == corpo renderizado com os mesmos valores;
  L. template SEM mapeamento mantem o comportamento manual legado;
  S. template com @TOKEN estatico e DETECTADO, sem auto-alteracao;
  M. editar o mapeamento local nao chama a Meta;
  R. sem regressao nos invariantes do PR #36 e do PR #37.

Meta API mockada; nenhuma credencial real, nenhuma rede.
Roda standalone:  python tests/test_conversas_template_param_map.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_template_param_map_test.db"
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

from datetime import datetime, timezone  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text as sql_text  # noqa: E402

import app.main as main  # noqa: E402
from app.database import engine, Base, SessionLocal  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.models.template import ServiceTemplate, TemplateParamMap  # noqa: E402
from app.services import meta_templates  # noqa: E402
from app.services import whatsapp  # noqa: E402

failures = []


def _safe(text: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="backslashreplace").decode(encoding)


def check(cond, msg):
    if cond:
        print(f"  PASS: {_safe(msg)}")
    else:
        print(f"  FAIL: {_safe(msg)}")
        failures.append(msg)


# ─── Catalogo Meta mockado ────────────────────────────────────────────
META_RAW = [
    {   # o caso de integracao do pacote: dois parametros, duas variaveis
        "name": "apresentacao", "language": "pt_BR", "status": "APPROVED", "category": "UTILITY",
        "components": [{"type": "BODY", "text": "Olá {{1}}, tudo bem? Meu nome é {{2}}."}],
    },
    {   # um parametro
        "name": "cotacao_confirmar_recebimento", "language": "pt_BR", "status": "APPROVED", "category": "UTILITY",
        "components": [{"type": "BODY", "text": "Olá {{1}}, recebemos sua cotação."}],
    },
    {   # MESMO nome, outro idioma: prova que a chave e o PAR
        "name": "apresentacao", "language": "en_US", "status": "APPROVED", "category": "UTILITY",
        "components": [{"type": "BODY", "text": "Hi {{1}}, my name is {{2}}."}],
    },
    {   # zero parametros
        "name": "aviso_simples", "language": "pt_BR", "status": "APPROVED", "category": "UTILITY",
        "components": [{"type": "BODY", "text": "Olá! Retomando nosso contato."}],
    },
    {   # LEGADO quebrado: @TOKEN como texto estatico, body_params = 0
        "name": "legado_token_estatico", "language": "pt_BR", "status": "APPROVED", "category": "UTILITY",
        "components": [{"type": "BODY", "text": "Olá @PRIMEIRONOMECLIENTE, tudo bem?"}],
    },
]

meta_write_calls = []


async def _fake_fetch(base_url, waba_id, headers):
    return META_RAW


meta_templates._fetch_meta_templates = _fake_fetch
meta_templates.invalidate_catalog_cache()


async def _fake_create_on_meta(template, db):
    """Criacao na Meta: registra a chamada para provar quem fala com a Meta."""
    meta_write_calls.append({"op": "create", "name": template.name})
    return {"success": False, "error": "Meta API não configurada"}


meta_templates.create_template_on_meta = _fake_create_on_meta

meta_calls = []
_wamid = {"n": 0}


async def _fake_send_template(to, template_name, language, components, db=None):
    _wamid["n"] += 1
    meta_calls.append({"to": to, "template_name": template_name,
                       "language": language, "components": components})
    return {"messages": [{"id": f"wamid.TPLMAP{_wamid['n']}"}]}


whatsapp.send_template_message = _fake_send_template


def meta_params():
    if not meta_calls:
        return None
    for comp in meta_calls[-1]["components"]:
        if comp.get("type") == "body":
            return [p["text"] for p in comp["parameters"]]
    return []


# ─── Setup ────────────────────────────────────────────────────────────
Base.metadata.create_all(bind=engine)


class _AdminUser:
    id = 1
    nome = "Júlia Atendente"
    email = "julia@bna.local"
    role = "ADMIN"
    is_active = True


class _SellerUser:
    id = 2
    nome = "Vendedor"
    email = "vendedor@bna.local"
    role = "user"
    is_active = True


CURRENT = {"user": _AdminUser()}
main.app.dependency_overrides[get_current_user] = lambda: CURRENT["user"]
client = TestClient(main.app)

session = SessionLocal()
session.execute(sql_text("CREATE TABLE IF NOT EXISTS leads (id INTEGER PRIMARY KEY, nome TEXT, email TEXT)"))
session.execute(sql_text("INSERT INTO leads (id, nome, email) VALUES (7, 'João Pedro', 'joao@ex.com')"))
session.execute(sql_text("INSERT INTO leads (id, nome, email) VALUES (8, '', '')"))
for t in META_RAW:
    session.add(ServiceTemplate(name=t["name"], language=t["language"]))

_AGORA = datetime.now(timezone.utc)
conv = Conversation(lead_id=7, whatsapp="5511977776666", nome="5511977776666",
                    status="aberta", atendente_id=1, is_bot_active=False,
                    unread_count=0, last_customer_msg_at=_AGORA)
conv_sem_nome = Conversation(lead_id=8, whatsapp="5511955554444", nome="5511955554444",
                             status="aberta", atendente_id=1, is_bot_active=False,
                             unread_count=0, last_customer_msg_at=_AGORA)
session.add_all([conv, conv_sem_nome])
session.commit()
CONV, CONV_SEM_NOME = conv.id, conv_sem_nome.id
session.close()

for payload in (
    {"token": "@PRIMEIRONOMECLIENTE", "name": "Primeiro Nome do Cliente",
     "kind": "dynamic", "source_key": "cliente.primeiro_nome"},
    {"token": "@NOMEATENDENTE", "name": "Nome do Atendente",
     "kind": "dynamic", "source_key": "atendente.nome"},
    {"token": "@DESATIVADA", "name": "Desativada", "kind": "fixed", "fixed_value": "x"},
):
    r = client.post("/api/variables", json=payload)
    assert r.status_code == 201, (payload["token"], r.status_code, r.text)
    if payload["token"] == "@DESATIVADA":
        client.put(f"/api/variables/{r.json()['id']}", json={"is_active": False})


def put_map(name, language, mappings):
    return client.put("/api/templates/param-map",
                      json={"name": name, "language": language, "mappings": mappings})


def get_map(name, language):
    return client.get(f"/api/templates/param-map?name={name}&language={language}")


def send_template(conv_id, name, params, language="pt_BR"):
    return client.post(f"/api/conversations/{conv_id}/messages", json={
        "content": "ignorado — o corpo real vem da Meta",
        "msg_type": "template", "template_name": name,
        "template_language": language, "template_params": params,
    })


def last_content(conv_id):
    sess = SessionLocal()
    try:
        m = sess.query(Message).filter(
            Message.conversation_id == conv_id, Message.direction == "outbound"
        ).order_by(Message.id.desc()).first()
        return m.content if m else None
    finally:
        sess.close()


def outbound_count(conv_id):
    sess = SessionLocal()
    try:
        return sess.query(Message).filter(
            Message.conversation_id == conv_id, Message.direction == "outbound"
        ).count()
    finally:
        sess.close()


# ============ D. Posicoes derivadas do BODY ============
print("D — posicoes vem do BODY real")

r = client.get("/api/templates/meta/approved")
catalog = {(t["name"], t["language"]): t for t in r.json()["templates"]}
check(catalog[("cotacao_confirmar_recebimento", "pt_BR")]["body_params"] == 1,
      "BODY com {{1}} -> 1 posicao")
check(catalog[("apresentacao", "pt_BR")]["body_params"] == 2,
      "BODY com {{1}} e {{2}} -> 2 posicoes")
check(catalog[("aviso_simples", "pt_BR")]["body_params"] == 0, "BODY sem parametro -> 0")

r = put_map("apresentacao", "pt_BR", {"2": "@NOMEATENDENTE", "1": "@PRIMEIRONOMECLIENTE"})
check(r.status_code == 200, f"PUT com posicoes fora de ordem -> 200 (got {r.status_code}: {r.text[:140]})")
check(list(r.json()["mappings"].keys()) == ["1", "2"],
      f"resposta ordenada numericamente (got {list(r.json()['mappings'].keys())})")


# ============ P. Persistencia ============
print("\nP — persistencia por (name, language)")

r = get_map("apresentacao", "pt_BR")
check(r.status_code == 200 and r.json()["mappings"] == {"1": "@PRIMEIRONOMECLIENTE", "2": "@NOMEATENDENTE"},
      f"mapeamento recuperado (got {r.json().get('mappings')})")

# Fonte de verdade e o BANCO, nao a sessao HTTP nem o JavaScript.
sess = SessionLocal()
rows = sess.query(TemplateParamMap).filter(
    TemplateParamMap.name == "apresentacao", TemplateParamMap.language == "pt_BR"
).order_by(TemplateParamMap.position).all()
sess.close()
check([(x.position, x.token) for x in rows] ==
      [(1, "@PRIMEIRONOMECLIENTE"), (2, "@NOMEATENDENTE")],
      f"linhas persistidas no banco (got {[(x.position, x.token) for x in rows]})")

# Replace-all: reenviar so {{1}} remove {{2}}, sem orfao.
put_map("apresentacao", "pt_BR", {"1": "@PRIMEIRONOMECLIENTE"})
check(get_map("apresentacao", "pt_BR").json()["mappings"] == {"1": "@PRIMEIRONOMECLIENTE"},
      "replace-all: posicao omitida e removida")
sess = SessionLocal()
orfaos = sess.query(TemplateParamMap).filter(
    TemplateParamMap.name == "apresentacao", TemplateParamMap.language == "pt_BR"
).count()
sess.close()
check(orfaos == 1, f"nenhuma linha orfa depois do replace (got {orfaos})")
put_map("apresentacao", "pt_BR", {"1": "@PRIMEIRONOMECLIENTE", "2": "@NOMEATENDENTE"})


# ============ K. name+language e a chave ============
print("\nK — mesmo nome, outro idioma")

put_map("apresentacao", "en_US", {"1": "@NOMEATENDENTE"})
check(get_map("apresentacao", "pt_BR").json()["mappings"] == {"1": "@PRIMEIRONOMECLIENTE", "2": "@NOMEATENDENTE"},
      "pt_BR nao foi afetado pelo en_US")
check(get_map("apresentacao", "en_US").json()["mappings"] == {"1": "@NOMEATENDENTE"},
      "en_US tem o proprio mapeamento")


# ============ V. Validacao ============
print("\nV — mapeamento invalido e recusado")

r = put_map("apresentacao", "pt_BR", {"1": "@NAOEXISTE"})
check(r.status_code == 422 and "@NAOEXISTE" in r.text, f"variavel inexistente -> 422 (got {r.status_code})")

r = put_map("apresentacao", "pt_BR", {"1": "@DESATIVADA"})
check(r.status_code == 422, f"variavel desativada -> 422 (got {r.status_code})")

r = put_map("apresentacao", "pt_BR", {"3": "@PRIMEIRONOMECLIENTE"})
check(r.status_code == 422, f"posicao 3 num template de 2 -> 422 (got {r.status_code})")

r = put_map("apresentacao", "pt_BR", {"0": "@PRIMEIRONOMECLIENTE"})
check(r.status_code == 422, f"posicao 0 -> 422 (got {r.status_code})")

r = put_map("aviso_simples", "pt_BR", {"1": "@PRIMEIRONOMECLIENTE"})
check(r.status_code == 422, f"template sem parametros -> 422 (got {r.status_code})")

r = put_map("apresentacao", "pt_BR", {"abc": "@PRIMEIRONOMECLIENTE"})
check(r.status_code == 422, f"posicao nao numerica -> 422 (got {r.status_code})")

r = put_map("template_que_nao_existe", "pt_BR", {"1": "@PRIMEIRONOMECLIENTE"})
check(r.status_code == 404, f"template desconhecido -> 404 (got {r.status_code})")

# Uma recusa nao pode ter corrompido o mapeamento bom que ja existia.
check(get_map("apresentacao", "pt_BR").json()["mappings"] == {"1": "@PRIMEIRONOMECLIENTE", "2": "@NOMEATENDENTE"},
      "mapeamento valido sobreviveu as tentativas invalidas")

# Escrita e de ADMIN; leitura e de qualquer autenticado.
CURRENT["user"] = _SellerUser()
r = put_map("apresentacao", "pt_BR", {"1": "@PRIMEIRONOMECLIENTE"})
check(r.status_code == 403, f"nao-admin nao escreve mapeamento -> 403 (got {r.status_code})")
check(get_map("apresentacao", "pt_BR").status_code == 200, "nao-admin LE o mapeamento (composer precisa)")
CURRENT["user"] = _AdminUser()


# ============ E/O. Envio: integracao do pacote ============
print("\nE/O — envio com duas posicoes mapeadas")

before_meta_writes = len(meta_write_calls)
r = send_template(CONV, "apresentacao", [])   # nenhum parametro manual!
check(r.status_code == 200, f"envio sem informar parametro -> 200 (got {r.status_code}: {r.text[:200]})")
check(meta_params() == ["João", "Júlia Atendente"],
      f"Meta recebe os valores RESOLVIDOS na ordem (got {meta_params()})")
check(meta_params() != ["@PRIMEIRONOMECLIENTE", "@NOMEATENDENTE"], "nunca o token")
check(all("{{" not in p for p in meta_params()), "nunca o placeholder {{n}}")

# Formato exato exigido no enunciado.
body_comp = next(c for c in meta_calls[-1]["components"] if c["type"] == "body")
check(body_comp["parameters"] == [{"type": "text", "text": "João"},
                                  {"type": "text", "text": "Júlia Atendente"}],
      f"payload posicional no formato da Meta (got {body_comp['parameters']})")


# ============ H. Message.content ============
print("\nH — historico == corpo renderizado")

check(last_content(CONV) == "Olá João, tudo bem? Meu nome é Júlia Atendente.",
      f"Message.content renderizado (got {last_content(CONV)!r})")
check("@" not in (last_content(CONV) or "") and "{{" not in (last_content(CONV) or ""),
      "historico sem token e sem placeholder")


# ============ Exemplos Meta nao viram valor ============
print("\nX — exemplos de aprovacao nunca viram valor de envio")

r = client.post("/api/templates", json={
    "name": "tpl_com_exemplo", "category": "UTILITY", "language": "pt_BR",
    "body_text": "Olá {{1}}, tudo bem?",
    "sample_values": {"body": ["EXEMPLO_APROVACAO"]},
})
check(r.status_code == 201, f"template local criado (got {r.status_code}: {r.text[:140]})")
check(r.json()["sample_values"] == {"body": ["EXEMPLO_APROVACAO"]}, "exemplo persistido no template local")

# O exemplo existe, mas o valor enviado vem do MAPEAMENTO — nunca dele.
r = put_map("cotacao_confirmar_recebimento", "pt_BR", {"1": "@PRIMEIRONOMECLIENTE"})
check(r.status_code == 200, "mapeia {{1}} do template de 1 parametro")
send_template(CONV, "cotacao_confirmar_recebimento", [])
check(meta_params() == ["João"], f"Meta recebe o valor real (got {meta_params()})")
check("EXEMPLO_APROVACAO" not in str(meta_calls[-1]), "exemplo de aprovacao NAO aparece no payload")


# ============ F. Fail closed ============
print("\nF — contexto ausente falha ANTES da Meta")

before_calls = len(meta_calls)
before_msgs = outbound_count(CONV_SEM_NOME)
r = send_template(CONV_SEM_NOME, "apresentacao", [])
check(r.status_code == 422, f"lead sem nome -> 422 (got {r.status_code})")
check("@PRIMEIRONOMECLIENTE" in r.text, f"erro NOMEIA a variavel (got {r.text[:180]})")
check(len(meta_calls) == before_calls, "NENHUMA chamada a Meta")
check(outbound_count(CONV_SEM_NOME) == before_msgs, "NADA persistido")


# ============ L. Compatibilidade: template sem mapeamento ============
print("\nL — template legado sem mapeamento")

put_map("apresentacao", "en_US", {})     # limpa
r = send_template(CONV, "apresentacao", ["Manual Um", "Manual Dois"], language="en_US")
check(r.status_code == 200, f"parametros manuais continuam valendo (got {r.status_code}: {r.text[:180]})")
check(meta_params() == ["Manual Um", "Manual Dois"],
      f"valores manuais chegam intactos (got {meta_params()})")

r = send_template(CONV, "apresentacao", ["so_um"], language="en_US")
check(r.status_code == 422, f"aridade legada continua exigida (got {r.status_code})")

r = send_template(CONV, "aviso_simples", [])
check(r.status_code == 200 and meta_calls[-1]["components"] == [],
      "template de 0 parametros inalterado")

# Mapeamento PARCIAL: {{1}} automatico, {{2}} manual.
put_map("apresentacao", "en_US", {"1": "@PRIMEIRONOMECLIENTE"})
r = send_template(CONV, "apresentacao", ["Resto Manual"], language="en_US")
check(r.status_code == 200 and meta_params() == ["João", "Resto Manual"],
      f"parcial: 1 automatico + 1 manual, na ordem (got {meta_params()})")
r = send_template(CONV, "apresentacao", ["a", "b"], language="en_US")
check(r.status_code == 422, "com 1 mapeada, enviar 2 manuais e recusado")
put_map("apresentacao", "en_US", {})


# ============ S. @TOKEN estatico: detecta, nao altera ============
print("\nS — template legado com @TOKEN como texto estatico")

r = client.get("/api/templates/meta/approved")
legado = next(t for t in r.json()["templates"] if t["name"] == "legado_token_estatico")
check(legado["body_params"] == 0, "template estatico tem body_params 0")
check(legado["static_tokens"] == ["@PRIMEIRONOMECLIENTE"],
      f"tokens estaticos DETECTADOS (got {legado.get('static_tokens')})")
check(legado["body_text"] == "Olá @PRIMEIRONOMECLIENTE, tudo bem?",
      "BODY NAO foi alterado automaticamente")

ok_tpl = next(t for t in r.json()["templates"] if t["name"] == "apresentacao")
check(ok_tpl["static_tokens"] == [], "template com {{n}} nao gera falso positivo")

js = (CONVERSAS_DIR / "static" / "js" / "conversas.js").read_text(encoding="utf-8")
check("static_tokens" in js, "composer consome a deteccao para avisar o operador")


# ============ M. Editar mapeamento nao toca a Meta ============
print("\nM — mapeamento local nao mexe no template da Meta")

before_writes = len(meta_write_calls)
before_body = catalog[("apresentacao", "pt_BR")]["body_text"]
put_map("apresentacao", "pt_BR", {"1": "@NOMEATENDENTE", "2": "@PRIMEIRONOMECLIENTE"})
check(len(meta_write_calls) == before_writes, "PUT de mapeamento NAO chama a Meta")
r = client.get("/api/templates/meta/approved")
agora = {(t["name"], t["language"]): t for t in r.json()["templates"]}
check(agora[("apresentacao", "pt_BR")]["body_text"] == before_body,
      "BODY aprovado permanece identico apos editar o mapeamento")

send_template(CONV, "apresentacao", [])
check(meta_params() == ["Júlia Atendente", "João"],
      f"a troca vale JA no proximo envio, sem cache (got {meta_params()})")
put_map("apresentacao", "pt_BR", {"1": "@PRIMEIRONOMECLIENTE", "2": "@NOMEATENDENTE"})


# ============ Integridade: variavel em uso ============
print("\nI — variavel usada em mapeamento nao pode sumir")

r = client.get("/api/variables")
var_id = next(v["id"] for v in r.json()["variables"] if v["token"] == "@PRIMEIRONOMECLIENTE")
r = client.delete(f"/api/variables/{var_id}")
check(r.status_code == 409, f"excluir variavel mapeada -> 409 (got {r.status_code})")
r = client.put(f"/api/variables/{var_id}", json={"token": "@OUTRONOME"})
check(r.status_code == 409, f"renomear variavel mapeada -> 409 (got {r.status_code})")
check(get_map("apresentacao", "pt_BR").json()["mappings"]["1"] == "@PRIMEIRONOMECLIENTE",
      "mapeamento intacto apos as tentativas")


# ============ Guards estaticos ============
print("\nG — sem segundo sistema de variaveis")

tpl_js = (CONVERSAS_DIR / "static" / "js" / "templates.js").read_text(encoding="utf-8")
check("PRIMEIRONOMECLIENTE" not in tpl_js, "templates.js NAO tem catalogo hardcoded")
check("/api/variables" in tpl_js, "dropdown busca o catalogo real do backend")

conv_py = (CONVERSAS_DIR / "app" / "routers" / "conversations.py").read_text(encoding="utf-8")
check("variables_service.render_strict" in conv_py, "envio reusa render_strict do PR #36")

svc = (CONVERSAS_DIR / "app" / "services" / "variables.py").read_text(encoding="utf-8")
check(svc.count("def render(") == 1, "existe UMA unica funcao render() no sistema")

tpl_py = (CONVERSAS_DIR / "app" / "routers" / "templates.py").read_text(encoding="utf-8")
check("PRIMEIRONOMECLIENTE" not in tpl_py, "router de templates sem token hardcoded")


# ─── Resultado ────────────────────────────────────────────────────────
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FALHA(S):")
    for f in failures:
        print(f"  - {_safe(f)}")
    sys.exit(1)
print("TODOS OS CHECKS PASSARAM")
