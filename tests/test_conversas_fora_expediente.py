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

Webhook real (POST /webhook), Meta e n8n neutralizados, SEM patch de relogio:
"aberto/fechado agora" vem das 7 linhas de BusinessHours — todas fechadas = fora
do horario; todas 00:00-23:59 = dentro. Escrever os 7 dias (e nao so o de hoje)
elimina a corrida da virada de dia. O cooldown e provado recuando o `created_at`
do aviso anterior no banco.

Roda standalone:  python tests/test_conversas_fora_expediente.py
"""
import logging
import os
import pathlib
import re
import sys
from datetime import datetime, timedelta, timezone as tz

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


def _inbound(numero, texto="oi, tudo bem?"):
    """Uma mensagem do cliente pelo webhook real. Limpa o que foi capturado antes."""
    _seq["n"] += 1
    sent.clear()
    scheduled.clear()
    r = client.post("/webhook", json={"entry": [{"changes": [{"value": {
        "contacts": [{"profile": {"name": "Cliente Expediente"}}],
        "messages": [{
            "from": numero, "id": f"wamid.INOOH{_seq['n']}", "type": "text",
            "timestamp": str(int(datetime.now(tz.utc).timestamp())),
            "text": {"body": texto},
        }],
    }}]}]})
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
