"""
CONV-NOTIFICATIONS-01 — notificacoes leves de novas mensagens no Conversas.

Camada 100% frontend sobre o polling existente (sem migration, sem service
worker, sem Web Push, sem WebSocket/SSE). Deteccao: delta de unread_count por
conversa (lista) + Set de ids inbound vistos (conversa aberta). Texto de
notificacao do navegador e SEMPRE generico (nunca conteudo do cliente).

Prova que (guards estaticos sobre JS/HTML/CSS + smoke do contrato backend):
  1. Baseline existe e evita spam no primeiro load.
  2. Dedupe existe (mapa de unread + Set de ids inbound vistos).
  3. Badge no document.title existe COM caminho de reset (titulo original).
  4. Notification.requestPermission SO no clique do opt-in (nunca no load).
  5. Texto generico: sem nome/telefone/conteudo do cliente na notificacao.
  6. Som atras de flag de interacao e envolto em try/catch.
  7. NENHUM serviceWorker. 8. NENHUM PushManager/Web Push.
  9. NENHUM keydown global (nao quebra a paleta "/" de quick replies).
 10. Sem innerHTML na secao de notificacoes.
 11. Contrato backend: GET /api/conversations expoe unread_count.
 12. Sem segredo nas respostas.

Roda standalone:  python tests/test_conversas_notifications.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_notifications_test.db"
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
from app.database import engine, SessionLocal, Base  # noqa: E402
from app.auth import get_current_user  # noqa: E402
from app.models.conversation import Conversation  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


Base.metadata.create_all(bind=engine)


class _DummyUser:
    id = 1
    email = "tester@local"
    is_admin = True


main.app.dependency_overrides[get_current_user] = lambda: _DummyUser()
client = TestClient(main.app)

js = (CONVERSAS_DIR / "static" / "js" / "conversas.js").read_text(encoding="utf-8")
html = (CONVERSAS_DIR / "templates" / "conversas.html").read_text(encoding="utf-8")
css = (CONVERSAS_DIR / "static" / "css" / "conversas.css").read_text(encoding="utf-8")

# secao de notificacoes no JS (delimitada por markers)
start = js.find("CONV-NOTIFICATIONS-01: notificacoes leves")
end = js.find("fim CONV-NOTIFICATIONS-01", start)
check(start != -1 and end > start, "secao de notificacoes presente no conversas.js")
section = js[start:end]

# ============ 1-2. BASELINE + DEDUPE ============
print("NOTIF — baseline e dedupe")
check("baselined" in section and "st.baselined = true" in section,
      "baseline da 1a carga existe (nao notifica historico)")
check("unreadByConv" in section and "new Map()" in section,
      "mapa de unread_count por conversa (dedupe do delta)")
check("seenInboundIds" in section and "new Set()" in section,
      "Set de ids de mensagens inbound ja vistas (conversa aberta)")
check("direction !== 'inbound'" in section,
      "so mensagens INBOUND contam (outbound nunca notifica)")
check("c.id === activeConversation.id) return" in section,
      "delta da lista PULA a conversa aberta (sem notificacao dupla)")
check("processListNotifications(conversations)" in js,
      "hook do delta da lista no loadConversations")
check("processChatNotifications(data, true)" in js
      and "processChatNotifications(activeConversation, false)" in js,
      "hooks do chat: poll notifica, abertura so baselina")

# ============ 3. TITULO COM RESET ============
print("\nNOTIF — badge no titulo da aba")
check("originalTitle: document.title" in section, "titulo original capturado")
check("`(${notificationState.pending}) ${notificationState.originalTitle}`" in section,
      "titulo vira '(N) <original>' com pendentes")
check(": notificationState.originalTitle" in section,
      "titulo RESTAURADO exatamente quando o contador zera")
check("ackConvNotifications" in js and "window.addEventListener('focus', ackConvNotifications)" in js,
      "reset do contador ao focar a aba")
check("visibilitychange" in section, "reset tambem ao voltar a aba visivel")
check("ackConvNotifications();" in js.split("async function loadChat")[1][:600],
      "abrir a conversa da o visto (reset) no contador")

# ============ 4. PERMISSAO SO NO CLIQUE ============
print("\nNOTIF — Notification API opt-in")
check(js.count("Notification.requestPermission") == 1,
      "requestPermission aparece UMA vez no JS")
btn_idx = js.find("convNotificationEnable")
req_idx = js.find("Notification.requestPermission")
click_idx = js.find("btn.addEventListener('click'", btn_idx)
check(btn_idx != -1 and click_idx != -1 and click_idx < req_idx,
      "requestPermission SO dentro do click do botao opt-in (nunca no load)")
check("Notification.permission !== 'granted') return" in section,
      "sem permissao concedida -> fallback silencioso (titulo/badge)")
check('id="convNotificationEnable"' in html and "Ativar notifica" in html,
      "botao 'Ativar notificações' presente no markup")

# ============ 5. TEXTO GENERICO (PRIVACIDADE) ============
print("\nNOTIF — texto generico, sem dados do cliente")
check("'Nova mensagem no Conversas'" in section, "titulo generico fixo")
check("'Há uma nova mensagem aguardando atendimento.'" in section, "corpo generico fixo")
notif_call = section[section.find("new Notification("):section.find("tag: 'conv-nova-mensagem'")]
check(notif_call and "${" not in notif_call,
      "payload da Notification NAO interpola variaveis")
for campo in (".nome", ".whatsapp", ".ultimo_msg", ".content"):
    check(campo not in section,
          f"secao de notificacoes nao usa campo de cliente '{campo}'")
check("tag: 'conv-nova-mensagem'" in section, "tag fixa colapsa notificacoes repetidas")

# ============ 6. SOM GUARDADO ============
print("\nNOTIF — som gated por interacao e defensivo")
check("soundReady" in section and "{ once: true }" in section
      and "pointerdown" in section,
      "som liberado apenas apos a 1a interacao (autoplay policy)")
check("if (!notificationState.soundReady || notificationState.soundMuted) return" in section,
      "beep respeita soundReady e mute")
beep = section[section.find("function playNotificationBeep"):section.find("function showBrowserNotification")]
check("try {" in beep and "catch" in beep, "audio inteiro em try/catch (nunca quebra o chat)")
check('id="convNotificationMute"' in html, "toggle de som presente no markup")

# ============ 7-8. SEM SERVICE WORKER / WEB PUSH ============
print("\nNOTIF — sem service worker e sem Web Push")
check("serviceWorker" not in js and "serviceWorker" not in html,
      "NENHUM serviceWorker no JS/HTML")
check("PushManager" not in js and "pushManager" not in js,
      "NENHUM PushManager/Web Push")

# ============ 9-10. NAO QUEBRA QUICK REPLIES / DOM SEGURO ============
print("\nNOTIF — convivencia com a paleta '/' e DOM seguro")
check("document.addEventListener('keydown'" not in js,
      "NENHUM keydown global (invariante da paleta '/' preservado)")
check("innerHTML" not in section, "sem innerHTML na secao de notificacoes")
check(".textContent =" in section or "textContent" in section,
      "conteudo dinamico via textContent")
check("console.warn" in section and section.count("catch") >= 4,
      "camada de notificacao envolta em try/catch (falha nunca derruba o chat)")

# ============ 11. CONTRATO BACKEND (SMOKE) ============
print("\nNOTIF — contrato do polling")
s = SessionLocal()
s.add(Conversation(lead_id=0, whatsapp="5511900099001", nome="Notif Smoke",
                   status="aberta", unread_count=3))
s.commit()
s.close()
data = client.get("/api/conversations").json()
check(data["total"] >= 1 and "unread_count" in data["conversations"][0]
      and data["conversations"][0]["unread_count"] == 3,
      "GET /api/conversations expoe unread_count (sinal da deteccao)")

# ============ 12. MARKUP/CSS/CACHE-BUST + SEM SEGREDO ============
print("\nNOTIF — markup, estilos e cache-bust")
check('id="convNotificationCount"' in html, "contador visual presente no header")
check(".conv-notification-count" in css and "#convNotificationMute.muted" in css,
      "estilos do contador e do mute presentes")
# cache-bust versionado presente (valor exato nao e pinado — pacotes futuros
# bumpam a versao; o bump deste pacote e verificado no diff/report)
check("conversas.js?v=" in html and "conversas.css?v=" in html,
      "cache-bust versionado de JS e CSS presente")
body = client.get("/api/conversations").text
check("test-secret-key" not in body, "resposta sem SECRET_KEY")

# --- Resultado ---
main.app.dependency_overrides.clear()
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE NOTIFICACOES PASSARAM")
