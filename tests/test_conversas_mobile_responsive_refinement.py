"""
CONV-MOBILE-RESPONSIVE-02 — refinamento responsivo completo do Conversas
(app shell mobile: chat + info do contato + Templates + Settings).

Causas-raiz corrigidas:
- Zoom no iPhone: TODOS os campos tinham font-size < 16px (composer 14px,
  busca 13px, selects 12px INLINE) -> focus-zoom do iOS que persiste; e
  faltava viewport-fit=cover para o safe-area funcionar de verdade.
- Info do contato inacessivel: o display:none !important <=1200px do
  CONV-MOBILE-PWA-01 escondia o #leadPanel permanentemente.
- Templates/Settings: .page-sidebar 240px fixa sem regra mobile (sobrepoe o
  conteudo), grids/forms/abas desktop-first.

Prova que (guards estaticos sobre os 3 templates + CSS + JS novos):
  1. Anti-zoom iOS (>=16px em input/select/textarea no mobile) +
     viewport-fit=cover nos 3 templates.
  2. Sem service worker. 3. Sem PushManager/Web Push. 4. Sem keydown global.
  5. Markers CONV-MOBILE-RESPONSIVE-02 no CSS e no JS.
  6-7. Fluxo lista->chat->voltar preservado (#mobileBack).
  8-10. Info do contato: abre por estado, fecha por botao, drawer/subview
     mobile reabrivel (o !important cego foi superado por regra especifica).
  11-13. Paleta "/", composer keyboard-safe e notificacoes preservados.
  14-15. Templates e Settings com regras mobile (drawer+backdrop; chips).
  16-17. Sem width fixa > viewport; overflow-x protegido.
  18. Manifest preservado e linkado. 19-20. Markers anteriores intactos.
  21. Paginas renderizam 200. 23. Sem mudanca de backend/migrations no diff.

Roda standalone:  python tests/test_conversas_mobile_responsive_refinement.py
"""
import json
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_mobile_resp02_test.db"
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
shell_js = (CONVERSAS_DIR / "static" / "js" / "page-shell.js").read_text(encoding="utf-8")
css = (CONVERSAS_DIR / "static" / "css" / "conversas.css").read_text(encoding="utf-8")
html_conv = (CONVERSAS_DIR / "templates" / "conversas.html").read_text(encoding="utf-8")
html_tpl = (CONVERSAS_DIR / "templates" / "templates.html").read_text(encoding="utf-8")
html_set = (CONVERSAS_DIR / "templates" / "settings.html").read_text(encoding="utf-8")
all_html = html_conv + html_tpl + html_set

# secoes novas por marker
css_start = css.find("CONV-MOBILE-RESPONSIVE-02: refinamento")
css_end = css.find("fim CONV-MOBILE-RESPONSIVE-02")
js_start = js.find("CONV-MOBILE-RESPONSIVE-02: info do contato")
js_end = js.find("fim CONV-MOBILE-RESPONSIVE-02", js_start)

# ============ 5. MARKERS DA SECAO NOVA ============
print("RESP02 — markers das secoes novas")
check(css_start != -1 and css_end > css_start, "secao CSS CONV-MOBILE-RESPONSIVE-02 presente")
check(js_start != -1 and js_end > js_start, "secao JS CONV-MOBILE-RESPONSIVE-02 presente")
css_section = css[css_start:css_end]
js_section = js[js_start:js_end]

# ============ 1. ANTI-ZOOM iOS ============
print("\nRESP02 — anti-zoom iOS")
check("input, select, textarea { font-size: 16px !important; }" in css_section,
      "campos >= 16px no mobile (vence font-size inline; sem focus-zoom iOS)")
for name, h in (("conversas", html_conv), ("templates", html_tpl), ("settings", html_set)):
    check("viewport-fit=cover" in h, f"viewport-fit=cover em {name}.html (safe-area real no iOS)")
