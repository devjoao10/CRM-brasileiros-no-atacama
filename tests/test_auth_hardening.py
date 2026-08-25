"""
AUDIT-2026-08-W1A — endurecimento da autenticacao (wave 1).

Cada teste aqui prova UM achado confirmado da auditoria. Sem os fixes, os
testes falham exatamente como descrito no comentario de cada bloco.

  F1  header `Authorization` obsoleto nao pode mais anular um cookie valido;
  F2  token sem `typ: "access"` (ex.: `type: "verify_email"`, entregue na URL)
      nao vale como sessao;
  F3  cookie de sessao com Secure fora de dev (falha fechado) e HttpOnly sempre;
  F4  POST /api/auth/logout exige sessao;
  F5  o form de login nunca pode fazer submit padrao com a senha;
  F6  no-store em tudo que nao e /static + CSP com frame-ancestors 'none'.

Rodar:  python tests/test_auth_hardening.py
   ou:  python -m pytest tests/test_auth_hardening.py
"""
import json
import os
import pathlib
import subprocess
import sys
from datetime import timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ADMIN_EMAIL = "admin@local.test"
ADMIN_PASSWORD = "LocalSmoke123!"
SECRET = "test-secret-key-audit-2026-08-w1a"

os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": "sqlite:///./scratch/auth_hardening_test.db",
    "SEED_INITIAL_ADMIN": "true",
    "ADMIN_INITIAL_EMAIL": ADMIN_EMAIL,
    "ADMIN_INITIAL_PASSWORD": ADMIN_PASSWORD,
    "SECRET_KEY": SECRET,
})
pathlib.Path("scratch").mkdir(exist_ok=True)
_DB = pathlib.Path("scratch/auth_hardening_test.db")
if _DB.exists():
    _DB.unlink()

from fastapi.testclient import TestClient  # noqa: E402  (requer httpx)

from app.auth import create_access_token, decode_token  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
with TestClient(app):  # dispara o lifespan (create_all + seed)
    pass

PROTECTED = "/hub"


def _session_cookie(token: str):
    client.cookies.clear()
    client.cookies.set("access_token", f"Bearer {token}")


def _valid_token(minutes: int = 60) -> str:
    return create_access_token({"sub": ADMIN_EMAIL, "role": "admin"},
                               expires_delta=timedelta(minutes=minutes))


# ─── F1. Bearer obsoleto NAO pode mais derrubar um cookie valido ─────
# ANTES: `get_current_user` fazia `raise credentials_exception` quando o header
# Bearer nao resolvia, sem NUNCA chegar ao ramo do cookie. Como o auth.js anexa
# o token do localStorage a toda request, um unico valor obsoleto ali 401ava a
# API inteira e o front derrubava a sessao no meio do uso.

def test_garbage_bearer_does_not_shadow_valid_cookie():
    _session_cookie(_valid_token())
    r = client.get("/api/auth/me",
                   headers={"Authorization": "Bearer lixo-do-localStorage"})
    assert r.status_code == 200, (
        "cookie valido tem de vencer um header Bearer obsoleto: "
        f"{r.status_code} {r.text[:200]}"
    )
    assert r.json()["email"] == ADMIN_EMAIL


def test_garbage_bearer_alone_still_401():
    """O fallthrough nao pode virar permissividade: sem cookie, continua 401."""
    client.cookies.clear()
    r = client.get("/api/auth/me",
                   headers={"Authorization": "Bearer lixo-do-localStorage"})
    assert r.status_code == 401, f"credencial invalida sozinha deve falhar: {r.status_code}"


def test_valid_bearer_without_cookie_still_works():
    client.cookies.clear()
    r = client.get("/api/auth/me",
                   headers={"Authorization": f"Bearer {_valid_token()}"})
    assert r.status_code == 200, f"o header Bearer valido continua valendo: {r.status_code}"


