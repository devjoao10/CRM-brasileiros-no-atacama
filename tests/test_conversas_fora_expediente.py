"""
CONV-EXPEDIENTE-01 — aviso "Fora do expediente" no webhook do Conversas.

Fixa as decisoes D6, D7 e D8 da rodada CONV-BIA-FILA-01 / CONV-EXPEDIENTE-01:

  D6  o aviso `out_of_hours` so sai quando a mensagem NAO vai para a Bia (Bia
      desligada na conversa OU agente global desligado). Quando a Bia vai
      responder, nada sai — e `greeting`/`waiting` seguem suprimidos fora do
      horario (a prioridade do `out_of_hours` configurado e preservada);
  D7  anti-spam: no maximo 1 aviso por conversa a cada 12h. So conta envio com o
      MESMO texto e status diferente de 'failed' ('delivered' e 'read' contam);
  D8  `@NOMEATENDENTE` / `@EMAILATENDENTE` resolvem para o atendente da conversa;
      sem atendente a frase continua insoluvel e e PULADA, sem promover outra.

Fecho da rodada final: o cooldown so conta o MESMO texto (mensagem da vendedora, com outro texto,
nao bloqueia o aviso) e uma corrida de inbounds da mesma conversa (o aviso so e gravado DEPOIS do
await da Meta) gera um unico envio — a guarda em memoria e liberada mesmo quando o envio falha.

Continuacao da rodada final:
  * a guarda tambem e liberada quando o banco cai logo depois de gravar o aviso (o commit expira a
    conversa; o `finally` nao pode depender de reler `conversation.id`);
  * o skip por "envio em voo" tem texto proprio no log, e a ORDEM das decisoes e fixa:
    Bia responde > envio em voo > cooldown > envia;
  * a fronteira das 12h e exata (12h = pode avisar; 12h menos 1 microssegundo = bloqueia) tanto na
    leitura naive do SQLite quanto na tz-aware do PostgreSQL (relogio injetado, sem mexer no relogio);
  * tabela `business_hours` VAZIA (estado inicial de producao) = aberto o dia todo = nenhum aviso;
  * toda chamada do webhook confere que NENHUM log ERROR saiu: o webhook transforma um crash em HTTP 200
    + log ERROR, e as asserts negativas ("nada enviado") passariam de graca num crash.

Webhook real (POST /webhook), Meta e n8n neutralizados, SEM patch de relogio:
"aberto/fechado agora" vem das 7 linhas de BusinessHours — todas fechadas = fora
do horario; todas 00:00-23:59 = dentro. Escrever os 7 dias (e nao so o de hoje)
elimina a corrida da virada de dia. O cooldown e provado recuando o `created_at`
do aviso anterior no banco.

Roda standalone:  python tests/test_conversas_fora_expediente.py
"""
import asyncio
import logging
import os
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone as tz
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_fora_expediente_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""       # sem HMAC em development
os.environ["META_ACCESS_TOKEN"] = ""
os.environ["META_PHONE_NUMBER_ID"] = ""
os.environ["N8N_AGENT_ENABLED"] = "true"

sys.path.insert(0, str(CONVERSAS_DIR))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

import app.main as main  # noqa: E402
import app.routers.webhook as wh  # noqa: E402
from app.auth import User  # noqa: E402
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.models.auto_reply import AutoReply, BusinessHours  # noqa: E402
from app.models.conversation import Conversation, Message  # noqa: E402
from app.models.message_variable import MessageVariable  # noqa: E402

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


# ─── Neutraliza TODA a rede ──────────────────────────────────────────────
sent = []                      # textos que "foram" para a Meta
meta = {"fail": False}         # True -> a Meta recusa o envio
_wamid = {"n": 0}


async def _noop(*a, **k):
    return None


async def _noop_false(*a, **k):
    return False


async def _fake_send(to, message, db=None):
    sent.append(message)
    if meta["fail"]:
        return {"error": True, "status_code": 400, "summary": "HTTP 400: erro simulado"}
    _wamid["n"] += 1
    return {"messages": [{"id": f"wamid.OOH{_wamid['n']}"}]}


_orig_send_text = wh.whatsapp.send_text_message
_orig_schedule = wh._schedule_agent_debounce
_orig_agent_enabled = wh.N8N_AGENT_ENABLED

wh.whatsapp.mark_as_read = _noop
wh.whatsapp.send_text_message = _fake_send
wh.crm_service.auto_link_conversation = _noop_false

# O debounce real criaria tasks de 15s dentro do loop do TestClient. So
# interessa SABER se a Bia foi agendada (== a mensagem vai para a Bia).
scheduled = []
wh._schedule_agent_debounce = lambda conversation_id: scheduled.append(conversation_id)

Base.metadata.create_all(bind=engine)
client = TestClient(main.app)


