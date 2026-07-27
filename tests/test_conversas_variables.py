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
from app.auth import get_current_user  # noqa: E402
from app.models.auto_reply import AutoReply, BusinessHours  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.models.quick_reply import QuickReply  # noqa: E402
from app.services import variables as variables_service  # noqa: E402
from app.services import whatsapp  # noqa: E402

failures = []


def _safe(text: str) -> str:
    """
    O console do Windows usa cp1252 e explode ao imprimir emoji. Como algumas
    mensagens de teste carregam o texto sob teste (que pode ter emoji),
    imprimimos sempre em uma forma que o terminal aceite. Nao afeta a
    comparacao — so a exibicao.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="backslashreplace").decode(encoding)


def check(cond, msg):
    if cond:
        print(f"  PASS: {_safe(msg)}")
    else:
        print(f"  FAIL: {_safe(msg)}")
        failures.append(msg)


Base.metadata.create_all(bind=engine)


class _AdminUser:
    id = 1
    nome = "Julia Atendente"
    email = "julia@bna.local"
    # CONV-VAR-01-HOTFIX-ADMIN-01: usa a forma REAL de producao ("ADMIN", o
    # NOME do membro do enum, que e o que o SQLAlchemy grava na coluna). Com
    # "admin" minusculo esta suite passaria mesmo com o bug de volta.
    role = "ADMIN"
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


# CONV-VAR-01-HOTFIX-ADMIN-01: apenas `get_current_user` e substituido. O
# `require_admin` REAL roda (ele depende de get_current_user, entao recebe o
# usuario injetado). Antes havia um duble de require_admin aqui que replicava
# a comparacao literal `role != "admin"` — ele reproduzia o bug e por isso a
# suite passava enquanto producao devolvia 403 para administradores.
main.app.dependency_overrides[get_current_user] = _current_user
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
# lead_id REAL: a tabela `leads` (do CRM) nao existe no banco de teste do
# Conversas, entao a leitura do CRM levanta de verdade — e so assim o caminho
# de rollback de VariableContext.lead() e realmente exercitado.
conv_crm = Conversation(
    lead_id=42, whatsapp="5511933334444", nome="Carlos Lead CRM",
    status="aberta", responsavel_nome="Julia Atendente",
)
session.add_all([conv_a, conv_b, conv_crm])
session.add(BusinessHours(weekday=0, is_open=True, open_time="13:00", close_time="19:00"))
session.commit()
CONV_A, CONV_B, CONV_CRM = conv_a.id, conv_b.id, conv_crm.id
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

# troca de tipo exige o campo do NOVO tipo na mesma requisicao — senao a
# variavel ficaria salva sem valor e passaria a bloquear todo envio em silencio
r = _create_var({"token": "@TROCATIPO", "name": "Troca de tipo",
                 "kind": "dynamic", "source_key": "cliente.primeiro_nome"})
TROCA_ID = r.json()["id"]
r = client.put(f"/api/variables/{TROCA_ID}", json={"kind": "fixed"})
check(r.status_code == 422, f"dynamic->fixed sem valor -> 422 (got {r.status_code})")
r = client.put(f"/api/variables/{TROCA_ID}", json={"kind": "fixed", "fixed_value": "Valor novo"})
check(r.status_code == 200 and r.json()["source_key"] is None,
      "dynamic->fixed com valor -> 200 e limpa a origem")
r = client.put(f"/api/variables/{TROCA_ID}", json={"kind": "dynamic"})
check(r.status_code == 422, "fixed->dynamic sem origem -> 422 (simetrico)")
client.delete(f"/api/variables/{TROCA_ID}")

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

# `_is_strict_position` e uma reimplementacao manual dos lookbehinds de
# TOKEN_PATTERN (a varredura precisa ver tambem as posicoes recusadas).
# Este cruzamento garante que as duas implementacoes nunca divirjam.
_amostras = [
    "@NOMEEMPRESA no inicio", "texto @NOMEEMPRESA no meio", "no fim @NOMEEMPRESA",
    "*@A_B* _@C1_ ~@DE~", "contato@empresa.com", "CONTATO@EMPRESA.COM",
    "José@EMPRESA", "Bom dia.@NOME", "@UM@DOIS", "linha1\n@TOK, fim.",
    "100@NOME", "_@NOME_", "@N", "@", "email a@B.com e @TOK", "@NOME__ x",
    # escapes: TOKEN_PATTERN ja recusa posicao precedida de '@', entao a
    # equivalencia continua valendo com a sintaxe nova
    "@@NOME", "x@@NOME", "@@NOME e @OUTRO", "@@", "texto sem arroba",
]
_esperado = {}
for s in _amostras:
    vistos = []
    for m in variables_service.TOKEN_PATTERN.finditer(s):
        tok = "@" + m.group(1)
        if tok not in vistos:
            vistos.append(tok)
    _esperado[s] = vistos
_diff = [(s, variables_service.find_tokens(s), _esperado[s])
         for s in _amostras if variables_service.find_tokens(s) != _esperado[s]]
check(not _diff, f"varredura concorda com TOKEN_PATTERN em todas as amostras (divergencias: {_diff})")


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


# ============ 5b. TOKEN COLADO A OUTRO TEXTO (posicao ambigua) ============
# Achado da code review: sem a varredura larga, um token em posicao recusada
# pelo padrao estrito era IGNORADO e chegava LITERAL ao cliente.
print("\nVAR — token colado a outro texto")

for texto, rotulo in (
    ("@PRIMEIRONOMECLIENTE@NOMEEMPRESA", "dois tokens colados"),
    ("Bom dia.@PRIMEIRONOMECLIENTE", "token colado a um ponto"),
    ("Pedido 100@NOMEEMPRESA", "token colado a um numero"),
):
    before = len(sent_payloads)
    r = _send(CONV_A, texto)
    check(r.status_code == 422, f"{rotulo} -> bloqueia o envio (got {r.status_code})")
    check("colada a outro texto" in r.json().get("detail", ""),
          f"{rotulo}: mensagem explica como corrigir")
    check(len(sent_payloads) == before, f"{rotulo}: nada foi enviado ao WhatsApp")

# e-mail MAIUSCULO cujo dominio NAO e token cadastrado segue literal
r = _send(CONV_A, "Envie para JOAO@GMAIL.COM hoje.")
check(r.status_code == 200 and _last_sent() == "Envie para JOAO@GMAIL.COM hoje.",
      f"e-mail com dominio nao cadastrado permanece literal (got {_last_sent()!r})")


# ============ 5c. ESCAPE DE ARROBA LITERAL (@@) ============
# CONV-VAR-01-HARD-01: `@@TEXTO` produz `@TEXTO` literal. Processado ANTES da
# deteccao de variaveis e nunca reinterpretado.
print("\nVAR — escape de arroba literal (@@)")

escapes = [
    ("Siga @@BRASILEIROSNOATACAMA",            "Siga @BRASILEIROSNOATACAMA",            "handle em CAIXA ALTA nao vira variavel"),
    ("@@NOMEEMPRESA",                          "@NOMEEMPRESA",                          "token CADASTRADO escapado sai literal"),
    ("@@INICIO do texto",                      "@INICIO do texto",                      "escape no INICIO"),
    ("texto @@MEIO texto",                     "texto @MEIO texto",                     "escape no MEIO"),
    ("texto no @@FIM",                         "texto no @FIM",                         "escape no FIM"),
    ("*@@NEGRITO* e _@@ITALICO_",              "*@NEGRITO* e _@ITALICO_",               "escape dentro de negrito e italico"),
    ("Fale com contato@empresa.com ou @@INSTA", "Fale com contato@empresa.com ou @INSTA", "e-mail junto de escape"),
    ("@@UM @@DOIS",                            "@UM @DOIS",                             "dois escapes na mesma mensagem"),
    ("Oi @PRIMEIRONOMECLIENTE, siga @@BNAOFICIAL 🎉",
     "Oi João, siga @BNAOFICIAL 🎉",                                                     "variavel + escape + emoji"),
    ("linha1 @@TAG\nlinha2 @@TAG",             "linha1 @TAG\nlinha2 @TAG",              "escape com quebra de linha"),
]
for entrada, esperado, rotulo in escapes:
    r = _send(CONV_A, entrada)
    ok = r.status_code == 200 and _last_sent() == esperado
    check(ok, f"{rotulo}: {entrada!r} -> {esperado!r} (got {r.status_code}/{_last_sent()!r})")

# duas arrobas produzem EXATAMENTE uma
r = _send(CONV_A, "@@")
check(_last_sent() == "@", f"'@@' sozinho -> '@' (got {_last_sent()!r})")

# escape NAO desliga a variavel simples nem o bloqueio de desconhecida
r = _send(CONV_A, "@@LITERAL e @PRIMEIRONOMECLIENTE")
check(_last_sent() == "@LITERAL e João",
      f"escape e variavel convivem na mesma mensagem (got {_last_sent()!r})")
r = _send(CONV_A, "Siga @BRASILEIROSNOATACAMA")
check(r.status_code == 422,
      "arroba SIMPLES desconhecida em caixa alta continua bloqueada no modo estrito")

# o texto escapado nunca e reinterpretado como variavel
_, probs = variables_service.render(
    SessionLocal(), "@@NOMEEMPRESA @@VARIAVELINEXISTENTE",
    variables_service.VariableContext(SessionLocal()))
check(probs == [], f"texto escapado nao gera problema de variavel (got {probs})")
check(variables_service.find_tokens("@@NOMEEMPRESA") == [],
      "escape nao produz token para a varredura")

# arrobas em sequencia e casos degenerados — o texto sempre sai integro
_db_esc = SessionLocal()
_ctx_esc = variables_service.VariableContext(_db_esc)
for entrada, esperado, rotulo in [
    ("@@@NOMEEMPRESA", "@Brasileiros no Atacama", "@@@TOKEN = arroba literal + variavel"),
    ("@@@@NOMEEMPRESA", "@@NOMEEMPRESA", "@@@@TOKEN = duas arrobas literais + texto"),
    ("@@ @@", "@ @", "escapes separados por espaco"),
    ("@", "@", "arroba sozinha intacta"),
    ("texto@", "texto@", "arroba no fim intacta"),
    ("@1", "@1", "arroba + 1 caractere intacta"),
    ("@_A", "@_A", "arroba + underscore intacta"),
    ("", "", "texto vazio"),
]:
    saida, _p = variables_service.render(_db_esc, entrada, _ctx_esc)
    check(saida == esperado, f"{rotulo}: {entrada!r} -> {esperado!r} (got {saida!r})")
_db_esc.close()


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

# variavel inativa usada mais adiante (preview e frases automaticas)
r = _create_var({"token": "@INATIVAAUTO", "name": "Inativa", "kind": "fixed", "fixed_value": "Valor X"})
client.put(f"/api/variables/{r.json()['id']}", json={"is_active": False})

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


# ============ 8b. LEGENDA DE MIDIA USA A MESMA RESOLUCAO ============
# Achado da code review: a legenda vem do MESMO composer que o seletor "@"
# alimenta — sem resolucao, anexar um arquivo enviaria o token literal.
print("\nVAR — legenda de midia")

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def _send_media(conversation_id, caption):
    return client.post(
        f"/api/conversations/{conversation_id}/messages/media",
        files={"file": ("foto.png", _PNG, "image/png")},
        data={"caption": caption},
    )


r = _send_media(CONV_A, "Olha isso @VARIAVELINEXISTENTE!")
check(r.status_code == 422, f"legenda com token desconhecido bloqueia -> 422 (got {r.status_code})")
check("@VARIAVELINEXISTENTE" in r.json().get("detail", ""),
      "legenda bloqueada informa o token nao reconhecido")

session = SessionLocal()
leaked = session.query(Message).filter(Message.content.like("%VARIAVELINEXISTENTE%")).count()
session.close()
check(leaked == 0, "legenda bloqueada nao persiste mensagem nem asset")

r = _send_media(CONV_A, "Oi @PRIMEIRONOMECLIENTE, veja @@BNAOFICIAL")
check(r.status_code == 200, f"legenda valida envia -> 200 (got {r.status_code})")
check(r.json()["content"] == "Oi João, veja @BNAOFICIAL",
      f"legenda resolvida e escapada no historico (got {r.json().get('content')!r})")


# ============ 8c. BRANCH media_url DO POST /messages ============
# CONV-VAR-01-HARD-01: branch so-API (o frontend usa multipart), mas tem
# conversation_id — entao passa pela mesma resolucao estrita.
print("\nVAR — POST /messages com media_url")

before = len(sent_payloads)
r = client.post(f"/api/conversations/{CONV_A}/messages", json={
    "content": "Legenda @VARIAVELINEXISTENTE", "msg_type": "image",
    "media_url": "https://exemplo.test/foto.png"})
check(r.status_code == 422, f"media_url com token invalido bloqueia -> 422 (got {r.status_code})")
check(len(sent_payloads) == before, "media_url bloqueado nao chega ao WhatsApp")

r = client.post(f"/api/conversations/{CONV_A}/messages", json={
    "content": "Legenda de @PRIMEIRONOMECLIENTE", "msg_type": "image",
    "media_url": "https://exemplo.test/foto.png"})
check(r.status_code == 200 and r.json()["content"] == "Legenda de João",
      f"media_url resolve e persiste renderizado (got {r.status_code}/{r.json().get('content')!r})")


# ============ 8d. EXCLUSAO DE VARIAVEL EM USO ============
print("\nVAR — protecao de exclusao")

r = _create_var({"token": "@EMUSO", "name": "Em uso", "kind": "fixed", "fixed_value": "valor"})
EMUSO_ID = r.json()["id"]
r = _create_var({"token": "@SEMUSO", "name": "Sem uso", "kind": "fixed", "fixed_value": "valor"})
SEMUSO_ID = r.json()["id"]

# sem referencias -> exclui
r = client.delete(f"/api/variables/{SEMUSO_ID}")
check(r.status_code == 200, f"variavel SEM uso e excluida -> 200 (got {r.status_code})")

# referencia em mensagem rapida
qr_ref = client.post("/api/quick-replies", json={
    "shortcut": "/usaemuso", "title": "Usa em uso",
    "content": "Bem-vindo a @EMUSO!"}).json()
r = client.delete(f"/api/variables/{EMUSO_ID}")
check(r.status_code == 409, f"variavel EM USO nao e excluida -> 409 (got {r.status_code})")
detalhe = r.json().get("detail", "")
check("@EMUSO" in detalhe and "usada em 1 mensagem." in detalhe,
      f"mensagem cita token e contagem exata (got {detalhe!r})")
check("desative" in detalhe.lower(), "mensagem orienta a desativar a variavel")

# renomear tambem quebraria as referencias -> mesmo bloqueio (senao a
# protecao de exclusao seria contornavel por um rename)
r = client.put(f"/api/variables/{EMUSO_ID}", json={"token": "@EMUSORENOMEADA"})
check(r.status_code == 409, f"renomear variavel EM USO -> 409 (got {r.status_code})")
check("renomear" in r.json().get("detail", ""), "mensagem do rename fala em renomear")
r = client.put(f"/api/variables/{EMUSO_ID}", json={"name": "Novo nome legivel"})
check(r.status_code == 200, "editar o NOME (sem mexer no token) continua permitido")

# comparacao EXATA: um token que e prefixo de outro nao conta como referencia
r2 = _create_var({"token": "@EMUSOMAIS", "name": "Prefixo", "kind": "fixed", "fixed_value": "v"})
PREFIXO_ID = r2.json()["id"]
uso_prefixo = client.get(f"/api/variables/{PREFIXO_ID}/usage").json()
check(uso_prefixo["total"] == 0,
      f"@EMUSOMAIS nao conta a referencia de @EMUSO (comparacao exata, got {uso_prefixo})")
check(client.delete(f"/api/variables/{PREFIXO_ID}").status_code == 200,
      "variavel com token prefixado por outra em uso e excluivel")

# referencia tambem em resposta automatica -> contagem soma
session = SessionLocal()
session.add(AutoReply(trigger="greeting", title="Saudacao",
                      message="Ola! Aqui e a @EMUSO.", is_active=True))
session.commit()
session.close()
r = client.delete(f"/api/variables/{EMUSO_ID}")
check(r.status_code == 409 and "2 mensagens" in r.json().get("detail", ""),
      f"conta mensagens rapidas + respostas automaticas (got {r.json().get('detail')!r})")

uso = client.get(f"/api/variables/{EMUSO_ID}/usage").json()
check(uso["quick_replies"] == 1 and uso["auto_replies"] == 1 and uso["total"] == 2,
      f"endpoint de uso detalha a origem das referencias (got {uso})")

# arroba ESCAPADA e e-mail NAO contam como referencia
client.put(f"/api/quick-replies/{qr_ref['id']}", json={"content": "Escapado @@EMUSO e mail a@EMUSO.com"})
session = SessionLocal()
session.query(AutoReply).filter(AutoReply.trigger == "greeting").update(
    {"message": "Sem referencia real: @@EMUSO"})
session.commit()
session.close()
uso = client.get(f"/api/variables/{EMUSO_ID}/usage").json()
check(uso["total"] == 0, f"escape e e-mail NAO contam como referencia (got {uso})")
r = client.delete(f"/api/variables/{EMUSO_ID}")
check(r.status_code == 200, f"sem referencia real, exclui -> 200 (got {r.status_code})")

# limpeza das fixtures deste bloco
client.delete(f"/api/quick-replies/{qr_ref['id']}")
session = SessionLocal()
session.query(AutoReply).filter(AutoReply.trigger == "greeting").delete()
session.commit()
session.close()


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

# preview distingue os 3 codigos de problema exigidos
for texto, code in (("@VARIAVELINEXISTENTE", "unknown"),
                    ("@SEMVALOR", "empty_fixed"),
                    ("@EMAILCLIENTE", "empty_dynamic"),
                    ("@INATIVAAUTO", "inactive")):
    p = client.post("/api/variables/preview",
                    json={"text": f"Texto {texto} fim", "conversation_id": CONV_A}).json()
    check(p["problems"] and p["problems"][0]["code"] == code,
          f"preview classifica {texto} como '{code}' (got {[x['code'] for x in p['problems']]})")

# mensagem sem variavel: preview mostra o texto INALTERADO
p = client.post("/api/variables/preview",
                json={"text": "Mensagem simples, sem variavel.", "conversation_id": CONV_A}).json()
check(p["ok"] is True and p["rendered"] == "Mensagem simples, sem variavel.",
      f"preview de mensagem sem variavel mostra texto inalterado (got {p['rendered']!r})")

# preview aplica o escape igual ao envio
p = client.post("/api/variables/preview",
                json={"text": "Siga @@BNAOFICIAL", "conversation_id": CONV_A}).json()
check(p["ok"] is True and p["rendered"] == "Siga @BNAOFICIAL",
      f"preview aplica o escape @@ (got {p['rendered']!r})")

# o preview NAO envia nada
before = len(sent_payloads)
client.post("/api/variables/preview",
            json={"text": "Oi @PRIMEIRONOMECLIENTE", "conversation_id": CONV_A})
check(len(sent_payloads) == before, "preview NUNCA envia mensagem")


# ============ 10. FRASE AUTOMATICA — TUDO OU NADA ============
# CONV-VAR-01-HARD-01: o antigo modo tolerante mutilava o texto
# ("Somos a , prazer."). Agora: resolve tudo, ou a resposta inteira e pulada.
print("\nVAR — frase automatica (tudo ou nada)")

session = SessionLocal()
session.add(AutoReply(trigger="end_service", title="Encerramento",
                      message="Obrigado @PRIMEIRONOMECLIENTE! Volte sempre.",
                      is_active=True))
session.commit()
session.close()

db_tmp = SessionLocal()
conv = db_tmp.query(Conversation).filter(Conversation.id == CONV_A).first()


def _auto(texto, conversa=None, usuario=None, trigger="end_service"):
    return variables_service.render_auto_reply(
        db_tmp, texto,
        variables_service.VariableContext(db_tmp, conversation=conversa, user=usuario),
        trigger=trigger)


check(_auto("Obrigado @PRIMEIRONOMECLIENTE!", conv) == "Obrigado João!",
      "todas resolvem -> texto renderizado")
check(_auto("Somos a @SEMVALOR, prazer.", conv) is None,
      "variavel FIXA sem valor -> resposta inteira PULADA (nao mutila)")
check(_auto("Fale com @VARIAVELINEXISTENTE agora.", conv) is None,
      "variavel DESCONHECIDA -> resposta inteira PULADA")
check(_auto("Confirma o e-mail @EMAILCLIENTE?", conv) is None,
      "variavel DINAMICA sem valor -> resposta inteira PULADA")
check(_auto("Aviso da @INATIVAAUTO hoje.", conv) is None,
      "variavel INATIVA -> resposta inteira PULADA")
check(_auto("@SEMVALOR", conv) is None,
      "texto que ficaria VAZIO -> resposta PULADA (nunca string vazia)")
check(_auto("   ", conv) is None, "texto so com espacos -> PULADA")
check(_auto("Mensagem fixa sem variavel.", conv) == "Mensagem fixa sem variavel.",
      "mensagem sem variavel passa intacta")
# Nenhum resultado pode conter token literal nem sobra de mutilacao
for texto in ("Somos a @SEMVALOR, prazer.", "Fale com @VARIAVELINEXISTENTE agora."):
    out = _auto(texto, conv)
    check(out is None, f"nunca devolve texto parcial para {texto!r} (got {out!r})")
db_tmp.close()

# A falha de UMA resposta automatica nao pode derrubar o fluxo
before = len(sent_payloads)
session = SessionLocal()
session.query(AutoReply).filter(AutoReply.trigger == "end_service").update(
    {"message": "Obrigado @VARIAVELINEXISTENTE!"})
session.commit()
session.close()
r = client.put(f"/api/conversations/{CONV_B}", json={"status": "encerrada"})
check(r.status_code == 200, "encerrar conversa -> 200 mesmo com frase automatica invalida")
check(len(sent_payloads) == before, "frase automatica invalida NAO e enviada")
session = SessionLocal()
conv_b_status = session.query(Conversation).filter(Conversation.id == CONV_B).first().status
session.close()
check(conv_b_status == "encerrada", "o encerramento em si funcionou (falha isolada na resposta)")
client.put(f"/api/conversations/{CONV_B}", json={"status": "aberta"})

# Caminho feliz continua enviando
session = SessionLocal()
session.query(AutoReply).filter(AutoReply.trigger == "end_service").update(
    {"message": "Obrigado @PRIMEIRONOMECLIENTE! Volte sempre."})
session.commit()
session.close()
before = len(sent_payloads)
r = client.put(f"/api/conversations/{CONV_A}", json={"status": "encerrada"})
check(r.status_code == 200, "encerrar conversa -> 200")
check(len(sent_payloads) > before and sent_payloads[-1]["message"] == "Obrigado João! Volte sempre.",
      f"frase automatica valida enviada resolvida (got {sent_payloads[-1]['message']!r})")
client.put(f"/api/conversations/{CONV_A}", json={"status": "aberta"})

check(not hasattr(variables_service, "render_lenient"),
      "render_lenient removido (modo de mutilacao nao existe mais)")

# --- contrato do LOG estruturado (requisito 1) ---
import logging as _logging  # noqa: E402


class _CapturaLog(_logging.Handler):
    def __init__(self):
        super().__init__()
        self.linhas = []

    def emit(self, record):
        self.linhas.append(record.getMessage())


_captura = _CapturaLog()
_svc_logger = _logging.getLogger("app.services.variables")
_svc_logger.addHandler(_captura)
_svc_logger.setLevel(_logging.INFO)

db_log = SessionLocal()
conv_log = db_log.query(Conversation).filter(Conversation.id == CONV_A).first()
variables_service.render_auto_reply(
    db_log, "Somos a @SEMVALOR.",
    variables_service.VariableContext(db_log, conversation=conv_log, user=None),
    trigger="out_of_hours")
db_log.close()
_log = " | ".join(_captura.linhas)
check("trigger=out_of_hours" in _log, f"log estruturado traz o tipo da resposta (got {_log!r})")
check("token=@SEMVALOR" in _log, "log estruturado traz o token")
check("problema=empty_fixed" in _log, "log estruturado traz o codigo do problema")
check(f"conversation_id={CONV_A}" in _log, "log estruturado traz a conversa")
check("João" not in _log and "Brasileiros no Atacama" not in _log,
      f"log NAO contem valor resolvido (got {_log!r})")

# --- a resposta automatica NUNCA levanta (nao derruba o webhook) ---
_captura.linhas.clear()
_render_original = variables_service.render


def _render_explode(*a, **k):
    raise RuntimeError("falha simulada no resolver")


variables_service.render = _render_explode
db_log = SessionLocal()
try:
    resultado = variables_service.render_auto_reply(
        db_log, "Qualquer texto @PRIMEIRONOMECLIENTE",
        variables_service.VariableContext(db_log), trigger="greeting")
    check(resultado is None, "erro inesperado no resolver -> resposta pulada, sem excecao")
except Exception as exc:
    check(False, f"render_auto_reply levantou excecao: {type(exc).__name__}")
finally:
    variables_service.render = _render_original
    db_log.close()
check(any("motivo=erro_inesperado" in l for l in _captura.linhas),
      "erro inesperado e registrado como tal")
check(not any("RuntimeError: falha simulada" in l for l in _captura.linhas),
      "log NAO expoe o texto bruto da excecao")
_svc_logger.removeHandler(_captura)


# --- webhook: pular uma resposta NAO pode promover outra ---
print("\nVAR — webhook: resposta pulada nao promove outra")
from app.routers import webhook as webhook_router  # noqa: E402

session = SessionLocal()
session.query(AutoReply).delete()
session.add_all([
    AutoReply(trigger="out_of_hours", title="Fora do expediente",
              message="Estamos fechados. Fale com @NOMEATENDENTE.", is_active=True),
    AutoReply(trigger="greeting", title="Saudacao",
              message="Bem-vindo a @NOMEEMPRESA!", is_active=True),
])
session.commit()
session.close()

db_wh = SessionLocal()
conv_wh = db_wh.query(Conversation).filter(Conversation.id == CONV_A).first()
# @NOMEATENDENTE e insoluvel no webhook (user=None) -> out_of_hours e pulada
conf_ooh, txt_ooh = webhook_router._resolve_auto_reply("out_of_hours", db_wh, conv_wh)
check(conf_ooh is True and txt_ooh is None,
      f"trigger configurado mas insoluvel -> (True, None) (got {conf_ooh}, {txt_ooh!r})")
conf_nada, txt_nada = webhook_router._resolve_auto_reply("break_time", db_wh, conv_wh)
check(conf_nada is False and txt_nada is None,
      "trigger sem resposta cadastrada -> (False, None)")
conf_ok, txt_ok = webhook_router._resolve_auto_reply("greeting", db_wh, conv_wh)
check(conf_ok is True and txt_ok == "Bem-vindo a Brasileiros no Atacama!",
      f"trigger resolvido -> (True, texto) (got {txt_ok!r})")

# escape tambem vale em resposta automatica
session = SessionLocal()
session.query(AutoReply).filter(AutoReply.trigger == "greeting").update(
    {"message": "Bem-vindo! Siga @@BRASILEIROSNOATACAMA"})
session.commit()
session.close()
db_wh2 = SessionLocal()
conv_wh2 = db_wh2.query(Conversation).filter(Conversation.id == CONV_A).first()
_, txt_esc = webhook_router._resolve_auto_reply("greeting", db_wh2, conv_wh2)
check(txt_esc == "Bem-vindo! Siga @BRASILEIROSNOATACAMA",
      f"escape @@ funciona em resposta automatica (got {txt_esc!r})")
db_wh2.close()
db_wh.close()

# o dispatcher retorna quando o trigger esta configurado, mesmo pulado
import inspect as _inspect  # noqa: E402
_disp = _inspect.getsource(webhook_router._send_auto_reply_if_needed)
check("if configured:" in _disp,
      "dispatcher decide pelo trigger CONFIGURADO, nao pelo texto resolvido")
check(_disp.count("_resolve_auto_reply") == 3,
      "os 3 gatilhos usam o resolvedor com distincao configurada/pulada")

session = SessionLocal()
session.query(AutoReply).delete()
session.add(AutoReply(trigger="end_service", title="Encerramento",
                      message="Obrigado @PRIMEIRONOMECLIENTE! Volte sempre.", is_active=True))
session.commit()
session.close()


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

# Integridade: falha ao ler o CRM faz rollback (senao, no PostgreSQL, a
# transacao abortada derrubaria o commit de record_outbound_message DEPOIS
# do envio ja aceito pela Meta — mensagem entregue e nao persistida).
# CONV_CRM tem lead_id=42 e a tabela `leads` NAO existe aqui: a query
# levanta de verdade e o bloco de rollback e realmente executado.
db_probe = SessionLocal()
conv_probe = db_probe.query(Conversation).filter(Conversation.id == CONV_CRM).first()
ctx_probe = variables_service.VariableContext(db_probe, conversation=conv_probe, user=None)
check(ctx_probe.lead() is None, "leitura do CRM indisponivel retorna None (nao propaga erro)")
# A sessao precisa continuar utilizavel DEPOIS da falha — e o que o rollback garante.
check(db_probe.query(Conversation).count() >= 3,
      "sessao continua utilizavel apos a falha (rollback limpou a transacao)")
db_probe.close()

before = len(sent_payloads)
r = _send(CONV_CRM, "Confirma o e-mail @EMAILCLIENTE?")   # dispara a leitura do CRM
check(r.status_code == 422, "envio bloqueado quando o dado do CRM nao esta disponivel")
r = _send(CONV_CRM, "Oi @PRIMEIRONOMECLIENTE, seguimos.")  # mesma sessao de request
check(r.status_code == 200 and _last_sent() == "Oi Carlos, seguimos.",
      f"envio seguinte funciona apos falha de leitura do CRM (got {_last_sent()!r})")
check(len(sent_payloads) == before + 1, "apenas o envio valido chegou ao WhatsApp")

# sem autenticacao -> 401
main.app.dependency_overrides.pop(get_current_user)
anon = TestClient(main.app)
check(anon.get("/api/variables").status_code == 401, "listar sem autenticacao -> 401")
check(anon.post("/api/variables", json={"token": "@X1", "name": "x", "kind": "fixed"}).status_code == 401,
      "criar sem autenticacao -> 401")
main.app.dependency_overrides[get_current_user] = _current_user


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

# CONV-VAR-01-HARD-01: previa visivel no composer
check('id="btnPreview"' in html_conv, "botao de visualizar presente no composer")
check('id="previewModalOverlay"' in html_conv and 'class="modal-overlay"' in html_conv,
      "modal de previa reusa o componente .modal-overlay existente")
check(".preview-block" in css and ".preview-status" in css, "CSS da previa presente")

p_start = js_conv.find("CONV-VAR-01-HARD-01: previa da mensagem")
p_end = js_conv.find("─── Helpers", p_start)
check(p_start != -1 and p_end > p_start, "secao da previa presente no conversas.js")
preview_section = js_conv[p_start:p_end]
check("/api/variables/preview" in preview_section,
      "previa busca o texto renderizado no BACKEND")
check("innerHTML" not in preview_section, "NENHUM innerHTML na secao da previa")
check("createElement" in preview_section and ".textContent =" in preview_section,
      "previa renderizada com createElement/textContent")
check("sendMessage" not in preview_section and "msg_type" not in preview_section,
      "previa NUNCA envia a mensagem")
check("replaceChildren()" in preview_section, "limpeza da previa via replaceChildren")
# a previa nao pode reimplementar a resolucao no JS
for proibido in ("@PRIMEIRONOME", "replace(/@", "fixed_value", "source_key"):
    check(proibido not in preview_section,
          f"previa nao reimplementa resolucao no JS (sem {proibido!r})")
check("unknown:" in preview_section and "empty_fixed:" in preview_section
      and "inactive:" in preview_section and "empty_dynamic:" in preview_section,
      "previa distingue desconhecida / sem valor fixo / inativa / sem valor dinamico")

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
