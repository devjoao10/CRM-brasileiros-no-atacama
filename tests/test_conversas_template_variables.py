"""
CONV-VAR-02 — variaveis dinamicas (@TOKEN) nos PARAMETROS de template.

Bug de producao: `cotacao_confirmar_recebimento` (1 parametro de BODY) foi
enviado com `@PRIMEIRONOMECLIENTE` como valor de `{{1}}` e o cliente recebeu
"Ola @PRIMEIRONOMECLIENTE, ...". Esta suite prova, sem assumir nada, ONDE o
literal aparecia (payload da Meta e/ou Message.content) e trava a correcao.

Prova que:
  P. o payload REAL entregue a `whatsapp.send_template_message` carrega o valor
     RESOLVIDO — a inspecao e feita sobre `components`, nao sobre o historico;
  L. parametro literal ("Joao") continua passando intacto;
  V. qualquer variavel ja suportada pelas mensagens rapidas resolve aqui pelo
     MESMO resolvedor (`services/variables.py`), sem mapa proprio de token;
  F. FAIL CLOSED — variavel sem dado, desativada ou inexistente BLOQUEIA antes
     da Meta: zero chamadas ao provider, zero Message persistida;
  H. `Message.content` guarda o texto RESOLVIDO (o que o cliente recebeu);
  O. a ordem posicional {{1}}..{{N}} e preservada apos a resolucao;
  Z. template sem parametro continua funcionando;
  I. `/initiate` usa o mesmo builder e portanto a mesma resolucao;
  R. o preview do template usa os valores RESOLVIDOS (mesmo mecanismo do envio);
  G. guards estaticos: o frontend nao reimplementa resolucao de @TOKEN.

Meta API mockada; nenhuma credencial real, nenhuma requisicao de rede.
Roda standalone:  python tests/test_conversas_template_variables.py
"""
import os
import pathlib
import sys
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_template_variables_test.db"
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
from sqlalchemy import text as sql_text  # noqa: E402

import app.main as main  # noqa: E402
from app.database import engine, Base, SessionLocal  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.models.template import ServiceTemplate  # noqa: E402
from app.services import meta_templates  # noqa: E402
from app.services import whatsapp  # noqa: E402

failures = []