# ─── Helpers ──────────────────────────────────────────────────────────────
AVISO = "Estamos fora do expediente agora. Respondemos assim que abrirmos."
_seq = {"n": 0}


def _horario(aberto: bool):
    """7 linhas (uma por dia): todas fechadas = fora do horario; 00:00-23:59 = dentro."""
    s = SessionLocal()
    try:
        s.query(BusinessHours).delete()
        for dia in range(7):
            s.add(BusinessHours(
                weekday=dia, is_open=aberto,
                open_time="00:00" if aberto else None,
                close_time="23:59" if aberto else None,
            ))
        s.commit()
    finally:
        s.close()


def _frases(**textos):
    """Recria as respostas automaticas: so os gatilhos informados, todos ativos."""
    s = SessionLocal()
    try:
        s.query(AutoReply).delete()
        for trigger, texto in textos.items():
            s.add(AutoReply(trigger=trigger, title=trigger, message=texto, is_active=True))
        s.commit()
    finally:
        s.close()


def _agente(ligado: bool):
    wh.N8N_AGENT_ENABLED = ligado


def _conversa(numero, *, bia, atendente_id=None):
    s = SessionLocal()
    try:
        c = Conversation(
            lead_id=0, whatsapp=numero, nome="Cliente Expediente", status="aberta",
            is_bot_active=bia, atendente_id=atendente_id,
            last_customer_msg_at=datetime.now(tz.utc),
        )
        s.add(c)
        s.commit()
        return c.id
    finally:
        s.close()


def _cid(numero):
    s = SessionLocal()
    try:
        return s.query(Conversation).filter(Conversation.whatsapp == numero).first().id
    finally:
        s.close()