# ─── F2. Token de outro proposito nao e sessao ───────────────────────
# `app/routers/users.py:124` emite create_access_token({"sub": ..,
# "type": "verify_email"}) e o entrega na QUERY STRING de um link. Sem checagem
# de `typ`, esse token era uma sessao completa de 8h do CRM.

def _verify_email_token() -> str:
    return create_access_token(data={"sub": ADMIN_EMAIL, "type": "verify_email"})


def test_verify_email_token_has_no_typ_claim():
    payload = decode_token(_verify_email_token())
    assert payload is not None, "o token de verificacao continua assinado/valido"
    assert "typ" not in payload, (
        "o token de verificacao NAO pode ganhar `typ: access` — e isso que o "
        f"mantem recusado como sessao: {payload}"
    )


def test_verify_email_token_is_rejected_as_session():
    token = _verify_email_token()
    _session_cookie(token)
    me = client.get("/api/auth/me", follow_redirects=False).status_code
    _session_cookie(token)
    hub = client.get(PROTECTED, follow_redirects=False).status_code
    assert me == 401, f"/api/auth/me aceitou um token verify_email como sessao: {me}"
    assert hub == 302, f"/hub aceitou um token verify_email como sessao: {hub}"

    client.cookies.clear()
    bearer = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert bearer.status_code == 401, (
        f"o mesmo token passou pelo header Authorization: {bearer.status_code}"
    )


def test_session_token_carries_typ_access():
    payload = decode_token(_valid_token())
    assert payload.get("typ") == "access", f"token de sessao sem `typ`: {payload}"


# ─── Sem regressao: o login normal continua funcionando ──────────────