def _safe(text: str) -> str:
    """Console cp1252 do Windows nao imprime acento/emoji: exibe sem quebrar."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="backslashreplace").decode(encoding)


def check(cond, msg):
    if cond:
        print(f"  PASS: {_safe(msg)}")
    else:
        print(f"  FAIL: {_safe(msg)}")
        failures.append(msg)


# ─── Catalogo Meta mockado ────────────────────────────────────────────
# `cotacao_confirmar_recebimento` reproduz o template REAL do incidente:
# 1 parametro de BODY, sem header de midia, sem botao.
META_RAW = [
    {
        "name": "cotacao_confirmar_recebimento", "language": "pt_BR",
        "status": "APPROVED", "category": "UTILITY",
        "components": [{"type": "BODY", "text": "Olá {{1}}, tudo bem? Recebemos sua cotação."}],
    },
    {   # 3 parametros: prova que a ORDEM sobrevive a resolucao
        "name": "confirmacao_reserva", "language": "pt_BR",
        "status": "APPROVED", "category": "UTILITY",
        "components": [{"type": "BODY", "text": "Olá {{1}}! Reserva #{{2}} com {{3}}."}],
    },
    {   # 0 parametros: caminho sem variavel nenhuma
        "name": "aviso_simples", "language": "pt_BR",
        "status": "APPROVED", "category": "UTILITY",
        "components": [{"type": "BODY", "text": "Olá! Retomando nosso contato."}],
    },
]


async def _fake_fetch(base_url, waba_id, headers):
    return META_RAW


meta_templates._fetch_meta_templates = _fake_fetch
meta_templates.invalidate_catalog_cache()


# ─── Mock do provider: captura o payload EXATO enviado a Meta ─────────
# `components` e o que a Meta recebe de fato. O historico (Message.content) e
# verificado em separado — sem isso nao daria para distinguir "literal enviado"
# de "literal apenas renderizado no historico".
meta_calls = []
_wamid_seq = {"n": 0}


async def _fake_send_template(to, template_name, language, components, db=None):
    _wamid_seq["n"] += 1
    meta_calls.append({
        "to": to, "template_name": template_name,
        "language": language, "components": components,
    })
    return {"messages": [{"id": f"wamid.TPLVAR{_wamid_seq['n']}"}]}


whatsapp.send_template_message = _fake_send_template


def meta_params():
    """Valores posicionais do BODY no ULTIMO payload entregue a Meta."""
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
    nome = "Julia Atendente"
    email = "julia@bna.local"
    role = "ADMIN"
    is_active = True


main.app.dependency_overrides[get_current_user] = lambda: _AdminUser()
client = TestClient(main.app)

session = SessionLocal()

# Tabela `leads` do CRM (compartilhada). O lead e a fonte de `@PRIMEIRONOMECLIENTE`
# quando a conversa nao tem nome proprio.
session.execute(sql_text(
    "CREATE TABLE IF NOT EXISTS leads (id INTEGER PRIMARY KEY, nome TEXT, email TEXT)"
))
session.execute(sql_text(
    "INSERT INTO leads (id, nome, email) VALUES (7, 'João Pedro Baldo', 'joao@exemplo.com')"
))
session.execute(sql_text(
    "INSERT INTO leads (id, nome, email) VALUES (8, '', '')"
))

for t in META_RAW:
    session.add(ServiceTemplate(name=t["name"], language=t["language"]))

_AGORA = datetime.now(timezone.utc)
# Contato "desconhecido": Conversation.nome guarda o proprio numero, entao o
# nome REAL so pode vir do lead — e o cenario do incidente.
conv_lead = Conversation(
    lead_id=7, whatsapp="5511977776666", nome="5511977776666",
    status="aberta", atendente_id=1, is_bot_active=False,
    responsavel_nome="Julia Atendente", unread_count=0,
    last_customer_msg_at=_AGORA,
)
# Lead SEM nome: a variavel de cliente nao tem valor -> deve BLOQUEAR.
conv_sem_nome = Conversation(
    lead_id=8, whatsapp="5511955554444", nome="5511955554444",
    status="aberta", atendente_id=1, is_bot_active=False,
    unread_count=0, last_customer_msg_at=_AGORA,
)
session.add_all([conv_lead, conv_sem_nome])
session.commit()
CONV_LEAD, CONV_SEM_NOME = conv_lead.id, conv_sem_nome.id
session.close()

# Variaveis cadastradas — as MESMAS usadas pelas mensagens rapidas.
for payload in (
    {"token": "@PRIMEIRONOMECLIENTE", "name": "Primeiro Nome do Cliente",
     "kind": "dynamic", "source_key": "cliente.primeiro_nome"},
    {"token": "@NOMEATENDENTE", "name": "Nome do Atendente",
     "kind": "dynamic", "source_key": "atendente.nome"},
    {"token": "@PROTOCOLO", "name": "Protocolo",
     "kind": "dynamic", "source_key": "conversa.numero"},
    {"token": "@NOMEEMPRESA", "name": "Nome da Empresa",
     "kind": "fixed", "fixed_value": "Brasileiros no Atacama"},
    {"token": "@DESATIVADA", "name": "Desativada",
     "kind": "fixed", "fixed_value": "nunca sai"},
):
    r = client.post("/api/variables", json=payload)
    assert r.status_code == 201, (payload["token"], r.status_code, r.text)
    if payload["token"] == "@DESATIVADA":
        client.put(f"/api/variables/{r.json()['id']}", json={"is_active": False})


def send_template(conv_id, name, params, language="pt_BR"):
    return client.post(f"/api/conversations/{conv_id}/messages", json={
        "content": "ignorado — o corpo real vem da Meta",
        "msg_type": "template",
        "template_name": name,
        "template_language": language,
        "template_params": params,
    })


def messages_of(conv_id):
    sess = SessionLocal()
    try:
        return sess.query(Message).filter(
            Message.conversation_id == conv_id
        ).order_by(Message.id).all()
    finally:
        sess.close()


def last_content(conv_id):
    msgs = messages_of(conv_id)
    return msgs[-1].content if msgs else None


# ============ L. Parametro literal continua valendo ============
print("\nL — parametro manual (sem variavel)")

before = len(meta_calls)
r = send_template(CONV_LEAD, "cotacao_confirmar_recebimento", ["João"])
check(r.status_code == 200, f"envio com valor literal -> 200 (got {r.status_code}: {r.text[:160]})")
check(len(meta_calls) == before + 1, "Meta foi chamada exatamente uma vez")
check(meta_params() == ["João"], f"Meta recebe o literal intacto (got {meta_params()})")
check(last_content(CONV_LEAD) == "Olá João, tudo bem? Recebemos sua cotação.",
      f"historico usa o literal (got {last_content(CONV_LEAD)!r})")


# ============ P. @PRIMEIRONOMECLIENTE -> valor resolvido ============
print("\nP — @PRIMEIRONOMECLIENTE resolvido ANTES do payload da Meta")

before = len(meta_calls)
r = send_template(CONV_LEAD, "cotacao_confirmar_recebimento", ["@PRIMEIRONOMECLIENTE"])
check(r.status_code == 200, f"envio com variavel -> 200 (got {r.status_code}: {r.text[:160]})")
check(len(meta_calls) == before + 1, "Meta chamada uma vez")
check(meta_params() == ["João"],
      f"PAYLOAD DA META recebe o nome real, nunca o token (got {meta_params()})")
check(meta_params() != ["@PRIMEIRONOMECLIENTE"],
      "PAYLOAD DA META nao carrega o token literal")


# ============ H. Message.content = o que o cliente recebeu ============
print("\nH — historico reflete o texto realmente enviado")

check(last_content(CONV_LEAD) == "Olá João, tudo bem? Recebemos sua cotação.",
      f"Message.content usa o valor resolvido (got {last_content(CONV_LEAD)!r})")
check("@PRIMEIRONOMECLIENTE" not in (last_content(CONV_LEAD) or ""),
      "Message.content nao guarda o token literal")


# ============ V. Outras variaveis: MESMO resolvedor das quick replies ============
print("\nV — demais variaveis do catalogo resolvem pelo mesmo resolvedor")

r = send_template(CONV_LEAD, "cotacao_confirmar_recebimento", ["@NOMEATENDENTE"])
check(r.status_code == 200 and meta_params() == ["Julia Atendente"],
      f"variavel de FUNCIONARIO resolve (got {meta_params()})")

r = send_template(CONV_LEAD, "cotacao_confirmar_recebimento", ["@NOMEEMPRESA"])
check(r.status_code == 200 and meta_params() == ["Brasileiros no Atacama"],
      f"variavel FIXA resolve (got {meta_params()})")

r = send_template(CONV_LEAD, "cotacao_confirmar_recebimento", ["@PROTOCOLO"])
check(r.status_code == 200 and meta_params() == [str(CONV_LEAD)],
      f"variavel de CONVERSA resolve (got {meta_params()})")

# A prova de "mesmo resolvedor": o valor entregue a Meta e IDENTICO ao que a
# mensagem rapida produz para o mesmo token na mesma conversa.
r = client.post("/api/variables/preview", json={
    "text": "@PRIMEIRONOMECLIENTE", "conversation_id": CONV_LEAD,
})
quick_reply_value = r.json()["rendered"]
send_template(CONV_LEAD, "cotacao_confirmar_recebimento", ["@PRIMEIRONOMECLIENTE"])
check(meta_params() == [quick_reply_value],
      f"template e mensagem rapida produzem o MESMO valor (got {meta_params()} vs {quick_reply_value!r})")


# ============ O. Ordem posicional preservada ============
print("\nO — ordem dos parametros {{1}}..{{N}}")

r = send_template(CONV_LEAD, "confirmacao_reserva",
                  ["@PRIMEIRONOMECLIENTE", "5521", "@NOMEATENDENTE"])
check(r.status_code == 200, f"3 parametros mistos -> 200 (got {r.status_code}: {r.text[:160]})")
check(meta_params() == ["João", "5521", "Julia Atendente"],
      f"ordem preservada apos a resolucao (got {meta_params()})")
check(last_content(CONV_LEAD) == "Olá João! Reserva #5521 com Julia Atendente.",
      f"historico monta na ordem certa (got {last_content(CONV_LEAD)!r})")

# Ordem INVERTIDA nos valores deve produzir texto diferente — sem isto um
# builder que embaralhasse os parametros passaria no teste acima por simetria.
r = send_template(CONV_LEAD, "confirmacao_reserva",
                  ["@NOMEATENDENTE", "5521", "@PRIMEIRONOMECLIENTE"])
check(meta_params() == ["Julia Atendente", "5521", "João"],
      f"trocar a ordem troca o payload (got {meta_params()})")


# ============ F. FAIL CLOSED ============
print("\nF — fail closed: token nunca chega a Meta")

def blocked(conv_id, name, params, rotulo):
    before_calls = len(meta_calls)
    before_msgs = len(messages_of(conv_id))
    r = send_template(conv_id, name, params)
    check(r.status_code == 422, f"{rotulo} -> 422 (got {r.status_code})")
    check(len(meta_calls) == before_calls, f"{rotulo}: NENHUMA chamada a Meta")
    check(len(messages_of(conv_id)) == before_msgs, f"{rotulo}: NADA persistido")
    detail = r.json().get("detail", "")
    detail = detail if isinstance(detail, str) else str(detail)
    return detail

d = blocked(CONV_SEM_NOME, "cotacao_confirmar_recebimento",
            ["@PRIMEIRONOMECLIENTE"], "variavel sem dado no contato")
check("@PRIMEIRONOMECLIENTE" in d, f"erro NOMEIA a variavel que faltou (got {d!r})")

d = blocked(CONV_LEAD, "cotacao_confirmar_recebimento",
            ["@NAOEXISTE"], "variavel inexistente")
check("@NAOEXISTE" in d, f"erro nomeia a variavel inexistente (got {d!r})")

blocked(CONV_LEAD, "cotacao_confirmar_recebimento",
        ["@DESATIVADA"], "variavel desativada")

# Um unico parametro ruim entre varios bons tambem bloqueia o envio INTEIRO.
blocked(CONV_LEAD, "confirmacao_reserva",
        ["@PRIMEIRONOMECLIENTE", "5521", "@NAOEXISTE"], "um parametro ruim entre bons")


# ============ Z. Template sem parametro ============
print("\nZ — template sem variavel continua funcionando")

before = len(meta_calls)
r = send_template(CONV_LEAD, "aviso_simples", [])
check(r.status_code == 200, f"template 0 parametros -> 200 (got {r.status_code})")
check(len(meta_calls) == before + 1, "Meta chamada")
check(meta_calls[-1]["components"] == [], "sem parametros -> components vazio")
check(last_content(CONV_LEAD) == "Olá! Retomando nosso contato.", "historico do template sem parametro")

# E-mail no parametro nao pode ser corrompido pela resolucao.
r = send_template(CONV_LEAD, "cotacao_confirmar_recebimento", ["CONTATO@EMPRESA.COM"])
check(r.status_code == 200 and meta_params() == ["CONTATO@EMPRESA.COM"],
      f"e-mail em MAIUSCULAS atravessa intacto (got {meta_params()})")


# ============ I. /initiate usa o mesmo caminho ============
print("\nI — /initiate resolve pelo mesmo builder")

before = len(meta_calls)
r = client.post("/api/conversations/initiate", json={
    "whatsapp": "5511944443333", "nome": "5511944443333", "lead_id": 7,
    "template_name": "cotacao_confirmar_recebimento",
    "template_language": "pt_BR",
    "template_params": ["@PRIMEIRONOMECLIENTE"],
})
check(r.status_code == 200, f"/initiate -> 200 (got {r.status_code}: {r.text[:160]})")
check(len(meta_calls) == before + 1 and meta_params() == ["João"],
      f"/initiate entrega o valor resolvido a Meta (got {meta_params()})")
new_conv = r.json().get("conversation_id")
check(last_content(new_conv) == "Olá João, tudo bem? Recebemos sua cotação.",
      f"/initiate grava o historico resolvido (got {last_content(new_conv)!r})")

# Fail closed tambem no /initiate: numero novo, sem lead -> sem nome.
before = len(meta_calls)
r = client.post("/api/conversations/initiate", json={
    "whatsapp": "5511922221111", "nome": "5511922221111",
    "template_name": "cotacao_confirmar_recebimento",
    "template_language": "pt_BR",
    "template_params": ["@PRIMEIRONOMECLIENTE"],
})
check(r.status_code == 422, f"/initiate sem dado -> 422 (got {r.status_code})")
check(len(meta_calls) == before, "/initiate bloqueado: nenhuma chamada a Meta")


# ============ R. Preview usa os valores resolvidos ============
print("\nR — preview do template")

r = client.post("/api/variables/preview", json={
    "text": "", "texts": ["@PRIMEIRONOMECLIENTE"], "conversation_id": CONV_LEAD,
})
check(r.status_code == 200, f"preview de parametros -> 200 (got {r.status_code}: {r.text[:160]})")
body = r.json() if r.status_code == 200 else {}
check(body.get("rendered_list") == ["João"],
      f"preview devolve o parametro RESOLVIDO (got {body.get('rendered_list')})")
check(body.get("ok") is True, "preview sem problemas quando resolve")

r = client.post("/api/variables/preview", json={
    "text": "", "texts": ["@PRIMEIRONOMECLIENTE"], "conversation_id": CONV_SEM_NOME,
})
body = r.json()
check(body.get("ok") is False and body.get("problems"),
      "preview sinaliza problema quando a variavel nao resolve")

r = client.post("/api/variables/preview", json={
    "text": "", "texts": ["@PRIMEIRONOMECLIENTE", "5521", "@NOMEATENDENTE"],
    "conversation_id": CONV_LEAD,
})
check(r.json().get("rendered_list") == ["João", "5521", "Julia Atendente"],
      f"preview mantem a ordem dos parametros (got {r.json().get('rendered_list')})")

# Prova de nao-divergencia: preview e envio produzem os MESMOS valores.
send_template(CONV_LEAD, "confirmacao_reserva",
              ["@PRIMEIRONOMECLIENTE", "5521", "@NOMEATENDENTE"])
check(r.json().get("rendered_list") == meta_params(),
      f"preview == payload da Meta (got {r.json().get('rendered_list')} vs {meta_params()})")

# Contrato antigo intacto: `text` sozinho continua respondendo como antes.
r = client.post("/api/variables/preview", json={
    "text": "Olá @PRIMEIRONOMECLIENTE", "conversation_id": CONV_LEAD,
})
check(r.status_code == 200 and r.json()["rendered"] == "Olá João",
      f"preview de texto (mensagem rapida) inalterado (got {r.json().get('rendered')!r})")


# ============ S. Rota irma /api/templates/{id}/send ============
# Mesma funcao whatsapp.send_template_message, outro caminho: sem conversa nao
# ha o que resolver, entao o invariante e mantido BLOQUEANDO — nunca deixando
# o token vazar para a Meta.
print("\nS — rota irma /api/templates/{id}/send")

sess = SessionLocal()
from app.models.template import MessageTemplate  # noqa: E402

legacy = MessageTemplate(
    name="cotacao_confirmar_recebimento", language="pt_BR", status="APPROVED",
    category="UTILITY", body_text="Olá {{1}}, tudo bem? Recebemos sua cotação.",
)
sess.add(legacy)
sess.commit()
LEGACY_ID = legacy.id
sess.close()


def legacy_send(body_values):
    return client.post(f"/api/templates/{LEGACY_ID}/send",
                       json={"to": "5511999998888", "variables": {"body": body_values}})


before = len(meta_calls)
r = legacy_send(["João"])
check(r.status_code == 200, f"valor literal -> 200 (got {r.status_code}: {r.text[:120]})")
check(meta_params() == ["João"], f"literal chega intacto (got {meta_params()})")

before = len(meta_calls)
r = legacy_send(["@PRIMEIRONOMECLIENTE"])
check(r.status_code == 422, f"variavel de conversa sem contexto -> 422 (got {r.status_code})")
check(len(meta_calls) == before, "rota irma: NENHUMA chamada a Meta com token literal")

# Variavel FIXA nao depende de conversa: resolve normalmente ate aqui.
r = legacy_send(["@NOMEEMPRESA"])
check(r.status_code == 200 and meta_params() == ["Brasileiros no Atacama"],
      f"variavel FIXA resolve mesmo sem conversa (got {meta_params()})")


# ============ G. Guards estaticos do frontend ============
print("\nG — frontend nao reimplementa a resolucao")

js = (CONVERSAS_DIR / "static" / "js" / "conversas.js").read_text(encoding="utf-8")
check("PRIMEIRONOMECLIENTE" not in js,
      "conversas.js NAO tem mapping hardcoded de @PRIMEIRONOMECLIENTE")
check("/api/variables/preview" in js,
      "preview do template consome o endpoint do resolvedor (nao resolve em JS)")

py = (CONVERSAS_DIR / "app" / "routers" / "conversations.py").read_text(encoding="utf-8")
check("PRIMEIRONOMECLIENTE" not in py,
      "conversations.py NAO tem mapping hardcoded de @PRIMEIRONOMECLIENTE")
check("variables_service.render_strict" in py,
      "o builder reusa render_strict do resolvedor existente")

svc = (CONVERSAS_DIR / "app" / "services" / "variables.py").read_text(encoding="utf-8")
check(svc.count("def render(") == 1, "existe UMA unica funcao render() no sistema")


# ─── Resultado ────────────────────────────────────────────────────────
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FALHA(S):")
    for f in failures:
        print(f"  - {_safe(f)}")
    sys.exit(1)
print("TODOS OS CHECKS PASSARAM")
