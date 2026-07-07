"""
CONV-MOBILE-PWA-01 — layout mobile-first do Conversas + fundacao PWA.

Contexto: o breakpoint 640px ja fazia da .conv-sidebar um drawer off-canvas e
o #mobileBack ja tinha handler, mas o fluxo nunca foi completado (nada abria a
lista no load nem fechava o drawer ao abrir a conversa; o botao voltar tinha
display:none inline sem regra mobile). Este pacote completa o fluxo
(lista -> chat -> voltar), torna o composer seguro com teclado aberto e
adiciona manifest PWA SEM service worker.

Prova que (guards estaticos sobre JS/HTML/CSS/manifest + smoke via TestClient):
  1. Breakpoints mobile existem, com a secao CONV-MOBILE-PWA-01 no CSS.
  2. Fluxo single-column existe (drawer 100%, estado .open governado por JS,
     back-to-list ligado ao #mobileBack existente).
  3. Composer keyboard-safe (dvh + safe-area-inset-bottom).
  4. Sem overflow horizontal (guarda no shell; sem width fixa nova > viewport).
  5. Paleta "/" e modal de tags tem regras mobile.
  6. Controles de notificacao preservados no markup.
  7. NENHUM serviceWorker; 8. NENHUM PushManager/Web Push.
  9. NENHUM keydown global novo (invariante compartilhado).
 10. Markers dos pacotes anteriores intactos; secao nova sem innerHTML.
 11. Pagina renderiza com viewport meta; manifest valido, linkado e servido.
 12. Cache-bust versionado presente; sem segredo nas respostas.

Roda standalone:  python tests/test_conversas_mobile_pwa.py
"""
import json
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_mobile_pwa_test.db"
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

js = (CONVERSAS_DIR / "static" / "js" / "conversas.js").read_text(encoding="utf-8")
html = (CONVERSAS_DIR / "templates" / "conversas.html").read_text(encoding="utf-8")
css = (CONVERSAS_DIR / "static" / "css" / "conversas.css").read_text(encoding="utf-8")

# ============ 1. BREAKPOINTS + SECAO CSS ============
print("MOBILE — breakpoints e secao CSS")
check("CONV-MOBILE-PWA-01: mobile-first" in css and "fim CONV-MOBILE-PWA-01" in css,
      "secao CONV-MOBILE-PWA-01 presente no CSS")
css_start = css.find("CONV-MOBILE-PWA-01: mobile-first")
css_end = css.find("fim CONV-MOBILE-PWA-01")
css_section = css[css_start:css_end]
check("@media (max-width: 640px)" in css_section, "breakpoint mobile 640px na secao")
check("@media (max-width: 420px)" in css_section, "breakpoint small-mobile 420px na secao")
check("@media (max-width: 1200px)" in css_section, "override do lead-panel <=1200px")

# ============ 2. FLUXO SINGLE-COLUMN ============
print("\nMOBILE — fluxo lista -> chat -> voltar")
check(".conv-sidebar { width: 100%; }" in css_section, "drawer da lista em tela cheia")
check("transform: translateX(-100%)" in css, "drawer off-canvas pre-existente preservado")
check("CONV-MOBILE-PWA-01: fluxo mobile" in js and "fim CONV-MOBILE-PWA-01" in js,
      "secao JS do fluxo mobile presente (markers)")
js_start = js.find("CONV-MOBILE-PWA-01: fluxo mobile")
js_end = js.find("fim CONV-MOBILE-PWA-01", js_start)
js_section = js[js_start:js_end]
check("matchMedia('(max-width: 640px)')" in js_section, "estado mobile via matchMedia")
check("classList.add('open')" in js_section, "load mobile abre a lista (drawer .open)")
check("classList.remove('open')" in js_section, "abrir conversa fecha o drawer")
check("closeMobileDrawerForChat();" in js.split("async function loadChat")[1][:1200],
      "loadChat fecha o drawer no mobile")
check("getElementById('mobileBack')" in js, "back-to-list ligado ao #mobileBack existente")
check(".mobile-back" in css_section and "display: flex !important" in css_section,
      "botao voltar revelado no mobile (vence o display:none inline)")

# ============ 3. COMPOSER KEYBOARD-SAFE ============
print("\nMOBILE — composer seguro com teclado")
check("height: 100dvh" in css_section, "altura do app em dvh (teclado mobile)")
check("env(safe-area-inset-bottom)" in css_section, "safe-area no composer")
check(".chat-input .btn-send { width: 44px; height: 44px; }" in css_section,
      "botao enviar com alvo de toque 44px")

# ============ 4. SEM OVERFLOW HORIZONTAL ============
print("\nMOBILE — sem overflow horizontal")
check("overflow-x: hidden" in css_section, "guarda de overflow-x no shell mobile")
# so declaracoes width: reais (exclui max-width/min-width das media queries)
fixed_widths = [int(m) for m in re.findall(r"(?<!max-)(?<!min-)width:\s*(\d{3,})px", css_section)]
check(all(w <= 420 for w in fixed_widths),
      f"nenhuma width fixa nova acima do viewport mobile (got {fixed_widths})")