check("user-scalable=no" not in all_html and "maximum-scale" not in all_html,
      "pinch-zoom NAO desabilitado (fix por font-size, acessivel)")

# ============ 2-4. GATES ============
print("\nRESP02 — gates de escopo")
check("serviceWorker" not in js and "serviceWorker" not in shell_js
      and "serviceWorker" not in all_html, "NENHUM serviceWorker")
check("PushManager" not in js and "PushManager" not in shell_js, "NENHUM PushManager/Web Push")
check("document.addEventListener('keydown'" not in js
      and "addEventListener('keydown'" not in shell_js,
      "NENHUM keydown global (invariante compartilhado)")

# ============ 6-7. FLUXO LISTA->CHAT->VOLTAR PRESERVADO ============
print("\nRESP02 — fluxo lista->chat->voltar preservado")
check("CONV-MOBILE-PWA-01: fluxo mobile" in js and "closeMobileDrawerForChat" in js,
      "fluxo do CONV-MOBILE-PWA-01 intacto")
check('id="mobileBack"' in html_conv and "getElementById('mobileBack')" in js,
      "back-to-list (#mobileBack) presente e ligado")

# ============ 8-10. INFO DO CONTATO COMO SUBVIEW ============
print("\nRESP02 — info do contato acessivel no mobile")
check("conv-mobile-info-open" in js_section and "classList.add" in js_section,
      "estado .conv-mobile-info-open governado pela secao nova")
check("infoLayoutQuery.matches" in js and "classList.toggle('conv-mobile-info-open')" in js,
      "#btnToggleInfo abre a subview no mobile/tablet (<=1200px)")
check('id="btnCloseInfoMobile"' in html_conv and "btnCloseInfoMobile" in js_section,
      "botao fechar da subview presente e ligado")
check("body.conv-mobile-info-open .lead-panel" in css_section
      and "display: flex !important" in css_section
      and "position: fixed" in css_section,
      "regra especifica REABRE o lead-panel (supera o display:none !important cego)")
check("closeMobileInfoPanel(); // CONV-MOBILE-RESPONSIVE-02" in js
      or "closeMobileInfoPanel();" in js.split("async function loadChat")[1][:1500],
      "trocar de conversa/voltar reseta a subview de info")

# ============ 11-13. PALETA / COMPOSER / NOTIFICACOES ============
print("\nRESP02 — paleta, composer e notificacoes preservados")
check(".qr-palette { left: 8px; right: 8px; width: auto; }" in css,
      "paleta '/' mobile-safe (regra do pacote anterior intacta)")
check("height: 100dvh" in css and "env(safe-area-inset-bottom)" in css,
      "composer keyboard-safe (dvh + safe-area) presente")
check('id="convNotificationCount"' in html_conv and 'id="convNotificationMute"' in html_conv,
      "controles de notificacao presentes")

# ============ 14. TEMPLATES MOBILE ============
print("\nRESP02 — pagina Templates mobile")
check(".page-sidebar" in css_section and "translateX(-100%)" in css_section,
      "sidebar das paginas vira drawer off-canvas no mobile")
check("body.conv-mobile-menu-open .page-sidebar" in css_section,
      "drawer abre por estado .conv-mobile-menu-open")
check("page-drawer-backdrop" in css_section, "backdrop do drawer presente no CSS")
check('id="pageMenuBtn"' in html_tpl and 'id="pageDrawerBackdrop"' in html_tpl,
      "hamburguer e backdrop no templates.html")
check("page-shell.js" in html_tpl, "page-shell.js incluido no templates.html")
check(".templates-grid { grid-template-columns: 1fr; }" in css_section,
      "grid de templates em 1 coluna no mobile")
check(".form-row { grid-template-columns: 1fr; }" in css_section,
      "form-row em 1 coluna no mobile")
check("overflow-wrap: break-word" in css_section, "texto longo dos cards nao estoura")
check("max-width: calc(100vw - 24px)" in css_section, "modal cabe no viewport mobile")

