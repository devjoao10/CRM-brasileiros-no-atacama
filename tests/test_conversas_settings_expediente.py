"""
CONV-EXPEDIENTE-01 — Configuracoes utilizaveis em producao (Frases Automaticas + Horario Comercial).

Contexto: em producao o seed de desenvolvimento nunca roda (ENVIRONMENT=production) e a tela de
Configuracoes so EDITAVA linhas que ja existiam — com `auto_replies`/`business_hours` vazias o dono
nao tinha como cadastrar a frase "Fora do expediente" nem o horario. Alem disso o toggle da tela
mostrava "Frase ativada" mesmo quando o PUT falhava.

Prova (HTTP real sobre SQLite, tabelas VAZIAS no inicio):
  1. GET /api/settings/auto-replies lista o catalogo inteiro (7 gatilhos), inativos e sem id;
  2. PUT cria a linha que faltar (texto padrao do catalogo) e o GET passa a devolve-la com id;
  3. gatilho fora do catalogo -> 404;
  4. ativar frase com mensagem vazia (greeting) -> 422 e NADA e criado;
  5. mensagem so com espacos numa frase ativa -> 422 e o texto anterior permanece;
  6. GET /api/settings/business-hours devolve sempre 7 dias (dia sem linha = aberto, sem horario);
  7. PUT business-hours valida HH:MM e fechamento > abertura (so em dia aberto);
  8. seed_auto_replies continua criando as mesmas 7 linhas (fonte unica AUTO_REPLY_PADROES);
  9. guards estaticos do settings.js/settings.html (toast de sucesso so com resp.ok);
 10. as rotas continuam exigindo login.

Roda standalone:  python tests/test_conversas_settings_expediente.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "test_conversas_settings_expediente.db"
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
from app import seed  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models.auto_reply import AUTO_REPLY_PADROES, AutoReply, BusinessHours  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


Base.metadata.create_all(bind=engine)


class _User:
    id = 1
    email = "tester@local"
    is_admin = True


main.app.dependency_overrides[get_current_user] = lambda: _User()
client = TestClient(main.app)

URL_FRASES = "/api/settings/auto-replies"
URL_HORARIOS = "/api/settings/business-hours"
MSG_ATIVAR_VAZIA = "Escreva a mensagem antes de ativar a frase automatica."
GATILHOS = ["greeting", "start_service", "waiting", "end_service", "out_of_hours", "break_time", "paused"]
# Padrao do SEED por gatilho: so greeting e break_time nascem desligados (e sem texto).
ATIVO_NO_SEED = {
    "greeting": False, "start_service": True, "waiting": True, "end_service": True,
    "out_of_hours": True, "break_time": False, "paused": True,
}


def _padrao(trigger):
    return next(p for p in AUTO_REPLY_PADROES if p["trigger"] == trigger)


def _item(itens, trigger):
    return next(i for i in itens if i["trigger"] == trigger)


def _linha(trigger):
    """Le a linha direto do banco (None se nao existe)."""
    db = SessionLocal()
    try:
        row = db.query(AutoReply).filter(AutoReply.trigger == trigger).first()
        if row is None:
            return None
        return {"id": row.id, "title": row.title, "message": row.message, "is_active": row.is_active}
    finally:
        db.close()


def _contagem(model):
    db = SessionLocal()
    try:
        return db.query(model).count()
    finally:
        db.close()


def _dia(weekday, is_open=True, open_time=None, close_time=None):
    return {"weekday": weekday, "is_open": is_open, "open_time": open_time, "close_time": close_time}


def _msg_validacao(resp):
    """Texto do primeiro erro de validacao do Pydantic (422 com lista em `detail`)."""
    detail = resp.json().get("detail")
    return detail[0].get("msg", "") if isinstance(detail, list) and detail else str(detail)


# ============ 0. CATALOGO — fonte unica dos textos padrao ============
print("SETTINGS 0 — catalogo AUTO_REPLY_PADROES")
check(_contagem(AutoReply) == 0 and _contagem(BusinessHours) == 0,
      "pre-condicao: auto_replies e business_hours VAZIAS (producao: seed nao roda)")
check(isinstance(AUTO_REPLY_PADROES, tuple), "catalogo e uma tupla (imutavel)")
check([p["trigger"] for p in AUTO_REPLY_PADROES] == GATILHOS, "catalogo tem os 7 gatilhos esperados, nesta ordem")
check(all(set(p) == {"trigger", "title", "message", "is_active"} for p in AUTO_REPLY_PADROES),
      "cada padrao tem exatamente trigger/title/message/is_active")
check({p["trigger"]: p["is_active"] for p in AUTO_REPLY_PADROES} == ATIVO_NO_SEED,
      "flags do seed preservadas (greeting e break_time desligados; os demais ligados)")
check(_padrao("greeting")["message"] == "" and _padrao("break_time")["message"] == "",
      "greeting e break_time nao tem texto padrao")
check(all(p["message"].strip() for p in AUTO_REPLY_PADROES if ATIVO_NO_SEED[p["trigger"]]),
      "todo padrao ligado no seed tem texto")

# ============ 1. GET /auto-replies com tabela vazia ============
print("\nSETTINGS 1 — GET /auto-replies lista o catalogo inteiro (tabela vazia)")
resp = client.get(URL_FRASES)
check(resp.status_code == 200, "GET auto-replies -> 200")
body = resp.json()
itens = body["auto_replies"]
check(len(itens) == 7 and body["total"] == 7, f"7 itens e total=7 (got {len(itens)} / {body.get('total')})")
check([i["trigger"] for i in itens] == GATILHOS, "gatilhos na ordem do catalogo")
check(all(i["id"] is None for i in itens), "gatilho sem linha vem com id nulo")
check(all(i["is_active"] is False for i in itens),
      "gatilho sem linha aparece INATIVO (mesmo os que o seed liga)")
check(all(i["created_at"] is None and i["updated_at"] is None for i in itens),
      "gatilho sem linha vem sem datas")
check(all(i["title"] == _padrao(i["trigger"])["title"] and i["message"] == _padrao(i["trigger"])["message"]
          for i in itens), "title e message vem do catalogo")
check(_contagem(AutoReply) == 0, "GET nao cria nada no banco")

# ============ 2. PUT cria a linha que faltar ============
print("\nSETTINGS 2 — PUT cria a linha do catalogo que ainda nao existe")
resp = client.put(f"{URL_FRASES}/out_of_hours", json={"is_active": True})
check(resp.status_code == 200, f"PUT out_of_hours {{is_active: true}} -> 200 (got {resp.status_code})")
criada = resp.json()
ooh = _padrao("out_of_hours")
check(criada.get("id") is not None, "linha criada tem id")
check(criada.get("is_active") is True, "linha criada esta ativa")
check(criada.get("message") == ooh["message"], "mensagem criada = texto padrao do catalogo")
check(criada.get("title") == ooh["title"], "titulo criado vem do catalogo")
linha = _linha("out_of_hours")
check(linha is not None and linha["is_active"] is True and linha["message"] == ooh["message"]
      and linha["title"] == ooh["title"], "linha persistida no banco (ativa, texto padrao, titulo do catalogo)")
ooh_id = criada.get("id")

resp = client.put(f"{URL_FRASES}/waiting", json={"message": "Texto proprio da fila"})
check(resp.status_code == 200, "PUT waiting {message} -> 200")
check(resp.json().get("is_active") is False, "criada sem is_active no payload -> INATIVA")
check(resp.json().get("message") == "Texto proprio da fila", "mensagem enviada prevalece sobre o texto padrao")
check(resp.json().get("title") == _padrao("waiting")["title"], "titulo continua vindo do catalogo")

resp = client.put(f"{URL_FRASES}/start_service", json={"message": "Bem-vindo!", "is_active": True})
check(resp.status_code == 200 and resp.json().get("is_active") is True
      and resp.json().get("message") == "Bem-vindo!", "PUT start_service {message, is_active} -> criada ativa com o texto")

itens = client.get(URL_FRASES).json()["auto_replies"]
check(len(itens) == 7, f"continua 7 itens (nenhum gatilho duplicado) (got {len(itens)})")
check([i["trigger"] for i in itens] == ["out_of_hours", "waiting", "start_service",
                                        "greeting", "end_service", "break_time", "paused"],
      "ordem: linhas do banco por id, depois os gatilhos do catalogo que faltam")
check(_item(itens, "out_of_hours")["id"] == ooh_id and _item(itens, "out_of_hours")["is_active"] is True,
      "GET devolve a out_of_hours criada, com id e ativa")
check(_item(itens, "out_of_hours")["created_at"] is not None, "GET devolve a data de criacao da linha real")
check(sum(1 for i in itens if i["id"] is None) == 4, "os 4 gatilhos restantes continuam sem linha")

# ============ 3. gatilho desconhecido ============
print("\nSETTINGS 3 — gatilho fora do catalogo")
resp = client.put(f"{URL_FRASES}/gatilho_inexistente", json={"is_active": True})
check(resp.status_code == 404, f"PUT gatilho desconhecido -> 404 (got {resp.status_code})")
check(_linha("gatilho_inexistente") is None and _contagem(AutoReply) == 3,
      "nenhuma linha criada para o gatilho desconhecido")

# ============ 4. ativar frase sem texto ============
print("\nSETTINGS 4 — ativar frase com mensagem vazia")
resp = client.put(f"{URL_FRASES}/greeting", json={"is_active": True})
check(resp.status_code == 422, f"PUT greeting {{is_active: true}} (texto padrao vazio) -> 422 (got {resp.status_code})")
check(resp.json().get("detail") == MSG_ATIVAR_VAZIA, "detail traz a mensagem pedida")
check(_linha("greeting") is None, "nenhuma linha criada para greeting")
resp = client.put(f"{URL_FRASES}/greeting", json={"message": "   ", "is_active": True})
check(resp.status_code == 422 and _linha("greeting") is None,
      "mensagem so com espacos + ativar -> 422 e nada criado")
itens = client.get(URL_FRASES).json()["auto_replies"]
check(_item(itens, "greeting")["id"] is None and _item(itens, "greeting")["is_active"] is False,
      "GET continua mostrando greeting sem linha e inativa")
resp = client.put(f"{URL_FRASES}/greeting", json={"message": "Ola! Seja bem-vindo.", "is_active": True})
linha = _linha("greeting")
check(resp.status_code == 200 and linha is not None and linha["is_active"] is True
      and linha["message"] == "Ola! Seja bem-vindo.",
      "texto + ativar no mesmo PUT -> 200 e criada ativa")

# ============ 5. texto vazio em frase ativa / rollback ============
print("\nSETTINGS 5 — mensagem vazia numa frase ativa e rollback")
resp = client.put(f"{URL_FRASES}/out_of_hours", json={"message": "   "})
check(resp.status_code == 422 and resp.json().get("detail") == MSG_ATIVAR_VAZIA,
      f"{{message: '   '}} numa frase ativa -> 422 (got {resp.status_code})")
check(_linha("out_of_hours")["message"] == ooh["message"] and _linha("out_of_hours")["is_active"] is True,
      "texto anterior permanece e a frase continua ativa")
resp = client.put(f"{URL_FRASES}/out_of_hours", json={"message": ""})
check(resp.status_code == 422 and _linha("out_of_hours")["message"] == ooh["message"],
      "{message: ''} numa frase ativa -> 422 e texto anterior permanece")

# frase INATIVA pode ficar com texto vazio, mas nao pode ser ativada assim
resp = client.put(f"{URL_FRASES}/waiting", json={"message": ""})
check(resp.status_code == 200 and _linha("waiting")["message"] == "" and _linha("waiting")["is_active"] is False,
      "frase inativa aceita texto vazio")
resp = client.put(f"{URL_FRASES}/waiting", json={"is_active": True})
check(resp.status_code == 422 and _linha("waiting")["is_active"] is False,
      "ativar frase existente sem texto -> 422 e o rollback mantem inativa")
resp = client.put(f"{URL_FRASES}/waiting", json={"message": "Voltamos ja", "is_active": True})
check(resp.status_code == 200 and _linha("waiting")["is_active"] is True
      and _linha("waiting")["message"] == "Voltamos ja", "texto + ativar na linha existente -> 200")

# edicao simples de linha existente (comportamento anterior preservado)
resp = client.put(f"{URL_FRASES}/out_of_hours", json={"message": "Estamos fora do horario, respondemos amanha."})
check(resp.status_code == 200 and resp.json().get("id") == ooh_id
      and _linha("out_of_hours")["message"] == "Estamos fora do horario, respondemos amanha.",
      "editar o texto de linha existente continua funcionando (mesmo id)")
check(_contagem(AutoReply) == 4, f"nenhuma linha duplicada ao editar (got {_contagem(AutoReply)})")

# ============ 6. GET /business-hours com tabela vazia ============
print("\nSETTINGS 6 — GET /business-hours devolve sempre 7 dias")
resp = client.get(URL_HORARIOS)
dias = resp.json().get("hours", [])
check(resp.status_code == 200 and len(dias) == 7, f"GET business-hours -> 200 com 7 dias (got {len(dias)})")
check([d["weekday"] for d in dias] == list(range(7)), "weekday 0..6 em ordem")
check(all(d["id"] is None and d["is_open"] is True and d["open_time"] is None and d["close_time"] is None
          for d in dias), "dia sem linha = aberto, sem horario, id nulo")
check(_contagem(BusinessHours) == 0, "GET nao cria nada no banco")

# ============ 7. PUT /business-hours valida ============
print("\nSETTINGS 7 — PUT /business-hours: formato HH:MM e fechamento > abertura")
for invalido in ("9:00", "24:00", "09:60", "0900", "09:00:00", "ab:cd"):
    r = client.put(URL_HORARIOS, json={"days": [_dia(0, True, invalido, "18:00")]})
    check(r.status_code == 422, f"open_time {invalido!r} -> 422 (got {r.status_code})")
r = client.put(URL_HORARIOS, json={"days": [_dia(0, True, "09:00", "9:00")]})
check(r.status_code == 422, "close_time '9:00' -> 422")

r = client.put(URL_HORARIOS, json={"days": [_dia(1, True, "18:00", "09:00")]})
check(r.status_code == 422, "dia aberto com fechamento ANTES da abertura -> 422")
check("Horario de fechamento deve ser depois da abertura" in _msg_validacao(r),
      "mensagem do 422 explica a regra do horario")
check("meia-noite" in _msg_validacao(r), "mensagem avisa que expediente pela meia-noite nao e suportado")
r = client.put(URL_HORARIOS, json={"days": [_dia(1, True, "09:00", "09:00")]})
check(r.status_code == 422, "dia aberto com fechamento IGUAL a abertura -> 422")

semana_com_erro = [_dia(d, True, "09:00", "18:00") for d in range(7)]
semana_com_erro[4] = _dia(4, True, "19:00", "13:00")
r = client.put(URL_HORARIOS, json={"days": semana_com_erro})
check(r.status_code == 422 and _contagem(BusinessHours) == 0,
      "um dia invalido derruba o PUT inteiro: nada e salvo")

# dia FECHADO com horarios nulos -> aceito; resposta traz os 7 dias
r = client.put(URL_HORARIOS, json={"days": [_dia(3, False)]})
check(r.status_code == 200, f"dia fechado com horarios nulos -> 200 (got {r.status_code})")
dias = r.json().get("hours", [])
check(len(dias) == 7 and [d["weekday"] for d in dias] == list(range(7)), "resposta do PUT traz sempre 7 dias")
check(dias[3]["id"] is not None and dias[3]["is_open"] is False, "dia salvo vem com id e fechado")
check(all(d["id"] is None and d["is_open"] is True for d in dias if d["weekday"] != 3),
      "dias sem linha completados como abertos, sem id")

# payload valido da semana inteira
semana = [
    _dia(0, True, "13:00", "19:00"),
    _dia(1, True, "00:00", "23:59"),            # limites do formato
    _dia(2, True),                              # aberto o dia todo (sem horario)
    _dia(3, False),                             # fechado, horarios nulos
    _dia(4, False, "20:00", "08:00"),           # fechado: a tela manda os valores dos campos desabilitados
    _dia(5, True, "09:30", "12:45"),
    _dia(6, True, "10:00", None),               # so um dos horarios: nao e validado
]
r = client.put(URL_HORARIOS, json={"days": semana})
check(r.status_code == 200, f"payload valido da semana -> 200 (got {r.status_code})")
check(len(r.json().get("hours", [])) == 7, "resposta do PUT tem 7 dias")
dias = client.get(URL_HORARIOS).json().get("hours", [])
check(len(dias) == 7 and all(d["id"] is not None for d in dias), "GET devolve os 7 dias, todos com id")
valores = [(d["weekday"], d["is_open"], d["open_time"], d["close_time"]) for d in dias]
check(valores == [(d["weekday"], d["is_open"], d["open_time"], d["close_time"]) for d in semana],
      "GET devolve exatamente os valores salvos")
check(_contagem(BusinessHours) == 7, f"7 linhas no banco, sem duplicar (got {_contagem(BusinessHours)})")

# atualizacao parcial: so a segunda muda; a resposta continua com a semana inteira
r = client.put(URL_HORARIOS, json={"days": [_dia(0, True, "14:00", "18:00")]})
dias = r.json().get("hours", [])
check(r.status_code == 200 and len(dias) == 7 and dias[0]["open_time"] == "14:00"
      and dias[0]["close_time"] == "18:00", "atualizacao de um dia so -> 200 e resposta com 7 dias")
check(dias[5]["open_time"] == "09:30" and dias[5]["close_time"] == "12:45",
      "os demais dias permanecem como estavam")

# ============ 8. seed continua criando as mesmas 7 linhas ============
print("\nSETTINGS 8 — seed_auto_replies usa o catalogo (resultado identico)")
db = SessionLocal()
db.query(AutoReply).delete()
db.commit()
db.close()
check(_contagem(AutoReply) == 0, "banco de frases vazio antes do seed")
seed.seed_auto_replies()
db = SessionLocal()
linhas = db.query(AutoReply).order_by(AutoReply.id).all()
check(len(linhas) == 7, f"seed num banco vazio cria 7 linhas (got {len(linhas)})")
for linha, padrao in zip(linhas, AUTO_REPLY_PADROES):
    check((linha.trigger, linha.title, linha.message, linha.is_active)
          == (padrao["trigger"], padrao["title"], padrao["message"], padrao["is_active"]),
          f"seed '{padrao['trigger']}': gatilho, titulo, texto e flag iguais ao catalogo")
check([linha.trigger for linha in linhas] == GATILHOS, "seed grava na ordem do catalogo")
db.close()
seed.seed_auto_replies()
check(_contagem(AutoReply) == 7, "seed e idempotente (2a execucao nao duplica)")

itens = client.get(URL_FRASES).json()["auto_replies"]
check([i["trigger"] for i in itens] == GATILHOS and all(i["id"] is not None for i in itens),
      "com o seed, o GET devolve as 7 linhas reais (com id), na ordem do catalogo")
check({i["trigger"]: i["is_active"] for i in itens} == ATIVO_NO_SEED,
      "linha real manda: flags do seed aparecem (nao o INATIVO dos gatilhos sem linha)")

# ============ 9. FRONTEND — guards estaticos ============
print("\nSETTINGS 9 — settings.js / settings.html (guards estaticos)")
js = (CONVERSAS_DIR / "static" / "js" / "settings.js").read_text(encoding="utf-8")
html = (CONVERSAS_DIR / "templates" / "settings.html").read_text(encoding="utf-8")

check(js.count("async function showBackendError(") == 1, "helper unico de erro do backend definido uma vez")
t_ini = js.find("window._toggleAutoReply = async function")
t_fim = js.find("window._saveAutoReply = async function", t_ini)
toggle = js[t_ini:t_fim]
check(t_ini != -1 and t_fim > t_ini, "toggle da frase localizado no settings.js")
check("resp.ok" in toggle and 0 <= toggle.find("resp.ok") < toggle.find("showToast(isActive"),
      "toggle: toast de sucesso SO depois de checar resp.ok")
check("showBackendError(" in toggle, "toggle: no erro mostra a mensagem do backend")
check("loadAutoReplies()" in toggle, "toggle: no erro recarrega a lista (volta ao valor real)")

s_ini = js.find("window._saveAutoReply = async function")
s_fim = js.find("// ─── Business Hours", s_ini)
check(s_ini != -1 and s_fim > s_ini and "showBackendError(" in js[s_ini:s_fim],
      "salvar frase: no erro mostra a mensagem do backend")
b_ini = js.find("async function saveBusinessHours")
b_fim = js.find("// ─── Quick Replies", b_ini)
check(b_ini != -1 and b_fim > b_ini and "showBackendError(" in js[b_ini:b_fim],
      "salvar horarios: no erro mostra a mensagem do backend")

check("horario de Brasilia" in html, "card de horario informa o fuso (Brasilia)")
check("no maximo uma vez a cada 12 horas por conversa" in html,
      "card de horario informa o anti-spam de 12h")
check("conversas em que a Bia esta desativada" in html,
      "card de horario informa que o aviso so sai quando a Bia esta desativada")

# ============ 10. AUTH inalterada ============
print("\nSETTINGS 10 — rotas continuam exigindo login")
main.app.dependency_overrides.pop(get_current_user)
anon = TestClient(main.app)
antes = _contagem(AutoReply)
check(anon.get(URL_FRASES).status_code == 401, "GET auto-replies sem login -> 401")
check(anon.put(f"{URL_FRASES}/out_of_hours", json={"is_active": False}).status_code == 401,
      "PUT auto-replies sem login -> 401")
check(anon.get(URL_HORARIOS).status_code == 401, "GET business-hours sem login -> 401")
check(anon.put(URL_HORARIOS, json={"days": [_dia(0, True, "09:00", "18:00")]}).status_code == 401,
      "PUT business-hours sem login -> 401")
check(_contagem(AutoReply) == antes, "tentativa sem login nao altera o banco")
main.app.dependency_overrides[get_current_user] = lambda: _User()

# --- Resultado ---
main.app.dependency_overrides.clear()
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE SETTINGS/EXPEDIENTE PASSARAM")
