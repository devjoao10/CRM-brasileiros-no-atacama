"""
CONV-HOTFIX-QUICK-REPLIES-01 — mensagens rapidas com "/" no composer.

Contexto: o backend de quick replies (modelo + CRUD /api/quick-replies +
/api/quick-replies/search + tela de settings) ja existia; o gatilho "/" no
chat NUNCA foi implementado no conversas.js — apertar "/" nao fazia nada.

Prova que:
  1. Backend (fonte pre-existente): criar/listar/buscar mensagens rapidas
     funciona (regressao da fonte usada pela paleta).
  2. Frontend (guards estaticos): paleta presente no markup, gatilho ligado
     SOMENTE ao composer, teclado (ArrowUp/Down/Enter/Esc), selecao insere
     SEM enviar, rendering XSS-safe (createElement/textContent, sem
     innerHTML na secao da paleta), cache-bust atualizado.

Roda standalone:  python tests/test_conversas_quick_replies.py
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_quick_replies_test.db"
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
from app.database import engine, Base  # noqa: E402
from app.auth import get_current_user  # noqa: E402

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


# ============ 1. BACKEND — fonte pre-existente da paleta ============
print("QR — backend /api/quick-replies (fonte da paleta)")
r1 = client.post("/api/quick-replies", json={
    "shortcut": "/boasvindas", "title": "Boas-vindas",
    "content": "Olá! Tudo bem? Como posso te ajudar?", "category": "Saudação",
})
check(r1.status_code == 201, "criar mensagem rapida -> 201")
r1b = client.post("/api/quick-replies", json={
    "shortcut": "/pagamento", "title": "Dados de pagamento",
    "content": "Segue o link de pagamento, qualquer dúvida me avise.",
})
check(r1b.status_code == 201, "segunda mensagem criada")
QR2_ID = r1b.json()["id"]

r1c = client.post("/api/quick-replies", json={
    "shortcut": "/boasvindas", "title": "Dup", "content": "x",
})
check(r1c.status_code == 409, "atalho duplicado -> 409")

lst = client.get("/api/quick-replies").json()
check(lst["total"] == 2, f"list retorna as 2 ativas (got {lst['total']})")

srch = client.get("/api/quick-replies/search?q=boas").json()
check(srch["total"] == 1 and srch["quick_replies"][0]["shortcut"] == "/boasvindas",
      "search?q= filtra por atalho/titulo/conteudo")

# inativa some da fonte da paleta (list usa active_only=True por default)
client.put(f"/api/quick-replies/{QR2_ID}", json={"is_active": False})
lst2 = client.get("/api/quick-replies").json()
check(lst2["total"] == 1, "inativa nao aparece no list default (paleta so ve ativas)")

# ============ 2. FRONTEND — guards estaticos da paleta ============
print("\nQR — guards estaticos do frontend")
js = (CONVERSAS_DIR / "static" / "js" / "conversas.js").read_text(encoding="utf-8")
html = (CONVERSAS_DIR / "templates" / "conversas.html").read_text(encoding="utf-8")
css = (CONVERSAS_DIR / "static" / "css" / "conversas.css").read_text(encoding="utf-8")

# markup + estilo
check('id="qrPalette"' in html and 'role="listbox"' in html, "markup da paleta presente (listbox)")
check(".qr-palette" in css and ".qr-palette-item" in css, "CSS da paleta presente")
# cache-bust: exige referencia VERSIONADA (nao pinna o valor — pacotes
# futuros bumpam a versao; o guard de valor exato quebrava a cada bump)
check("conversas.js?v=" in html, "cache-bust do JS presente")
check("conversas.css?v=" in html, "cache-bust do CSS presente")

# secao da paleta no JS
start = js.find("CONV-HOTFIX-QUICK-REPLIES-01: paleta")
end = js.find("CONV-07: Atribuicao", start)
check(start != -1 and end > start, "secao da paleta presente no conversas.js")
section = js[start:end]

# XSS-safe: criacao segura de DOM, nunca innerHTML na secao da paleta
check("createElement" in section and ".textContent =" in section,
      "itens renderizados com createElement/textContent")
check("innerHTML" not in section,
      "NENHUM innerHTML na secao da paleta (conteudo e controlado por usuarios)")
check("replaceChildren()" in section, "limpeza da paleta via replaceChildren (sem innerHTML='')")

# gatilho ligado SOMENTE ao composer
check("getElementById('msgInput').addEventListener('input', updateQrPalette)" in js,
      "gatilho/filtro ligado ao #msgInput")
check("document.addEventListener('keydown'" not in js,
      "NENHUM keydown global (paleta nao abre fora do composer)")
check("startsWith('/')" in section, "paleta so abre com '/' no INICIO do composer")

# teclado: setas, Esc, Enter
check("'ArrowDown'" in section and "'ArrowUp'" in section, "navegacao com ArrowDown/ArrowUp")
check("'Escape'" in section and "closeQrPalette(true)" in section,
      "Esc fecha e mantem '/' literal (dismiss)")
check("e.key === 'Enter'" in section and "selectQuickReply(qrIndex)" in section,
      "Enter seleciona o item destacado")

# selecao INSERE e NAO envia
check("sendMessage" not in section,
      "secao da paleta NUNCA chama sendMessage (nao envia automaticamente)")
check("if (handleQrPaletteKeydown(e)) return;" in js,
      "keydown do composer consulta a paleta ANTES do envio")
idx_gate = js.find("if (handleQrPaletteKeydown(e)) return;")
idx_send = js.find("sendMessage();", idx_gate)
check(idx_gate != -1 and idx_send != -1 and idx_gate < idx_send,
      "gate da paleta vem ANTES do sendMessage no mesmo handler")

# fechar ao clicar fora
check("closeQrPalette(false)" in js and "palette.contains(e.target)" in js,
      "clique fora fecha a paleta")

# ============ 3. SEM SEGREDO ============
print("\nQR — nenhum segredo nas respostas")
body = client.get("/api/quick-replies").text
check("test-secret-key" not in body, "resposta sem SECRET_KEY")

# --- Resultado ---
main.app.dependency_overrides.clear()
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE QUICK REPLIES PASSARAM")