# ============ 5. PALETA "/" E MODAL DE TAGS ============
print("\nMOBILE — paleta e modal usaveis")
check(".qr-palette { left: 8px; right: 8px; width: auto; }" in css_section,
      "paleta '/' cabe no viewport mobile")
check(".tag-modal" in css_section and "calc(100vw - 32px)" in css_section,
      "modal de tags cabe no viewport mobile")

# ============ 6. CONTROLES DE NOTIFICACAO PRESERVADOS ============
print("\nMOBILE — notificacoes preservadas")
check('id="convNotificationCount"' in html and 'id="convNotificationEnable"' in html
      and 'id="convNotificationMute"' in html,
      "controles de notificacao intactos no markup")
check(".conv-sidebar-header h1 { display: none; }" in css_section,
      "header mobile abre espaco (titulo oculto, logo mantem a marca)")

# ============ 7-9. SEM SERVICE WORKER / PUSH / KEYDOWN GLOBAL ============
print("\nMOBILE — gates de escopo")
check("serviceWorker" not in js and "serviceWorker" not in html,
      "NENHUM serviceWorker no JS/HTML")
check("PushManager" not in js and "pushManager" not in js, "NENHUM PushManager/Web Push")
check("document.addEventListener('keydown'" not in js,
      "NENHUM keydown global (invariante compartilhado)")

# ============ 10. MARKERS ANTIGOS + SECAO NOVA LIMPA ============
print("\nMOBILE — convivencia com pacotes anteriores")
check("CONV-HOTFIX-QUICK-REPLIES-01: paleta" in js, "marker de quick replies intacto")
check("CONV-NOTIFICATIONS-01: notificacoes leves" in js
      and "fim CONV-NOTIFICATIONS-01" in js, "markers de notificacoes intactos")
check("innerHTML" not in js_section, "secao mobile do JS sem innerHTML")
check("try {" in js_section and "catch" in js_section,
      "fluxo mobile defensivo (falha nunca derruba o chat)")

# ============ 11. PAGINA + MANIFEST ============
print("\nPWA — pagina, manifest e link")
r_page = client.get("/")
check(r_page.status_code == 200, "GET / (pagina do Conversas) -> 200")
check('name="viewport"' in r_page.text and "width=device-width" in r_page.text,
      "viewport meta presente na pagina")
check('rel="manifest"' in html and "/static/manifest.json" in html,
      "manifest linkado no template")
check('name="theme-color"' in html, "theme-color meta presente")

manifest_path = CONVERSAS_DIR / "static" / "manifest.json"
check(manifest_path.exists(), "manifest.json existe em conversas/static/")
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
check(manifest.get("name") == "BNA Conversas" and manifest.get("short_name") == "Conversas",
      "manifest: name/short_name corretos")
check(manifest.get("display") == "standalone" and manifest.get("scope") == "/",
      "manifest: display standalone e scope /")
check(manifest.get("start_url") == "/",
      "manifest: start_url '/' (pagina do Conversas e servida em / neste app)")
check(manifest.get("theme_color") == "#366B85" and manifest.get("background_color") == "#F9F8F7",
      "manifest: cores da identidade BNA (--primary / --light)")
icons = manifest.get("icons") or []
check(len(icons) == 1 and icons[0]["src"] == "/static/img/logo.png"
      and (CONVERSAS_DIR / "static" / "img" / "logo.png").exists(),
      "manifest: icone reusa logo existente da marca (nenhum asset inventado)")
# o mount StaticFiles(directory="static") e RELATIVO ao cwd; em producao o
# app Conversas roda de dentro de conversas/ — reproduz isso na requisicao
_prev_cwd = os.getcwd()
os.chdir(CONVERSAS_DIR)
try:
    r_manifest = client.get("/static/manifest.json")
finally:
    os.chdir(_prev_cwd)
check(r_manifest.status_code == 200 and r_manifest.json().get("name") == "BNA Conversas",
      "manifest servido pelo app em /static/manifest.json (cwd conversas/)")
check("SECRET" not in manifest_path.read_text(encoding="utf-8").upper(),
      "manifest sem strings sensiveis")

# ============ 12. CACHE-BUST + SEM SEGREDO ============
print("\nMOBILE — cache-bust e segredos")
check("conversas.js?v=" in html and "conversas.css?v=" in html,
      "cache-bust versionado de JS e CSS presente")
check("test-secret-key" not in r_page.text and "test-secret-key" not in r_manifest.text,
      "respostas sem SECRET_KEY")

# --- Resultado ---
main.app.dependency_overrides.clear()
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE MOBILE/PWA PASSARAM")