class _CapturaErros(logging.Handler):
    """Guarda toda mensagem de nivel ERROR (ou acima) de QUALQUER logger: o handler vai no logger raiz."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.mensagens = []

    def emit(self, record):
        self.mensagens.append(f"{record.name}: {record.getMessage()}")


def _inbound(numero, texto="oi, tudo bem?"):
    """Uma mensagem do cliente pelo webhook real. Limpa o que foi capturado antes.

    O webhook transforma um crash em HTTP 200 + log ERROR (`receive_webhook`), e uma assert negativa
    ("nada foi enviado") passaria de graca nesse crash. Por isso TODA chamada confere que nenhum ERROR
    foi logado: cada caso "sem aviso" tambem prova que nada quebrou no caminho.
    """
    _seq["n"] += 1
    sent.clear()
    scheduled.clear()
    erros = _CapturaErros()
    logging.getLogger().addHandler(erros)
    try:
        r = client.post("/webhook", json={"entry": [{"changes": [{"value": {
            "contacts": [{"profile": {"name": "Cliente Expediente"}}],
            "messages": [{
                "from": numero, "id": f"wamid.INOOH{_seq['n']}", "type": "text",
                "timestamp": str(int(datetime.now(tz.utc).timestamp())),
                "text": {"body": texto},
            }],
        }}]}]})
    finally:
        logging.getLogger().removeHandler(erros)
    check(not erros.mensagens,
          f"webhook de ...{numero[-4:]} sem nenhum log ERROR (got {erros.mensagens!r})")
    return r


def _outbound(cid):
    """[(texto, status)] das mensagens enviadas pela casa, na ordem de criacao."""
    s = SessionLocal()
    try:
        rows = s.query(Message).filter(
            Message.conversation_id == cid, Message.direction == "outbound",
        ).order_by(Message.id).all()
        return [(m.content, m.status) for m in rows]
    finally:
        s.close()


def _retroceder_avisos(cid, horas, minutos=0):
    """Recua o `created_at` do(s) aviso(s) ja enviado(s) — prova o cooldown sem mexer no relogio."""
    s = SessionLocal()
    try:
        s.query(Message).filter(
            Message.conversation_id == cid,
            Message.direction == "outbound",
            Message.content == AVISO,
        ).update(
            {Message.created_at: datetime.now(tz.utc) - timedelta(hours=horas, minutes=minutos)},
            synchronize_session=False,
        )
        s.commit()
    finally:
        s.close()


def _marcar_status(cid, status):
    s = SessionLocal()
    try:
        s.query(Message).filter(
            Message.conversation_id == cid, Message.direction == "outbound",
        ).update({Message.status: status}, synchronize_session=False)
        s.commit()
    finally:
        s.close()


class _CapturaLog(logging.Handler):
    """Guarda (nivel, mensagem JA FORMATADA) do logger do router do webhook."""

    def __init__(self):
        super().__init__()
        self.registros = []

    def emit(self, record):
        self.registros.append((record.levelname, record.getMessage()))


def _inbound_com_log(numero, texto="oi, tudo bem?"):
    handler = _CapturaLog()
    nivel = wh.logger.level
    wh.logger.addHandler(handler)
    wh.logger.setLevel(logging.INFO)
    try:
        _inbound(numero, texto)
    finally:
        wh.logger.removeHandler(handler)
        wh.logger.setLevel(nivel)
    return handler.registros


# ─── Fixtures de dados ──────────────────────────────────────────────────
s = SessionLocal()
julia = User(nome="Julia Atendente", email="julia.ooh@example.com",
             hashed_password="placeholder-sem-login", role="USER")
s.add(julia)
s.add_all([
    MessageVariable(name="Nome do Atendente", token="@NOMEATENDENTE",
                    kind="dynamic", source_key="atendente.nome", is_active=True),
    MessageVariable(name="Email do Atendente", token="@EMAILATENDENTE",
                    kind="dynamic", source_key="atendente.email", is_active=True),
])
s.commit()
JULIA_ID = julia.id
s.close()


# =====================================================================
# D7/1 — Bia desligada + atendente + fora do horario -> UM aviso
# =====================================================================
print("D7 — Bia desligada + atendente + fora do horario: exatamente 1 aviso enviado e persistido")

_horario(False)
_frases(out_of_hours=AVISO)
_agente(True)

N1 = "5511911000001"
cid1 = _conversa(N1, bia=False, atendente_id=JULIA_ID)
r = _inbound(N1)
check(r.status_code == 200, f"webhook responde 200 (got {r.status_code})")
check(sent == [AVISO], f"exatamente 1 aviso foi para a Meta (got {sent})")
check(_outbound(cid1) == [(AVISO, "sent")],
      f"o aviso foi persistido como outbound 'sent' (got {_outbound(cid1)})")
check(scheduled == [], "Bia desligada: nada foi agendado para o agente")


# =====================================================================
# D7/2 — 2a mensagem dentro de 12h -> nenhum aviso novo
# =====================================================================
print("\nD7 — 2a mensagem do cliente dentro de 12h: nenhum aviso novo")

_inbound(N1, "alguem ai?")
check(sent == [], f"nenhum aviso novo na 2a mensagem (got {sent})")
check(_outbound(cid1) == [(AVISO, "sent")],
      f"continua 1 unico aviso persistido (got {_outbound(cid1)})")

# Fronteira: 11h55 ainda esta DENTRO da janela de 12h (pega cooldown curto demais).
_retroceder_avisos(cid1, 11, 55)
_inbound(N1, "e agora?")
check(sent == [], f"aviso de 11h55 atras ainda bloqueia (got {sent})")


# =====================================================================
# D7/3 — aviso anterior ha mais de 12h -> novo aviso
# =====================================================================
print("\nD7 — aviso anterior ha 12h05: novo aviso, e a trava volta depois dele")

_retroceder_avisos(cid1, 12, 5)
_inbound(N1, "bom dia?")
check(sent == [AVISO], f"passadas as 12h, um novo aviso sai (got {sent})")
check(_outbound(cid1) == [(AVISO, "sent"), (AVISO, "sent")],
      f"agora ha 2 avisos persistidos (got {_outbound(cid1)})")
_inbound(N1, "obrigado")
check(sent == [], f"o aviso novo reinicia o cooldown (got {sent})")


# =====================================================================
# D7/4 — 'failed' nao conta; 'delivered'/'read' contam
# =====================================================================
print("\nD7 — aviso 'failed' e tentado de novo; 'delivered' e 'read' contam como enviado")

N4 = "5511911000004"
cid4 = _conversa(N4, bia=False, atendente_id=JULIA_ID)
meta["fail"] = True
_inbound(N4, "oi?")                # o aviso e tentado e a Meta recusa
check(sent == [AVISO], f"o aviso foi TENTADO (got {sent})")
check(_outbound(cid4) == [(AVISO, "failed")],
      f"a tentativa recusada ficou persistida como 'failed' (got {_outbound(cid4)})")

meta["fail"] = False
_inbound(N4, "alguem?")
check(sent == [AVISO], f"aviso 'failed' nao conta: a proxima mensagem envia de novo (got {sent})")
check(_outbound(cid4) == [(AVISO, "failed"), (AVISO, "sent")],
      f"agora ha o 'failed' e o 'sent' (got {_outbound(cid4)})")

_inbound(N4, "e ai?")
check(sent == [], f"com um 'sent' recente, o seguinte e barrado (got {sent})")

# NOT_FAILED_STATUSES nao inclui delivered/read — mas um aviso entregue ou lido TAMBEM foi enviado.
for status in ("delivered", "read"):
    _marcar_status(cid4, status)
    _inbound(N4, f"mais uma ({status})")
    check(sent == [], f"aviso com status {status!r} tambem conta como enviado (got {sent})")


# =====================================================================
# D6/5 — Bia vai responder: nenhum aviso, nenhum greeting/waiting
# =====================================================================
print("\nD6 — Bia ligada + agente global ligado + fora do horario: so a Bia fala")

_frases(out_of_hours=AVISO, greeting="Bem-vindo! Recebemos sua mensagem.",
        waiting="Ja vamos te atender, aguarde um instante.")
_agente(True)

# (a) conversa NOVA: o greeting seria candidato.
N5A = "5511911000005"
_inbound(N5A)
cid5a = _cid(N5A)
check(sent == [], f"conversa nova: nenhum aviso/greeting/waiting (got {sent})")
check(_outbound(cid5a) == [], f"conversa nova: nada persistido (got {_outbound(cid5a)})")
check(scheduled == [cid5a], f"conversa nova: o debounce da Bia foi agendado (got {scheduled})")

# (b) conversa existente sem atendente: o waiting seria candidato.
N5B = "5511911000006"
cid5b = _conversa(N5B, bia=True, atendente_id=None)
_inbound(N5B)
check(sent == [], f"conversa existente sem atendente: nenhum aviso/waiting (got {sent})")
check(_outbound(cid5b) == [], f"conversa existente: nada persistido (got {_outbound(cid5b)})")
check(scheduled == [cid5b], f"conversa existente: o debounce da Bia foi agendado (got {scheduled})")


# =====================================================================
# D6/6 — agente global desligado: a Bia nao vai responder -> aviso sai
# =====================================================================
print("\nD6 — N8N_AGENT_ENABLED desligado com is_bot_active=True: o aviso sai")

_frases(out_of_hours=AVISO)
_agente(False)
N6 = "5511911000007"
cid6 = _conversa(N6, bia=True, atendente_id=None)
_inbound(N6)
check(sent == [AVISO], f"sem agente global, o aviso e enviado (got {sent})")
check(_outbound(cid6) == [(AVISO, "sent")], f"e persistido (got {_outbound(cid6)})")
check(scheduled == [], f"sem agente global nada e agendado (got {scheduled})")
_agente(True)


# =====================================================================
# Dentro do horario: sem aviso
# =====================================================================
print("\nDentro do horario, Bia desligada e com atendente: nenhum aviso")

_horario(True)
_frases(out_of_hours=AVISO, greeting="Bem-vindo!", waiting="Aguarde um instante.")
N7 = "5511911000008"
cid7 = _conversa(N7, bia=False, atendente_id=JULIA_ID)
_inbound(N7)
check(sent == [], f"dentro do horario nada e enviado (got {sent})")
check(_outbound(cid7) == [], f"e nada e persistido (got {_outbound(cid7)})")


# =====================================================================
# D8 — @NOMEATENDENTE / @EMAILATENDENTE
# =====================================================================
print("\nD8 — variaveis do atendente na frase de fora do expediente")

_horario(False)
TEXTO_VAR = "Fora do expediente. Quem te atende: @NOMEATENDENTE, e-mail @EMAILATENDENTE."
_frases(out_of_hours=TEXTO_VAR, greeting="Bem-vindo!", waiting="Aguarde um instante.")
_agente(True)

N8A = "5511911000009"
cid8a = _conversa(N8A, bia=False, atendente_id=JULIA_ID)
_inbound(N8A)
esperado = "Fora do expediente. Quem te atende: Julia Atendente, e-mail julia.ooh@example.com."
check(sent == [esperado], f"com atendente, as variaveis resolvem para ele (got {sent})")
check(_outbound(cid8a) == [(esperado, "sent")], f"e o texto resolvido e o persistido (got {_outbound(cid8a)})")

N8B = "5511911000010"
cid8b = _conversa(N8B, bia=False, atendente_id=None)
_inbound(N8B)
check(sent == [], f"sem atendente a frase e PULADA e NADA e promovido no lugar (got {sent})")
check(_outbound(cid8b) == [], f"nada persistido (got {_outbound(cid8b)})")

# O atendente entra so com nome e email: a linha inteira de `users` traz o hash da senha e a
# API key, que nao tem por que passar pela memoria do webhook so para preencher uma variavel.
consultas = []


def _registra_sql(conn, cursor, statement, parameters, context, executemany):
    consultas.append(statement)


db_var = SessionLocal()
conv_var = db_var.query(Conversation).filter(Conversation.id == cid8a).first()
event.listen(engine, "before_cursor_execute", _registra_sql)
try:
    configurada, texto_var = wh._resolve_auto_reply("out_of_hours", db_var, conv_var)
finally:
    event.remove(engine, "before_cursor_execute", _registra_sql)
    db_var.close()
check(configurada is True and texto_var == esperado, f"o texto resolve igual com a consulta enxuta (got {texto_var!r})")
consultas_users = [q for q in consultas if "FROM users" in q]
check(len(consultas_users) == 1, f"o atendente e lido com UMA consulta a users (got {len(consultas_users)})")
check(all("hashed_password" not in q and "api_key" not in q for q in consultas_users),
      "a consulta nao traz hashed_password nem api_key")
check(all("users.nome" in q and "users.email" in q for q in consultas_users),
      "a consulta traz nome e email, que e o que as variaveis usam")


# =====================================================================
# Prioridade preservada: aviso barrado pelo cooldown NAO promove waiting/greeting
# =====================================================================
print("\nPrioridade: fora do horario com out_of_hours configurado, nada de waiting/greeting")

_frases(out_of_hours=AVISO, greeting="Bem-vindo!", waiting="Aguarde um instante.")
N9 = "5511911000011"
cid9 = _conversa(N9, bia=False, atendente_id=None)
_inbound(N9)
check(sent == [AVISO], f"1a mensagem: so o aviso, sem waiting (got {sent})")
_inbound(N9, "oi de novo")
check(sent == [], f"2a mensagem (aviso barrado pelo cooldown): o waiting NAO assume o lugar (got {sent})")
check(_outbound(cid9) == [(AVISO, "sent")], f"so o aviso existe no historico (got {_outbound(cid9)})")


# =====================================================================
# Sem out_of_hours configurado: greeting/waiting seguem como antes
# =====================================================================
print("\nSem out_of_hours configurado: o fluxo greeting/waiting segue inalterado")

_frases(greeting="Bem-vindo! Recebemos sua mensagem.", waiting="Aguarde um instante.")
N10 = "5511911000012"
_inbound(N10)       # conversa nova; Bia ligada + agente ligado; fora do horario
check(sent == ["Bem-vindo! Recebemos sua mensagem."],
      f"sem out_of_hours, a conversa nova ainda recebe o greeting (got {sent})")


# =====================================================================
# Logs de skip: identificam a conversa pelo id, nunca pelo telefone
# =====================================================================
print("\nLogs: o skip identifica a conversa pelo id e nao carrega o telefone")

_frases(out_of_hours=AVISO)

# (a) skip porque a Bia vai responder
N11A = "5511911000013"
cid11a = _conversa(N11A, bia=True, atendente_id=None)
regs = _inbound_com_log(N11A)
ooh = [m for nivel, m in regs if nivel == "INFO" and "fora do expediente" in m.lower()]
check(bool(ooh), f"skip por 'Bia vai responder' gera log INFO (got {regs!r})")
check(all(re.search(rf"\b{cid11a}\b", m) for m in ooh),
      f"o log identifica a conversa pelo id {cid11a} (got {ooh!r})")
check(all(N11A not in m for m in ooh), f"o log nao carrega o telefone (got {ooh!r})")

# (b) skip porque o aviso ja foi enviado nas ultimas 12h
N11B = "5511911000014"
cid11b = _conversa(N11B, bia=False, atendente_id=JULIA_ID)
_agente(False)
_inbound(N11B)                                  # 1o aviso sai
regs = _inbound_com_log(N11B, "ei")             # 2o e barrado
ooh = [m for nivel, m in regs if nivel == "INFO" and "fora do expediente" in m.lower()]
check(bool(ooh), f"skip por cooldown gera log INFO (got {regs!r})")
check(all(re.search(rf"\b{cid11b}\b", m) for m in ooh),
      f"o log identifica a conversa pelo id {cid11b} (got {ooh!r})")
check(all(N11B not in m for m in ooh), f"o log nao carrega o telefone (got {ooh!r})")
_agente(True)


# =====================================================================
# D7 — a clausula "MESMO texto": a mensagem da vendedora nao e aviso
# =====================================================================
print("\nD7 — mensagem recente da vendedora (outro texto) NAO conta como aviso")

_horario(False)
_frases(out_of_hours=AVISO)
_agente(True)
N12 = "5511911000015"
MSG_VENDEDORA = "Oi, aqui e a Julia"
cid12 = _conversa(N12, bia=False, atendente_id=JULIA_ID)
s = SessionLocal()
s.add(Message(conversation_id=cid12, direction="outbound", content=MSG_VENDEDORA, msg_type="text",
              status="sent", created_at=datetime.now(tz.utc) - timedelta(hours=1)))
s.commit()
s.close()
_inbound(N12, "ola, voces atendem hoje?")
check(sent == [AVISO],
      f"Bia desligada + atendente + fora do horario, com uma mensagem da vendedora de 1h atras: o aviso E enviado (got {sent})")
check(_outbound(cid12) == [(MSG_VENDEDORA, "sent"), (AVISO, "sent")],
      f"a mensagem da vendedora e o aviso coexistem no historico (got {_outbound(cid12)})")
_inbound(N12, "alguem?")
check(sent == [], f"o aviso, esse sim, passa a bloquear o seguinte (got {sent})")


# =====================================================================
# D7 — corrida: o aviso so e GRAVADO depois do await da Meta
# =====================================================================
print("\nD7 — 3 inbounds concorrentes da mesma conversa (Meta lenta): exatamente 1 aviso")


async def _envio_lento(to, message, db=None):
    # A Meta demora: o aviso so e persistido DEPOIS deste await — e a janela da corrida.
    sent.append(message)
    await asyncio.sleep(0.05)
    _wamid["n"] += 1
    return {"messages": [{"id": f"wamid.OOH{_wamid['n']}"}]}


async def _envio_explode(to, message, db=None):
    raise RuntimeError("falha simulada de rede")


async def _chamadas_juntas(cid, n, *, bia_vai_responder=False):
    """`n` execucoes de `_send_auto_reply_if_needed` da MESMA conversa, cada uma com a sua sessao."""
    sessoes = [SessionLocal() for _ in range(n)]
    try:
        conversas = [sessao.query(Conversation).filter(Conversation.id == cid).first() for sessao in sessoes]
        await asyncio.gather(*(
            wh._send_auto_reply_if_needed(conversa, False, sessao, bia_vai_responder=bia_vai_responder)
            for conversa, sessao in zip(conversas, sessoes)
        ))
    finally:
        for sessao in sessoes:
            sessao.close()


_horario(False)
_frases(out_of_hours=AVISO)
N13 = "5511911000016"
cid13 = _conversa(N13, bia=False, atendente_id=JULIA_ID)
sent.clear()
captura = _CapturaLog()
nivel_anterior = wh.logger.level
wh.logger.addHandler(captura)
wh.logger.setLevel(logging.INFO)
wh.whatsapp.send_text_message = _envio_lento
try:
    asyncio.run(_chamadas_juntas(cid13, 3))
finally:
    wh.whatsapp.send_text_message = _fake_send
    wh.logger.removeHandler(captura)
    wh.logger.setLevel(nivel_anterior)
check(sent == [AVISO], f"3 chamadas concorrentes enviam UM so aviso (got {len(sent)}: {sent})")
check(_outbound(cid13) == [(AVISO, "sent")], f"e UM so outbound e gravado (got {_outbound(cid13)})")
check(wh._avisos_fora_do_expediente_em_voo == set(), "a guarda em memoria e liberada ao final")
pulos = [m for nivel, m in captura.registros if nivel == "INFO" and "nao enviado" in m]
check(len(pulos) == 2 and all(re.search(rf"\b{cid13}\b", m) for m in pulos) and all(N13 not in m for m in pulos),
      f"as 2 que cederam logam o skip em INFO com o id da conversa e sem o telefone (got {pulos!r})")
check(all("esta em envio" in m for m in pulos) and not any("ja enviado" in m for m in pulos),
      f"o skip por envio em voo tem texto proprio, nao o do cooldown (got {pulos!r})")

# Um envio que EXPLODE tambem libera a guarda: se ela vazasse, a conversa ficaria sem aviso ate
# o processo reiniciar. O erro continua subindo (o `finally` so garante a limpeza).
N14 = "5511911000017"
cid14 = _conversa(N14, bia=False, atendente_id=JULIA_ID)
wh.whatsapp.send_text_message = _envio_explode
explodiu = False
try:
    asyncio.run(_chamadas_juntas(cid14, 1))
except RuntimeError:
    explodiu = True
finally:
    wh.whatsapp.send_text_message = _fake_send
check(explodiu, "o erro do envio continua subindo para o chamador")
check(wh._avisos_fora_do_expediente_em_voo == set(), "envio que explode libera a guarda")
check(_outbound(cid14) == [], "e nada foi gravado")
sent.clear()
asyncio.run(_chamadas_juntas(cid14, 1))
check(sent == [AVISO] and _outbound(cid14) == [(AVISO, "sent")],
      f"a tentativa seguinte envia o aviso normalmente (got {sent}, {_outbound(cid14)})")


# =====================================================================
# Ordem das decisoes do aviso: Bia responde > envio em voo > cooldown > envia
# =====================================================================
print("\nOrdem das decisoes: Bia responde > envio em voo > cooldown > envia")


def _despacha(cid, *, bia_vai_responder):
    """Uma execucao de `_send_auto_reply_if_needed`; devolve as mensagens INFO de skip do aviso."""
    sent.clear()
    captura_skip = _CapturaLog()
    nivel_antes = wh.logger.level
    wh.logger.addHandler(captura_skip)
    wh.logger.setLevel(logging.INFO)
    try:
        asyncio.run(_chamadas_juntas(cid, 1, bia_vai_responder=bia_vai_responder))
    finally:
        wh.logger.removeHandler(captura_skip)
        wh.logger.setLevel(nivel_antes)
    return [m for nivel, m in captura_skip.registros if nivel == "INFO" and "nao enviado" in m]


_horario(False)
_frases(out_of_hours=AVISO)
N15 = "5511911000018"
cid15 = _conversa(N15, bia=False, atendente_id=JULIA_ID)
_inbound(N15)                                    # grava um aviso recente: o cooldown fica armado
check(sent == [AVISO], f"pre-condicao: o 1o aviso saiu e armou o cooldown (got {sent})")
wh._avisos_fora_do_expediente_em_voo.add(cid15)  # ...e o envio de OUTRO inbound desta conversa esta em voo
try:
    pulos = _despacha(cid15, bia_vai_responder=True)
    check(sent == [] and len(pulos) == 1 and "a Bia vai responder" in pulos[0],
          f"Bia responde + envio em voo + cooldown: vence 'a Bia vai responder' (got {sent}, {pulos!r})")
    pulos = _despacha(cid15, bia_vai_responder=False)
    check(sent == [] and len(pulos) == 1 and "esta em envio" in pulos[0],
          f"envio em voo + cooldown: vence 'esta em envio' (got {sent}, {pulos!r})")
finally:
    wh._avisos_fora_do_expediente_em_voo.discard(cid15)
pulos = _despacha(cid15, bia_vai_responder=False)
check(sent == [] and len(pulos) == 1 and "ja enviado nas ultimas" in pulos[0],
      f"so o cooldown: 'ja enviado nas ultimas 12h' (got {sent}, {pulos!r})")
_retroceder_avisos(cid15, 13)
pulos = _despacha(cid15, bia_vai_responder=False)
check(sent == [AVISO] and pulos == [], f"nada impede: o aviso sai (got {sent}, {pulos!r})")


# =====================================================================
# business_hours VAZIA (estado inicial de producao): sem linha = aberto o dia todo = nenhum aviso
# =====================================================================
print("\nbusiness_hours vazia (producao antes de salvar os horarios): sem linha = aberto, nenhum aviso")

s = SessionLocal()
s.query(BusinessHours).delete()
s.commit()
linhas_horario = s.query(BusinessHours).count()
s.close()
check(linhas_horario == 0, f"pre-condicao: a tabela business_hours esta vazia (got {linhas_horario})")
_frases(out_of_hours=AVISO)        # SO o aviso existe: se algo for enviado, so pode ser ele
_agente(True)
N17 = "5511911000020"
cid17 = _conversa(N17, bia=False, atendente_id=JULIA_ID)
_inbound(N17)
check(sent == [], f"tabela vazia + Bia desligada + out_of_hours ativo: nada e enviado (got {sent})")
check(_outbound(cid17) == [], f"e nada e gravado (got {_outbound(cid17)})")
_horario(False)                    # contraste: com os 7 dias SALVOS como fechados, a mesma conversa recebe o aviso
_inbound(N17, "e agora?")
check(sent == [AVISO], f"com os 7 dias salvos como fechados o aviso sai (got {sent})")


# =====================================================================
# D7 — a guarda e liberada mesmo se o banco cair logo depois de gravar o aviso
# =====================================================================
print("\nD7 — banco indisponivel logo apos gravar o aviso: a guarda nao fica presa")

# `_save_outbound_message` commita, e o commit EXPIRA a conversa: reler `conversation.id` no `finally`
# faz um SELECT. Com o banco fora do ar ele levantaria DENTRO do `finally` — mascara o erro original,
# pula o `discard` e deixa o id preso na guarda ate o processo reiniciar. O fix e capturar o id antes.
caida = {"on": False}


def _derruba_consultas(conn, cursor, statement, parameters, context, executemany):
    if caida["on"]:
        raise OperationalError(statement, parameters, Exception("banco indisponivel (simulado)"))


_salvar_original = wh._save_outbound_message


def _salva_e_derruba(conversation, content, db, wa_response):
    resultado = _salvar_original(conversation, content, db, wa_response)
    caida["on"] = True      # o aviso JA foi gravado; a partir daqui qualquer consulta falha
    return resultado


_horario(False)
_frases(out_of_hours=AVISO)
N18 = "5511911000021"
cid18 = _conversa(N18, bia=False, atendente_id=JULIA_ID)
sent.clear()
erro_inesperado = None
event.listen(engine, "before_cursor_execute", _derruba_consultas)
wh._save_outbound_message = _salva_e_derruba
try:
    asyncio.run(_chamadas_juntas(cid18, 1))
except Exception as exc:        # noqa: BLE001 — queremos saber se ALGO estourou, seja o que for
    erro_inesperado = exc
finally:
    caida["on"] = False
    wh._save_outbound_message = _salvar_original
    event.remove(engine, "before_cursor_execute", _derruba_consultas)
check(wh._avisos_fora_do_expediente_em_voo == set(),
      f"banco fora do ar logo apos o commit: a guarda e liberada (presos: {wh._avisos_fora_do_expediente_em_voo})")
check(erro_inesperado is None,
      f"e nada estoura depois do commit (got {type(erro_inesperado).__name__}: {erro_inesperado})")
check(sent == [AVISO] and _outbound(cid18) == [(AVISO, "sent")],
      f"o aviso foi enviado e gravado uma unica vez (got {sent}, {_outbound(cid18)})")


# =====================================================================
# D7 — fronteira EXATA das 12h, com o relogio injetado: naive (SQLite) e aware (PostgreSQL)
# =====================================================================
print("\nD7 — fronteira das 12h com o relogio fixo: 12h pode avisar, 12h menos 1 microssegundo bloqueia")

AGORA = datetime(2026, 10, 7, 12, 0, 0, 123456, tzinfo=tz.utc)
DOZE_HORAS = timedelta(hours=12)
UM_MICROSSEGUNDO = timedelta(microseconds=1)
BRASILIA = tz(timedelta(hours=-3))
check(wh.OUT_OF_HOURS_COOLDOWN == DOZE_HORAS, "a constante do cooldown e de 12h (decisao D7)")

N16 = "5511911000019"
cid16 = _conversa(N16, bia=False, atendente_id=JULIA_ID)
s = SessionLocal()
s.add(Message(conversation_id=cid16, direction="outbound", content=AVISO, msg_type="text",
              status="sent", created_at=AGORA - DOZE_HORAS))
s.commit()
s.close()


def _recente_no_sqlite(enviado_em):
    """Regrava o `created_at` do aviso e consulta com o relogio fixo em AGORA. O SQLite devolve naive."""
    sessao = SessionLocal()
    try:
        sessao.query(Message).filter(Message.conversation_id == cid16, Message.content == AVISO).update(
            {Message.created_at: enviado_em}, synchronize_session=False)
        sessao.commit()
        return wh._aviso_fora_do_expediente_recente(sessao, cid16, AVISO, agora=AGORA)
    finally:
        sessao.close()


class _ConsultaFalsa:
    """Responde a cadeia `query().filter().order_by().first()` com uma linha fixa."""

    def __init__(self, linha):
        self._linha = linha

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return self._linha


class _SessaoFalsa:
    def __init__(self, linha):
        self._linha = linha

    def query(self, *args, **kwargs):
        return _ConsultaFalsa(self._linha)


def _recente_com_datetime_aware(enviado_em):
    """O PostgreSQL devolve `created_at` COM tzinfo (o SQLite nunca): a linha falsa reproduz isso."""
    sessao = _SessaoFalsa(SimpleNamespace(created_at=enviado_em))
    return wh._aviso_fora_do_expediente_recente(sessao, 1, AVISO, agora=AGORA)


check(_recente_no_sqlite(AGORA - DOZE_HORAS) is False,
      "SQLite (naive): aviso de EXATAMENTE 12h atras nao bloqueia (pode avisar de novo)")
check(_recente_no_sqlite(AGORA - DOZE_HORAS + UM_MICROSSEGUNDO) is True,
      "SQLite (naive): 12h menos 1 microssegundo AINDA bloqueia")
check(_recente_no_sqlite(AGORA - DOZE_HORAS - UM_MICROSSEGUNDO) is False,
      "SQLite (naive): 12h mais 1 microssegundo nao bloqueia")
check(_recente_no_sqlite(AGORA - timedelta(minutes=1)) is True, "SQLite (naive): aviso de 1 minuto atras bloqueia")

check(_recente_com_datetime_aware(AGORA - DOZE_HORAS) is False,
      "PostgreSQL (aware): aviso de EXATAMENTE 12h atras nao bloqueia")
check(_recente_com_datetime_aware(AGORA - DOZE_HORAS + UM_MICROSSEGUNDO) is True,
      "PostgreSQL (aware): 12h menos 1 microssegundo AINDA bloqueia")
check(_recente_com_datetime_aware((AGORA - DOZE_HORAS).astimezone(BRASILIA)) is False,
      "PostgreSQL (aware, UTC-3): o MESMO instante de 12h atras, em outro fuso, nao bloqueia")
check(_recente_com_datetime_aware((AGORA - DOZE_HORAS + UM_MICROSSEGUNDO).astimezone(BRASILIA)) is True,
      "PostgreSQL (aware, UTC-3): 12h menos 1 microssegundo, em outro fuso, bloqueia")
check(wh._aviso_fora_do_expediente_recente(_SessaoFalsa(None), 1, AVISO, agora=AGORA) is False,
      "sem nenhum aviso anterior: nao bloqueia")


# ─── Resultado ──────────────────────────────────────────────────────────
wh.whatsapp.send_text_message = _orig_send_text
wh._schedule_agent_debounce = _orig_schedule
wh.N8N_AGENT_ENABLED = _orig_agent_enabled
main.app.dependency_overrides.clear()

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FALHA(S):")
    for f in failures:
        print(f"  - {_safe(f)}")
    sys.exit(1)
print("TODOS OS TESTES DE FORA DO EXPEDIENTE PASSARAM")
