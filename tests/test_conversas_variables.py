"""
CONV-VAR-01 — variaveis dinamicas (@TOKEN) em mensagens do Conversas.

Prova que:
  1. CRUD de variaveis (fixa e dinamica), duplicidade, token invalido;
  2. permissoes: criar/editar/excluir exigem admin; usar/listar nao;
  3. resolucao no BACKEND antes do envio (fixo, cliente, funcionario, empresa);
  4. formatacao preservada (acentos, negrito, quebras de linha, emoji,
     pontuacao, inicio/meio/fim, repeticao, multiplas variaveis);
  5. e-mails e mencoes comuns NAO sao corrompidos;
  6. variavel sem valor / desconhecida BLOQUEIAM o envio (nada persistido);
  7. o texto ORIGINAL da mensagem rapida permanece intacto e o historico
     guarda o texto RENDERIZADO;
  8. isolamento entre contatos (cliente A nunca recebe dado do cliente B);
  9. preview usa o mesmo mecanismo do envio;
 10. desativar a variavel bloqueia o envio;
 11. frase automatica resolve em modo tolerante (nunca bloqueia);
 12. guards estaticos do frontend (XSS-safe, insercao sem envio).

Roda standalone:  python tests/test_conversas_variables.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_variables_test.db"
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

import app.main as main  # noqa: E402
from app.database import engine, Base, SessionLocal  # noqa: E402
from app.auth import get_current_user, require_admin  # noqa: E402
from app.models.auto_reply import AutoReply, BusinessHours  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.models.quick_reply import QuickReply  # noqa: E402
from app.services import variables as variables_service  # noqa: E402
from app.services import whatsapp  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


Base.metadata.create_all(bind=engine)


class _AdminUser:
    id = 1
    nome = "Julia Atendente"
    email = "julia@bna.local"
    role = "admin"
    is_active = True


class _SellerUser:
    id = 2
    nome = "Vendedor Teste"
    email = "vendedor@bna.local"
    role = "user"
    is_active = True


CURRENT = {"user": _AdminUser()}


def _current_user():
    return CURRENT["user"]


async def _require_admin_override():
    from fastapi import HTTPException

    user = CURRENT["user"]
    if getattr(user, "role", None) != "admin":
        raise HTTPException(status_code=403, detail="Acesso restrito a administradores")
    return user


main.app.dependency_overrides[get_current_user] = _current_user
main.app.dependency_overrides[require_admin] = _require_admin_override
client = TestClient(main.app)


# ─── Mock do provider: NENHUMA chamada real a Meta ────────────────────
sent_payloads = []
_wamid_seq = {"n": 0}


async def _fake_send_text(to, message, db=None):
    _wamid_seq["n"] += 1
    sent_payloads.append({"to": to, "message": message})
    return {"messages": [{"id": f"wamid.TEST{_wamid_seq['n']}"}]}


whatsapp.send_text_message = _fake_send_text

# ─── Fixtures de dados ────────────────────────────────────────────────
session = SessionLocal()
conv_a = Conversation(
    lead_id=0, whatsapp="5511988887777", nome="João Pedro Baldo",
    status="aberta", responsavel_nome="Julia Atendente",
)
conv_b = Conversation(
    lead_id=0, whatsapp="5511911112222", nome="Érica  Souza",
    status="aberta", responsavel_nome="Outro Responsavel",
)
session.add_all([conv_a, conv_b])
session.add(BusinessHours(weekday=0, is_open=True, open_time="13:00", close_time="19:00"))
session.commit()
CONV_A, CONV_B = conv_a.id, conv_b.id
session.close()


def _create_var(payload):
    return client.post("/api/variables", json=payload)


def _send(conversation_id, content):
    return client.post(f"/api/conversations/{conversation_id}/messages",
                       json={"content": content, "msg_type": "text"})


def _last_sent():
    return sent_payloads[-1]["message"] if sent_payloads else None


# ============ 1. CRUD ============
print("VAR — CRUD de variaveis")

r = _create_var({"token": "@NOMEEMPRESA", "name": "Nome da Empresa",
                 "kind": "fixed", "fixed_value": "Brasileiros no Atacama"})
check(r.status_code == 201, f"criar variavel FIXA -> 201 (got {r.status_code})")
VAR_EMPRESA = r.json()["id"] if r.status_code == 201 else None

r = _create_var({"token": "@PRIMEIRONOMECLIENTE", "name": "Primeiro Nome do Cliente",
                 "kind": "dynamic", "source_key": "cliente.primeiro_nome"})
check(r.status_code == 201, "criar variavel DINAMICA -> 201")
check(r.json().get("source_label") == "Cliente: Primeiro Nome",
      "resposta traz o rotulo legivel da origem")

r = _create_var({"token": "@NOMECLIENTE", "name": "Nome Completo do Cliente",
                 "kind": "dynamic", "source_key": "cliente.nome_completo"})
check(r.status_code == 201, "criar @NOMECLIENTE (nome completo)")

r = _create_var({"token": "@NOMEATENDENTE", "name": "Nome do Atendente",
                 "kind": "dynamic", "source_key": "atendente.nome"})
check(r.status_code == 201, "criar @NOMEATENDENTE (funcionario)")

r = _create_var({"token": "@EXPEDIENTE", "name": "Expediente do Dia",
                 "kind": "dynamic", "source_key": "empresa.expediente_hoje"})
check(r.status_code == 201, "criar @EXPEDIENTE (empresa)")

# token normalizado (minusculo e sem @ viram MAIUSCULO com @)
r = _create_var({"token": "protocolo", "name": "Protocolo",
                 "kind": "dynamic", "source_key": "conversa.numero"})
check(r.status_code == 201 and r.json()["token"] == "@PROTOCOLO",
      "token normalizado: 'protocolo' -> '@PROTOCOLO'")
VAR_PROTOCOLO = r.json()["id"] if r.status_code == 201 else None

# duplicidade
r = _create_var({"token": "@NOMEEMPRESA", "name": "Dup", "kind": "fixed", "fixed_value": "x"})
check(r.status_code == 409, f"token duplicado -> 409 (got {r.status_code})")

# tokens invalidos
for bad in ("@PRIMEIRO NOME", "@", "@nome-com-hifen", "@A", "@NOME_", "@_NOME"):
    r = _create_var({"token": bad, "name": "Invalida", "kind": "fixed", "fixed_value": "x"})
    check(r.status_code == 422, f"token invalido {bad!r} -> 422 (got {r.status_code})")

# dinamica sem origem / com origem inexistente
r = _create_var({"token": "@SEMORIGEM", "name": "Sem origem", "kind": "dynamic"})
check(r.status_code == 422, "variavel dinamica sem origem -> 422")
r = _create_var({"token": "@ORIGEMFALSA", "name": "Origem falsa",
                 "kind": "dynamic", "source_key": "cliente.cpf_secreto"})
check(r.status_code == 422, "origem fora do catalogo -> 422 (catalogo controlado)")

# edicao
r = client.put(f"/api/variables/{VAR_EMPRESA}", json={"fixed_value": "Brasileiros no Atacama LTDA"})
check(r.status_code == 200 and r.json()["fixed_value"] == "Brasileiros no Atacama LTDA",
      "editar variavel -> 200")
client.put(f"/api/variables/{VAR_EMPRESA}", json={"fixed_value": "Brasileiros no Atacama"})

# exclusao
r = _create_var({"token": "@DESCARTAVEL", "name": "Descartavel", "kind": "fixed", "fixed_value": "x"})
TMP_ID = r.json()["id"]
r = client.delete(f"/api/variables/{TMP_ID}")
check(r.status_code == 200, "excluir variavel -> 200")
check(client.get(f"/api/variables/{TMP_ID}").status_code == 404, "variavel excluida some (404)")

# catalogo
cat = client.get("/api/variables/catalog").json()
groups = {g["group"] for g in cat["groups"]}
check({"Cliente", "Funcionário", "Empresa"}.issubset(groups),
      f"catalogo agrupado por Cliente/Funcionario/Empresa (got {sorted(groups)})")
check(all(o["key"] in variables_service.CATALOG for g in cat["groups"] for o in g["options"]),
      "catalogo exposto == catalogo do backend (sem propriedade inventada)")


# ============ 2. PERMISSOES ============
print("\nVAR — permissoes do CRUD")
CURRENT["user"] = _SellerUser()
r = _create_var({"token": "@PROIBIDA", "name": "Proibida", "kind": "fixed", "fixed_value": "x"})
check(r.status_code == 403, f"vendedor NAO cria variavel -> 403 (got {r.status_code})")
r = client.put(f"/api/variables/{VAR_EMPRESA}", json={"name": "Hack"})
check(r.status_code == 403, "vendedor NAO edita variavel -> 403")
r = client.delete(f"/api/variables/{VAR_EMPRESA}")
check(r.status_code == 403, "vendedor NAO exclui variavel -> 403")
r = client.get("/api/variables")
check(r.status_code == 200, "vendedor LISTA variaveis (precisa para inserir) -> 200")
CURRENT["user"] = _AdminUser()


# ============ 3. RESOLUCAO NO ENVIO ============
print("\nVAR — resolucao no backend antes do envio")

r = _send(CONV_A, "Bem-vindo à @NOMEEMPRESA!")
check(r.status_code == 200 and _last_sent() == "Bem-vindo à Brasileiros no Atacama!",
      f"valor FIXO resolvido (got {_last_sent()!r})")

r = _send(CONV_A, "Olá @PRIMEIRONOMECLIENTE!")
check(_last_sent() == "Olá João!", f"cliente: PRIMEIRO nome (got {_last_sent()!r})")

r = _send(CONV_A, "Olá @NOMECLIENTE!")
check(_last_sent() == "Olá João Pedro Baldo!", f"cliente: nome COMPLETO (got {_last_sent()!r})")

r = _send(CONV_A, "Falo com @NOMEATENDENTE.")
check(_last_sent() == "Falo com Julia Atendente.", f"funcionario: nome (got {_last_sent()!r})")

r = _send(CONV_A, "Protocolo @PROTOCOLO.")
check(_last_sent() == f"Protocolo {CONV_A}.", f"conversa: protocolo (got {_last_sent()!r})")

# acentos + espacos externos no nome de origem
r = _send(CONV_B, "Oi @PRIMEIRONOMECLIENTE!")
check(_last_sent() == "Oi Érica!", f"acentos preservados no primeiro nome (got {_last_sent()!r})")

check(variables_service.first_name("   Ana Maria") == "Ana", "espacos externos ignorados no primeiro nome")
check(variables_service.first_name("João Pedro Baldo") == "João", "unicode preservado (João)")


# ============ 4. POSICAO, FORMATACAO E REPETICAO ============
print("\nVAR — posicao, formatacao e repeticao")

r = _send(CONV_A, "@PRIMEIRONOMECLIENTE, tudo bem?")
check(_last_sent() == "João, tudo bem?", "variavel no INICIO + pontuacao")

r = _send(CONV_A, "Oi @PRIMEIRONOMECLIENTE, tudo bem?")
check(_last_sent() == "Oi João, tudo bem?", "variavel no MEIO")

r = _send(CONV_A, "Atenciosamente, @NOMEATENDENTE")
check(_last_sent() == "Atenciosamente, Julia Atendente", "variavel no FIM")

r = _send(CONV_A, "*@NOMEEMPRESA* fala com _@PRIMEIRONOMECLIENTE_ e ~@PROTOCOLO~")
check(_last_sent() == f"*Brasileiros no Atacama* fala com _João_ e ~{CONV_A}~",
      f"negrito/italico/riscado do WhatsApp preservados (got {_last_sent()!r})")

r = _send(CONV_A, "Olá @PRIMEIRONOMECLIENTE!\nAqui é a @NOMEEMPRESA.\n\nAté!")
check(_last_sent() == "Olá João!\nAqui é a Brasileiros no Atacama.\n\nAté!",
      "quebras de linha preservadas")

r = _send(CONV_A, "Oi @PRIMEIRONOMECLIENTE 🎉🏔️ bem-vindo!")
check(_last_sent() == "Oi João 🎉🏔️ bem-vindo!", "emojis preservados")

r = _send(CONV_A, "@PRIMEIRONOMECLIENTE, a @NOMEEMPRESA agradece, @PRIMEIRONOMECLIENTE!")
check(_last_sent() == "João, a Brasileiros no Atacama agradece, João!",
      "multiplas variaveis + mesma variavel repetida")

r = _send(CONV_A, "Mensagem sem nenhuma variavel.")
check(_last_sent() == "Mensagem sem nenhuma variavel.", "mensagem SEM variavel passa intacta")


# ============ 5. E-MAILS E MENCOES NAO SAO CORROMPIDOS ============
print("\nVAR — e-mails e mencoes comuns")

r = _send(CONV_A, "Escreva para contato@empresa.com que resolvemos.")
check(_last_sent() == "Escreva para contato@empresa.com que resolvemos.",
      f"e-mail minusculo intacto (got {_last_sent()!r})")

r = _send(CONV_A, "Escreva para CONTATO@EMPRESA.COM.BR agora.")
check(_last_sent() == "Escreva para CONTATO@EMPRESA.COM.BR agora.",
      f"e-mail MAIUSCULO intacto (got {_last_sent()!r})")

r = _send(CONV_A, "Me chama no @insta da agencia.")
check(_last_sent() == "Me chama no @insta da agencia.", "mencao minuscula intacta")

r = _send(CONV_A, "Custo: 100@ unidade.")
check(_last_sent() == "Custo: 100@ unidade.", "'@' solto intacto")


# ============ 6. BLOQUEIO: SEM VALOR / DESCONHECIDA / INATIVA ============
print("\nVAR — bloqueio de envio")

before = len(sent_payloads)
r = _send(CONV_A, "Ola @VARIAVELINEXISTENTE, tudo bem?")
check(r.status_code == 422, f"variavel DESCONHECIDA bloqueia o envio -> 422 (got {r.status_code})")
check("@VARIAVELINEXISTENTE" in r.json()["detail"] and "não é uma variável cadastrada" in r.json()["detail"],
      f"mensagem simples informa o token nao reconhecido (got {r.json().get('detail')!r})")
check(len(sent_payloads) == before, "NADA foi enviado ao WhatsApp no bloqueio")

session = SessionLocal()
msgs = session.query(Message).filter(Message.content.like("%VARIAVELINEXISTENTE%")).count()
session.close()
check(msgs == 0, "NADA foi persistido no historico no bloqueio (token nunca vira mensagem)")

# variavel FIXA sem valor configurado
r = _create_var({"token": "@SEMVALOR", "name": "Fixa sem valor", "kind": "fixed"})
check(r.status_code == 201, "variavel fixa pode ser criada sem valor (admin preenche depois)")
r = _send(CONV_A, "Somos a @SEMVALOR.")
check(r.status_code == 422 and "não possui um valor configurado" in r.json()["detail"],
      f"variavel FIXA sem valor bloqueia (got {r.json().get('detail')!r})")

# variavel DINAMICA sem valor para este contato (lead sem e-mail)
r = _create_var({"token": "@EMAILCLIENTE", "name": "Email do Cliente",
                 "kind": "dynamic", "source_key": "cliente.email"})
check(r.status_code == 201, "criar @EMAILCLIENTE")
r = _send(CONV_A, "Confirma o e-mail @EMAILCLIENTE?")
check(r.status_code == 422 and "não possui valor para este contato" in r.json()["detail"],
      f"variavel DINAMICA sem valor bloqueia (got {r.json().get('detail')!r})")

# desativacao
r = client.put(f"/api/variables/{VAR_PROTOCOLO}", json={"is_active": False})
check(r.status_code == 200 and r.json()["is_active"] is False, "desativar variavel -> 200")
r = _send(CONV_A, "Protocolo @PROTOCOLO.")
check(r.status_code == 422, "variavel DESATIVADA bloqueia o envio")
check(client.get("/api/variables").json()["total"] < client.get("/api/variables?active_only=false").json()["total"],
      "inativa some da lista padrao (seletor so mostra ativas)")
client.put(f"/api/variables/{VAR_PROTOCOLO}", json={"is_active": True})


# ============ 7. ORIGINAL PRESERVADO / HISTORICO RENDERIZADO ============
print("\nVAR — original preservado e historico renderizado")

qr = client.post("/api/quick-replies", json={
    "shortcut": "/bemvindo", "title": "Boas-vindas",
    "content": "Olá @PRIMEIRONOMECLIENTE! Aqui é a @NOMEEMPRESA.",
}).json()
QR_ID = qr["id"]

r = _send(CONV_A, qr["content"])
check(r.status_code == 200, "envio a partir do texto da mensagem rapida -> 200")
check(_last_sent() == "Olá João! Aqui é a Brasileiros no Atacama.",
      f"mensagem rapida resolvida no envio (got {_last_sent()!r})")
check(r.json()["content"] == "Olá João! Aqui é a Brasileiros no Atacama.",
      "historico guarda o texto RENDERIZADO (foi o que o cliente recebeu)")

original = client.get(f"/api/quick-replies/{QR_ID}").json()["content"]
check(original == "Olá @PRIMEIRONOMECLIENTE! Aqui é a @NOMEEMPRESA.",
      f"texto ORIGINAL da mensagem rapida permanece intacto (got {original!r})")

session = SessionLocal()
stored = session.query(QuickReply).filter(QuickReply.id == QR_ID).first().content
session.close()
check("@PRIMEIRONOMECLIENTE" in stored, "banco da mensagem rapida NAO foi reescrito")


# ============ 8. ISOLAMENTO ENTRE CONTATOS ============
print("\nVAR — isolamento entre contatos")

_send(CONV_A, "Oi @PRIMEIRONOMECLIENTE")
a_text = _last_sent()
_send(CONV_B, "Oi @PRIMEIRONOMECLIENTE")
b_text = _last_sent()
check(a_text == "Oi João" and b_text == "Oi Érica",
      f"cada conversa resolve com o SEU contato (A={a_text!r}, B={b_text!r})")
check("João" not in b_text, "cliente B nunca recebe dado do cliente A")

_send(CONV_A, "Oi @PRIMEIRONOMECLIENTE")
check(_last_sent() == "Oi João", "voltar para A nao reaproveita o contexto de B (sem cache entre envios)")


# ============ 9. PREVIEW COERENTE COM O ENVIO ============
print("\nVAR — preview")

p = client.post("/api/variables/preview",
                json={"text": "Olá @PRIMEIRONOMECLIENTE, aqui é a @NOMEEMPRESA.",
                      "conversation_id": CONV_A}).json()
check(p["ok"] is True and p["rendered"] == "Olá João, aqui é a Brasileiros no Atacama.",
      f"preview == texto realmente enviado (got {p['rendered']!r})")

p = client.post("/api/variables/preview",
                json={"text": "Ola @VARIAVELINEXISTENTE", "conversation_id": CONV_A}).json()
check(p["ok"] is False and p["problems"][0]["token"] == "@VARIAVELINEXISTENTE",
      "preview aponta claramente a variavel sem valor/desconhecida")
check("@VARIAVELINEXISTENTE" not in p["rendered"],
      "preview tambem nao exibe o token literal (coerente com o envio)")


# ============ 10. FRASE AUTOMATICA — MODO TOLERANTE ============
print("\nVAR — frase automatica (modo tolerante)")

session = SessionLocal()
session.add(AutoReply(trigger="end_service", title="Encerramento",
                      message="Obrigado @PRIMEIRONOMECLIENTE! Atendido por @NOMEATENDENTE.",
                      is_active=True))
session.commit()
session.close()

ctx_sem_atendente = variables_service.VariableContext(
    SessionLocal(), conversation=None, user=None)
db_tmp = SessionLocal()
conv = db_tmp.query(Conversation).filter(Conversation.id == CONV_A).first()
lenient = variables_service.render_lenient(
    db_tmp, "Obrigado @PRIMEIRONOMECLIENTE! Atendido por @NOMEATENDENTE.",
    variables_service.VariableContext(db_tmp, conversation=conv, user=None))
db_tmp.close()
check("@NOMEATENDENTE" not in lenient and "@PRIMEIRONOMECLIENTE" not in lenient,
      "modo tolerante NUNCA envia o token literal")
check("João" in lenient, f"modo tolerante resolve o que da (got {lenient!r})")
check(lenient == "Obrigado João! Atendido por .", f"texto tolerante previsivel (got {lenient!r})")

before = len(sent_payloads)
r = client.put(f"/api/conversations/{CONV_A}", json={"status": "encerrada"})
check(r.status_code == 200, "encerrar conversa -> 200 (frase automatica NAO bloqueia)")
check(len(sent_payloads) > before and "João" in sent_payloads[-1]["message"],
      f"frase automatica enviada com a variavel resolvida (got {sent_payloads[-1]['message']!r})")
client.put(f"/api/conversations/{CONV_A}", json={"status": "aberta"})


# ============ 11. SEGURANCA — sem execucao arbitraria / sem segredo ============
print("\nVAR — seguranca")

svc_src = (CONVERSAS_DIR / "app" / "services" / "variables.py").read_text(encoding="utf-8")
for forbidden in ("eval(", "exec(", "__import__", "subprocess"):
    check(forbidden not in svc_src, f"resolver NAO usa {forbidden} (sem execucao arbitraria)")
check(svc_src.count("compile(") == svc_src.count("re.compile("),
      "unico 'compile(' do resolver e o de regex (nunca compilacao de codigo)")

# valor com aparencia de token nao vira variavel (sem recursao)
client.put(f"/api/variables/{VAR_EMPRESA}", json={"fixed_value": "Grupo @PRIMEIRONOMECLIENTE S/A"})
r = _send(CONV_A, "Somos a @NOMEEMPRESA.")
check(_last_sent() == "Somos a Grupo @PRIMEIRONOMECLIENTE S/A.",
      f"valor substituido NAO e re-escaneado (sem recursao) (got {_last_sent()!r})")
client.put(f"/api/variables/{VAR_EMPRESA}", json={"fixed_value": "Brasileiros no Atacama"})

# valor com backreference de regex nao quebra a substituicao
client.put(f"/api/variables/{VAR_EMPRESA}", json={"fixed_value": r"BNA \1 \g<0> $1"})
r = _send(CONV_A, "Empresa: @NOMEEMPRESA")
check(_last_sent() == r"Empresa: BNA \1 \g<0> $1",
      f"backreferences no valor sao literais (got {_last_sent()!r})")
client.put(f"/api/variables/{VAR_EMPRESA}", json={"fixed_value": "Brasileiros no Atacama"})

body = client.get("/api/variables?active_only=false").text
check("test-secret-key" not in body, "resposta sem SECRET_KEY")

# sem autenticacao -> 401
main.app.dependency_overrides.pop(get_current_user)
main.app.dependency_overrides.pop(require_admin)
anon = TestClient(main.app)
check(anon.get("/api/variables").status_code == 401, "listar sem autenticacao -> 401")
check(anon.post("/api/variables", json={"token": "@X1", "name": "x", "kind": "fixed"}).status_code == 401,
      "criar sem autenticacao -> 401")
main.app.dependency_overrides[get_current_user] = _current_user
main.app.dependency_overrides[require_admin] = _require_admin_override


# ============ 12. FRONTEND — guards estaticos ============
print("\nVAR — guards estaticos do frontend")

js_conv = (CONVERSAS_DIR / "static" / "js" / "conversas.js").read_text(encoding="utf-8")
js_set = (CONVERSAS_DIR / "static" / "js" / "settings.js").read_text(encoding="utf-8")
html_conv = (CONVERSAS_DIR / "templates" / "conversas.html").read_text(encoding="utf-8")
html_set = (CONVERSAS_DIR / "templates" / "settings.html").read_text(encoding="utf-8")
css = (CONVERSAS_DIR / "static" / "css" / "conversas.css").read_text(encoding="utf-8")

check('id="varPalette"' in html_conv and 'role="listbox"' in html_conv,
      "seletor de variaveis presente no composer (listbox)")
check('id="btnVars"' in html_conv, "botao de inserir variavel presente")
check('data-tab="variables"' in html_set and 'id="panel-variables"' in html_set,
      "aba de Variaveis presente em /settings")
check('id="varModalOverlay"' in html_set and 'name="varKind"' in html_set,
      "modal com escolha Fixo/Variavel presente")
check('id="varFixedGroup"' in html_set and 'id="varDynamicGroup"' in html_set,
      "campo de valor fixo E seletor de propriedade presentes")
check('id="qrVarInsert"' in html_set, "insercao de variavel no editor de mensagem rapida")
check(".var-palette" in css and ".var-item" in css, "CSS das variaveis presente")
check("conversas.js?v=" in html_conv and "settings.js?v=" in html_set, "cache-bust atualizado")

start = js_conv.find("CONV-VAR-01: seletor de variaveis")
end = js_conv.find("─── Helpers", start)
check(start != -1 and end > start, "secao do seletor presente no conversas.js")
section = js_conv[start:end]
check("createElement" in section and ".textContent =" in section,
      "itens do seletor renderizados com createElement/textContent")
check("innerHTML" not in section, "NENHUM innerHTML na secao do seletor de variaveis")
check("replaceChildren()" in section, "limpeza do seletor via replaceChildren")
check("sendMessage" not in section, "seletor de variaveis NUNCA chama sendMessage (so insere)")
check("setSelectionRange" in section, "insercao acontece na posicao do cursor")
check("document.addEventListener('keydown'" not in js_conv,
      "NENHUM keydown global (regressao CONV-HOTFIX-QUICK-REPLIES-01)")
check("resp.status === 422" in js_conv,
      "frontend devolve o texto ao composer quando o backend bloqueia (422)")
check("&quot;" in js_set and "&#39;" in js_set,
      "escapeHtml do settings.js escapa aspas (SEC-CONV-01)")

# a paleta de mensagens rapidas continua intacta (regressao)
qr_start = js_conv.find("CONV-HOTFIX-QUICK-REPLIES-01: paleta")
qr_end = js_conv.find("CONV-07: Atribuicao", qr_start)
qr_section = js_conv[qr_start:qr_end]
check(qr_start != -1 and qr_end > qr_start, "secao da paleta '/' preservada")
check("innerHTML" not in qr_section and "sendMessage" not in qr_section,
      "secao da paleta '/' segue sem innerHTML e sem sendMessage")


# --- Resultado ---
main.app.dependency_overrides.clear()
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE VARIAVEIS PASSARAM")