def test_normal_login_flow_still_works_end_to_end():
    client.cookies.clear()
    r = client.post("/api/auth/login",
                    json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, f"login valido deveria passar: {r.status_code} {r.text[:200]}"
    assert client.cookies.get("access_token"), "login precisa instalar o cookie de sessao"

    assert client.get(PROTECTED, follow_redirects=False).status_code == 200
    me = client.get("/api/auth/me", follow_redirects=False)
    assert me.status_code == 200 and me.json()["email"] == ADMIN_EMAIL

    # O token do corpo (o que vai para o localStorage) tambem continua servindo.
    body_token = r.json()["access_token"]
    client.cookies.clear()
    assert client.get("/api/auth/me",
                      headers={"Authorization": f"Bearer {body_token}"}).status_code == 200


# ─── F3. Flags do cookie de sessao ───────────────────────────────────
# ANTES: `os.getenv("ENVIRONMENT") == "production"`. Um valor tipografico como
# "Production"/"prod" mandava o cookie de sessao SEM Secure. O teste usa
# justamente "Production": falha com o codigo antigo e passa com o novo
# (`secure = ENVIRONMENT != "development"`).

_PROD_PROBE = """
import pathlib, sys
sys.path.insert(0, ".")
pathlib.Path("scratch").mkdir(exist_ok=True)
from fastapi.testclient import TestClient
from app.main import app
with TestClient(app) as c:
    r = c.post("/api/auth/login", json={"email": %r, "password": %r})
    print("STATUS", r.status_code)
    for v in r.headers.get_list("set-cookie"):
        print("SETCOOKIE", v)
""" % (ADMIN_EMAIL, ADMIN_PASSWORD)


def _login_set_cookie_in(environment: str) -> str:
    """Sobe o app num subprocesso com ENVIRONMENT=<valor> e devolve o Set-Cookie.

    Subprocesso porque ENVIRONMENT e lido uma unica vez, no import de
    `app.config` — nao da para trocar dentro do processo ja carregado.
    """
    db = pathlib.Path(f"scratch/auth_hardening_{environment.lower()}.db")
    if db.exists():
        db.unlink()
    env = dict(os.environ,
               ENVIRONMENT=environment,
               DATABASE_URL=f"sqlite:///./{db.as_posix()}",
               SECRET_KEY=SECRET,
               SEED_INITIAL_ADMIN="true",
               ADMIN_INITIAL_EMAIL=ADMIN_EMAIL,
               ADMIN_INITIAL_PASSWORD=ADMIN_PASSWORD)
    p = subprocess.run([sys.executable, "-c", _PROD_PROBE],
                       capture_output=True, text=True, cwd=str(ROOT), env=env)
    assert p.returncode == 0, f"subprocesso falhou: {p.stderr.strip()[-800:]}"
    assert "STATUS 200" in p.stdout, f"login no subprocesso falhou: {p.stdout!r}"
    cookies = [ln[len("SETCOOKIE "):] for ln in p.stdout.splitlines()
               if ln.startswith("SETCOOKIE ") and "access_token=" in ln]
    assert cookies, f"nenhum Set-Cookie de sessao: {p.stdout!r}"
    return cookies[0]


def test_session_cookie_is_secure_outside_development():
    raw = _login_set_cookie_in("Production")
    assert "Secure" in raw, (
        "ENVIRONMENT nao-canonico ('Production') tem de continuar marcando "
        f"Secure — a flag falha fechado: {raw}"
    )
    assert "HttpOnly" in raw, f"cookie de sessao sem HttpOnly: {raw}"


def test_session_cookie_is_httponly_in_development():
    client.cookies.clear()
    r = client.post("/api/auth/login",
                    json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    raw = [v for v in r.headers.get_list("set-cookie") if "access_token=" in v][0]
    assert "HttpOnly" in raw, f"HttpOnly nao pode depender do ambiente: {raw}"
    assert "Secure" not in raw, (
        "em development o cookie precisa trafegar em http local: " + raw
    )
    # max_age derivado de ACCESS_TOKEN_EXPIRE_MINUTES, nao mais cravado em 28800.
    from app.config import ACCESS_TOKEN_EXPIRE_MINUTES
    assert f"Max-Age={ACCESS_TOKEN_EXPIRE_MINUTES * 60}" in raw, (
        f"max_age nao acompanha o tempo de vida do JWT: {raw}"
    )


# ─── F4. Logout exige sessao ─────────────────────────────────────────

def test_logout_without_credentials_is_rejected():
    client.cookies.clear()
    r = client.post("/api/auth/logout")
    assert r.status_code == 401, (
        f"logout anonimo tem de ser recusado (era 200, sem atribuicao): {r.status_code}"
    )


def test_logout_with_session_still_works():
    client.cookies.clear()
    assert client.post("/api/auth/login",
                       json={"email": ADMIN_EMAIL,
                             "password": ADMIN_PASSWORD}).status_code == 200
    r = client.post("/api/auth/logout")
    assert r.status_code == 200, f"logout autenticado deveria passar: {r.status_code}"
    assert not client.cookies.get("access_token"), "logout precisa remover o cookie"


def test_auth_js_logout_checks_response_status():
    js = (ROOT / "static/js/auth.js").read_text(encoding="utf-8")
    block = js.split("async logout()")[1].split("requireAuth()")[0]
    assert "response.ok" in block, (
        "fetch nao rejeita em 4xx/5xx — sem checar `response.ok` um logout "
        "recusado pelo servidor era reportado como sucesso"
    )


# ─── F6. Cache e CSP ─────────────────────────────────────────────────

def test_every_non_static_response_is_no_store():
    _session_cookie(_valid_token())
    for path in ("/login", "/hub", "/api/auth/me", "/leads"):
        r = client.get(path, follow_redirects=False)
        assert "no-store" in r.headers.get("cache-control", ""), (
            f"{path} sem no-store: {r.headers.get('cache-control')!r}"
        )
    client.cookies.clear()
    r = client.get(PROTECTED, follow_redirects=False)
    assert "no-store" in r.headers.get("cache-control", ""), \
        "o 302 para /login tambem nao pode ser cacheado"


def test_static_assets_stay_cacheable():
    r = client.get("/static/js/auth.js")
    assert r.status_code == 200, f"asset nao servido: {r.status_code}"
    assert "no-store" not in r.headers.get("cache-control", ""), \
        "assets de /static nao carregam estado de sessao e seguem cacheaveis"


def test_every_response_has_csp():
    _session_cookie(_valid_token())
    for path in ("/login", "/hub", "/api/auth/me", "/static/js/auth.js"):
        csp = client.get(path, follow_redirects=False).headers.get(
            "content-security-policy", "")
        assert "frame-ancestors 'none'" in csp, f"{path}: CSP ausente/fraca: {csp!r}"
        assert "object-src 'none'" in csp, f"{path}: object-src: {csp!r}"
        assert "base-uri 'self'" in csp, f"{path}: base-uri: {csp!r}"


def test_csp_allows_what_the_app_actually_loads():
    """A CSP nao pode quebrar o app: jsdelivr (chart.js/marked/fullcalendar),
    Google Fonts e o inline dos templates continuam permitidos."""
    csp = client.get("/login", follow_redirects=False).headers.get(
        "content-security-policy", "")
    directives = {d.strip().split(" ")[0]: d.strip() for d in csp.split(";") if d.strip()}
    script = directives.get("script-src", "")
    style = directives.get("style-src", "")
    assert "https://cdn.jsdelivr.net" in script and "'unsafe-inline'" in script,         f"script-src quebraria chart.js/marked/fullcalendar e o inline: {script!r}"
    assert "https://fonts.googleapis.com" in style and "'unsafe-inline'" in style,         f"style-src quebraria Google Fonts e o style= inline: {style!r}"
    assert "https://fonts.gstatic.com" in directives.get("font-src", ""),         f"font-src sem o host das fontes: {csp!r}"


# ─── F5. O form de login nunca faz submit padrao com a senha ─────────

def test_login_form_cannot_leak_password_via_default_submit():
    html = (ROOT / "templates/login.html").read_text(encoding="utf-8")
    form = html.split('<form id="loginForm"')[1].split(">")[0]
    assert 'method="post"' in form, (
        f"sem method=post o submit padrao vira GET com a senha na URL: <form{form}>"
    )
    assert "onsubmit=" in form and "return false" in form, (
        f"o submit nativo precisa ser bloqueado no proprio form: <form{form}>"
    )

    pwd = html.split('type="password"')[1].split(">")[0]
    assert "name=" not in pwd, (
        f"input de senha com `name` continua serializavel em um submit: {pwd}"
    )
    assert 'id="password"' in pwd, "login.js resolve o campo por id — nao pode sumir"


def test_login_js_wires_the_form_on_every_path():
    js = (ROOT / "static/js/login.js").read_text(encoding="utf-8")
    assert "function wireLoginForm()" in js, "a ligacao do form precisa ser reutilizavel"
    head = js.split("document.addEventListener")[1].split("function wireLoginForm")[0]
    assert head.count("Auth.clearAuth();") == 3, \
        "os 3 desfechos de falha do bloco autenticado mudaram — revisar a ligacao"
    assert head.count("wireLoginForm();") == 4, (
        "todo caminho que deixa o usuario no /login tem de ligar o form: "
        f"{head.count('wireLoginForm();')} chamadas para 4 caminhos"
    )


def test_login_js_form_handler_still_prevents_default():
    js = (ROOT / "static/js/login.js").read_text(encoding="utf-8")
    assert "function wireLoginForm()" in js, "a ligacao do form precisa ser reutilizavel"
    body = js.split("function wireLoginForm()")[1]
    assert "form.addEventListener('submit'" in body
    assert "e.preventDefault();" in body, "o listener continua barrando o submit nativo"
    assert "/api/auth/login" in body, "o login real continua sendo por fetch"


# ─── Comportamental: roda o login.js no node com DOM stubado ─────────
# Mesmo harness dos testes de JS ja existentes no repo. Aqui o DOM TEM o form:
# prova que a ligacao acontece mesmo quando a checagem de sessao falha.
# O `eval` abaixo carrega static/js/login.js (arquivo do proprio repo, nao
# entrada externa) num processo node descartavel — e a unica forma de rodar um
# script de pagina sem bundler; o cenario chega por JSON.parse de argv.

_HARNESS = r"""
const fs = require('fs');
const scen = JSON.parse(process.argv[1]);
const nav = [];
const listeners = [];
const local = scen.token ? { crm_access_token: 'jwt' } : {};
const session = scen.hop ? { crm_hub_hop: '1' } : {};
const mk = o => ({
  getItem: k => (k in o ? o[k] : null),
  setItem: (k, v) => { o[k] = String(v); },
  removeItem: k => { delete o[k]; },
});
global.localStorage = mk(local);
global.sessionStorage = mk(session);
global.Auth = {
  HOP_KEY: 'crm_hub_hop',
  getToken: () => global.localStorage.getItem('crm_access_token'),
  isAuthenticated() { return !!this.getToken(); },
  clearAuth() { delete local.crm_access_token; delete session.crm_hub_hop; },
};
global.window = { location: { search: '', origin: 'https://crm.test' } };
Object.defineProperty(global.window.location, 'href', {
  get: () => '', set: v => { nav.push(v); },
});
global.fetch = () => Promise.resolve({ ok: scen.me === 200, status: scen.me });
let handler = null;
const el = id => ({
  id,
  value: '',
  type: id === 'password' ? 'password' : 'text',
  classList: { toggle: () => {}, remove: () => {}, add: () => {} },
  addEventListener: (ev) => { listeners.push(id + ':' + ev); },
  focus: () => {},
});
global.document = {
  addEventListener: (ev, cb) => { if (ev === 'DOMContentLoaded') handler = cb; },
  getElementById: el,
};
eval(fs.readFileSync('static/js/login.js', 'utf8'));
handler();
setTimeout(() => {
  console.log(JSON.stringify({ nav, listeners }));
}, 0);
"""


def _run_login_js(**scen):
    import shutil
    node = shutil.which("node")
    if node is None:
        return None
    p = subprocess.run([node, "-e", _HARNESS, "--", json.dumps(scen)],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert p.returncode == 0, f"node falhou: {p.stderr.strip()[:600]}"
    return json.loads(p.stdout)


def test_login_js_wires_submit_when_session_check_fails():
    """O caminho exato do F5: sessao invalida -> antes, form sem listener."""
    r = _run_login_js(token=True, hop=False, me=401)
    if r is None:
        return  # node ausente: os greps acima ja cobrem a estrutura
    assert r["nav"] == [], f"nao pode navegar com sessao invalida: {r['nav']}"
    assert "loginForm:submit" in r["listeners"], (
        "form ficou SEM listener de submit — Enter viraria submit padrao do "
        f"navegador, com a senha: {r['listeners']}"
    )


def test_login_js_wires_submit_on_the_hop_guard_path():
    r = _run_login_js(token=True, hop=True, me=200)
    if r is None:
        return
    assert r["nav"] == [], f"a segunda tentativa criaria o loop: {r['nav']}"
    assert "loginForm:submit" in r["listeners"], r["listeners"]


def test_login_js_wires_submit_when_not_authenticated():
    r = _run_login_js(token=False, hop=False, me=401)
    if r is None:
        return
    assert "loginForm:submit" in r["listeners"], r["listeners"]


def test_login_js_valid_session_still_redirects_once():
    """A guarda AUTH-LOOP-01 continua intacta."""
    r = _run_login_js(token=True, hop=False, me=200)
    if r is None:
        return
    assert r["nav"] == ["/hub"], f"sessao valida deveria ir ao hub: {r['nav']}"


TESTS = [v for k, v in globals().items() if k.startswith("test_")]

if __name__ == "__main__":
    failures = 0
    for fn in TESTS:
        try:
            fn()
            print(f"OK   {fn.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passaram")
    raise SystemExit(1 if failures else 0)