# ============ 15. SETTINGS MOBILE ============
print("\nRESP02 — pagina Settings mobile")
check('id="pageMenuBtn"' in html_set and 'id="pageDrawerBackdrop"' in html_set,
      "hamburguer e backdrop no settings.html")
check("page-shell.js" in html_set, "page-shell.js incluido no settings.html")
check(".settings-tabs { overflow-x: auto" in css_section,
      "abas de settings viram chips rolaveis (logica do settings.js intacta)")
check(".settings-tab { white-space: nowrap; flex: 0 0 auto" in css_section,
      "chips nao encolhem e rolam horizontalmente")

# page-shell.js: estado so sob matchMedia, fechamento por backdrop/navegacao
check("matchMedia('(max-width: 640px)')" in shell_js, "page-shell gated por matchMedia")
check("backdrop.addEventListener('click', closeMenu)" in shell_js,
      "tocar no backdrop fecha o drawer")
check("closest && e.target.closest('a')" in shell_js, "navegar fecha o drawer")

# ============ 16-17. SEM OVERFLOW / SEM WIDTH FIXA ============
print("\nRESP02 — sem overflow horizontal")
fixed_widths = [int(m) for m in re.findall(r"(?<!max-)(?<!min-)width:\s*(\d{3,})px", css_section)]
check(all(w <= 420 for w in fixed_widths),
      f"nenhuma width fixa nova acima do viewport mobile (got {fixed_widths})")
check("overflow-x: hidden" in css_section or "overflow-x: hidden" in css,
      "protecao de overflow-x no mobile")
check(".filter-search { width: 100%; }" in css_section, "busca de templates nao forca 240px")

# ============ 18-20. MANIFEST + MARKERS ANTERIORES ============
print("\nRESP02 — manifest e markers anteriores intactos")
check('rel="manifest"' in html_conv and "/static/manifest.json" in html_conv,
      "manifest preservado e linkado")
manifest = json.loads((CONVERSAS_DIR / "static" / "manifest.json").read_text(encoding="utf-8"))
check(manifest.get("name") == "BNA Conversas", "manifest valido e inalterado")
check("CONV-NOTIFICATIONS-01: notificacoes leves" in js and "fim CONV-NOTIFICATIONS-01" in js,
      "markers de notificacoes intactos")
check("CONV-HOTFIX-QUICK-REPLIES-01: paleta" in js, "marker de quick replies intacto")
check("CONV-MOBILE-PWA-01: mobile-first" in css and "fim CONV-MOBILE-PWA-01" in css,
      "secao CSS do CONV-MOBILE-PWA-01 intacta (guards pinados preservados)")
check("innerHTML" not in js_section and "innerHTML" not in shell_js,
      "secoes novas sem innerHTML")

# ============ 21. PAGINAS RENDERIZAM ============
print("\nRESP02 — paginas renderizam")
for path in ("/", "/templates", "/settings"):
    r = client.get(path)
    check(r.status_code == 200, f"GET {path} -> 200")

# ============ 23. SEM BACKEND/MIGRATION NO DIFF ============
print("\nRESP02 — sem backend/migration no pacote")
try:
    changed = subprocess.run(
        ["git", "diff", "--name-only", "origin/main"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=30,
    ).stdout.splitlines()
    bad = [f for f in changed if f.startswith("conversas/app/") or f.startswith("migrations/")]
    check(not bad, f"diff nao toca conversas/app nem migrations ({bad or 'ok'})")
except Exception as e:  # ambiente sem git: nao bloqueia a suite
    print(f"  SKIP: verificacao de diff indisponivel ({type(e).__name__})")

body = client.get("/").text
check("test-secret-key" not in body, "resposta sem SECRET_KEY")
check("conversas.js?v=" in html_conv and "conversas.css?v=" in html_conv,
      "cache-bust versionado presente")

# --- Resultado ---
main.app.dependency_overrides.clear()
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DO REFINAMENTO RESPONSIVO PASSARAM")
