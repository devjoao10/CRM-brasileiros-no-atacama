"""
AUDIT-2026-08-WG — regressao: cache-busting de /static/** em base.html e
login.html (BUG 3).

Antes, base.html versionava o CSS a mao (?v=2, ?v=1) mas carregava
/static/js/auth.js, /static/js/layout.js e /static/js/notifications.js sem
NENHUM cache-buster; login.html tinha o mesmo problema em auth.js/login.js
(e o proprio variables.css nem tinha query string). Depois de um deploy, o
navegador continuava servindo o JS/CSS antigo do cache ate um hard refresh
manual. app/config.py ja tinha VERSION = "1.0.0" mas nada a referenciava.

Prova, em duas camadas:
  1. Fonte (estatico): TODO src=/href= para /static/** nos dois templates
     usa o MESMO placeholder Jinja (?v={{ asset_version }}) — nao um numero
     colado a mao, e nao um mix de valores.
  2. Renderizado (via app.routers.pages, dono da wiring): o placeholder
     resolve para app.config.VERSION de verdade, provando que o valor vem
     do contexto renderizado e nao e coincidencia de dois literais iguais.

Roda standalone:  python tests/test_asset_cache_busting.py
"""
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "templates"

os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("SECRET_KEY", "test-secret-key-asset-cache-busting")
os.environ.setdefault("DATABASE_URL", "sqlite:///./scratch/asset_cache_busting_test.db")
# Nao precisamos do seed do admin — so da wiring de asset_version — mas
# importar app.config em dev com SEED_INITIAL_ADMIN=true (o default) exige
# ADMIN_INITIAL_PASSWORD. Desliga o seed em vez de inventar uma senha fixture.
os.environ.setdefault("SEED_INITIAL_ADMIN", "false")
(ROOT / "scratch").mkdir(exist_ok=True)

sys.path.insert(0, str(ROOT))

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


STATIC_REF_RE = re.compile(r'(?:src|href)="(/static/[^"]*)"')
TOKEN = "?v={{ asset_version }}"


def check_source_versions(name):
    html = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    refs = STATIC_REF_RE.findall(html)
    check(len(refs) > 0, f"{name}: tem referencias /static/** para checar (achou {len(refs)})")
    for ref in refs:
        check(ref.endswith(TOKEN), f"{name}: {ref!r} termina com o placeholder dinamico {TOKEN!r}")
    check("?v=2" not in html and "?v=1\"" not in html and "?v=3" not in html,
          f"{name}: literais antigos (?v=1/?v=2/?v=3) foram substituidos pela mesma variavel")
    return refs


print("Cache busting — fonte de templates/base.html")
base_refs = check_source_versions("base.html")

print("\nCache busting — fonte de templates/login.html")
login_refs = check_source_versions("login.html")
# login.html tinha variables.css SEM NENHUMA query string antes do fix —
# reforca que ele tambem entrou no cache-busting, nao so o JS.
check(any("variables.css" in r for r in login_refs),
      "login.html: variables.css tambem ganhou o token (antes nao tinha nem ?v= nenhum)")


print("\nCache busting — token vem do contexto renderizado, nao e coincidencia")
from app.routers.pages import templates as page_templates  # noqa: E402
from app.config import VERSION  # noqa: E402

expected = f"?v={VERSION}"
check(bool(VERSION), "app.config.VERSION esta definido (fonte da verdade)")

rendered_login = page_templates.env.get_template("login.html").render()
check(expected in rendered_login, f"login.html renderizado contem o token esperado ({expected})")
check("{{ asset_version }}" not in rendered_login,
      "login.html renderizado nao deixou o placeholder Jinja sem resolver")
check(rendered_login.count("?v=") == rendered_login.count(expected),
      "login.html renderizado: todo ?v= encontrado e EXATAMENTE o mesmo valor (sem drift)")

rendered_base = page_templates.env.get_template("base.html").render(
    page_title="Teste", active_nav="dashboard", sector="comercial",
)
check(expected in rendered_base, f"base.html renderizado contem o token esperado ({expected})")
check("{{ asset_version }}" not in rendered_base,
      "base.html renderizado nao deixou o placeholder Jinja sem resolver")

base_static_tokens = {m for m in re.findall(r'\?v=([^"&]+)', rendered_base)}
check(base_static_tokens == {VERSION},
      f"base.html renderizado: um unico valor de versao em uso ({base_static_tokens})")

# --- Resultado ---
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE CACHE BUSTING PASSARAM")
