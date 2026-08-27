"""
AUTH-LOOP-01 — regressao do loop infinito /login <-> /hub.

Incidente: o navegador entrou em loop entre /login e /hub ate tomar 429.
Causa: DUAS fontes de verdade de autenticacao. /api/auth/me validava o JWT do
localStorage (header `Authorization`) e /hub so checava a PRESENCA do cookie
`access_token`. Com localStorage valido + cookie ausente, /api/auth/me
respondia 200, /hub respondia 302 para /login, e o login.js tentava de novo,
sem fim, sem invalidar nada.

Este arquivo prova, no backend e no frontend:
  - /hub e /api/auth/me concordam sobre a MESMA credencial (o cookie);
  - cookie invalido/expirado e removido no 302 (recuperacao automatica);
  - o login.js valida com o cookie, nao com o localStorage;
  - existe uma guarda one-shot contra redirect repetido;
  - rate limit do login continua ativo.

Rodar:  python tests/test_auth_session_consistency.py
   ou:  python -m pytest tests/test_auth_session_consistency.py
"""
import os
import pathlib
import sys
from datetime import timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ADMIN_EMAIL = "admin@local.test"
ADMIN_PASSWORD = "LocalSmoke123!"

os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": "sqlite:///./scratch/auth_session_test.db",
    "SEED_INITIAL_ADMIN": "true",
    "ADMIN_INITIAL_EMAIL": ADMIN_EMAIL,
    "ADMIN_INITIAL_PASSWORD": ADMIN_PASSWORD,
    "SECRET_KEY": "test-secret-key-auth-loop-01",
})
pathlib.Path("scratch").mkdir(exist_ok=True)
_DB = pathlib.Path("scratch/auth_session_test.db")
if _DB.exists():
    _DB.unlink()

from fastapi.testclient import TestClient  # noqa: E402  (requer httpx)

from app.auth import create_access_token  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models.user import User  # noqa: E402

client = TestClient(app)
with TestClient(app):  # dispara o lifespan (create_all + seed)
    pass

PROTECTED = "/hub"


def _cookie(token: str):
    """Instala um cookie de sessao no client, zerando o estado anterior."""
    client.cookies.clear()
    client.cookies.set("access_token", token)


def _login():
    """Login real: o cookie tem de vir do backend para que o Set-Cookie de
    remocao do logout case com ele no jar do httpx."""
    client.cookies.clear()
    r = client.post("/api/auth/login",
                    json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, f"login valido deveria passar: {r.status_code} {r.text[:200]}"
    return r


def _valid_token(minutes: int = 60, email: str = ADMIN_EMAIL) -> str:
    return create_access_token({"sub": email, "role": "admin"},
                               expires_delta=timedelta(minutes=minutes))


def _set_active(email: str, active: bool):
    db = SessionLocal()
    try:
        db.query(User).filter(User.email == email).update({"is_active": active})
        db.commit()
    finally:
        db.close()


# ─── 1/2. Gate basico da pagina protegida ────────────────────────────

def test_unauthenticated_hub_redirects_to_login():
    client.cookies.clear()
    r = client.get(PROTECTED, follow_redirects=False)
    assert r.status_code == 302, f"sem sessao deveria redirecionar: {r.status_code}"
    assert "/login" in r.headers.get("location", "")


def test_authenticated_hub_returns_200():
    _cookie(f"Bearer {_valid_token()}")
    r = client.get(PROTECTED, follow_redirects=False)
    assert r.status_code == 200, f"sessao valida deveria servir o hub: {r.status_code}"
    assert "Hub de Setores" in r.text


# ─── 3/4. As duas rotas concordam sobre a mesma credencial ───────────

def _agree(token_value):
    """(status /api/auth/me, status /hub) para o MESMO cookie."""
    _cookie(token_value)
    me = client.get("/api/auth/me", follow_redirects=False).status_code
    _cookie(token_value)
    hub = client.get(PROTECTED, follow_redirects=False).status_code
    return me, hub


def test_valid_session_me_and_hub_agree():
    me, hub = _agree(f"Bearer {_valid_token()}")
    assert (me, hub) == (200, 200), f"sessao valida divergiu: me={me} hub={hub}"


def test_invalid_session_me_and_hub_agree():
    # ANTES da correcao: me=401 e hub=200 (o gate so olhava a presenca).
    me, hub = _agree("Bearer nao-e-um-jwt")
    assert me == 401, f"/api/auth/me deveria recusar cookie invalido: {me}"
    assert hub == 302, f"/hub deveria recusar cookie invalido: {hub}"


def test_expired_session_me_and_hub_agree():
    expired = f"Bearer {_valid_token(minutes=-5)}"
    me, hub = _agree(expired)
    assert me == 401, f"/api/auth/me deveria recusar sessao expirada: {me}"
    assert hub == 302, f"/hub deveria recusar sessao expirada: {hub}"


# ─── 5/6. Recuperacao automatica: a credencial ruim e removida ───────

def _deletes_cookie(response) -> bool:
    return any(
        "access_token=" in v and ('Max-Age=0' in v or 'expires=' in v.lower())
        for v in response.headers.get_list("set-cookie")
    )


def test_invalid_cookie_is_cleared_on_redirect():
    _cookie("Bearer nao-e-um-jwt")
    r = client.get(PROTECTED, follow_redirects=False)
    assert r.status_code == 302
    assert _deletes_cookie(r), (
        "cookie invalido precisa ser apagado no 302 — sem isso o estado ruim "
        f"persiste para sempre: {r.headers.get_list('set-cookie')}"
    )


def test_expired_cookie_is_cleared_on_redirect():
    _cookie(f"Bearer {_valid_token(minutes=-5)}")
    r = client.get(PROTECTED, follow_redirects=False)
    assert r.status_code == 302 and _deletes_cookie(r)


# ─── 7. A condicao exata do incidente ────────────────────────────────

def test_incident_state_no_cookie_but_valid_bearer_does_not_loop():
    """localStorage valido + cookie ausente = o estado do incidente.

    O login.js passou a validar SEM o header `Authorization` (cookie apenas),
    exatamente como /hub. Nesse estado a checagem do login retorna 401 -> o
    frontend limpa o estado e fica parado, em vez de navegar para /hub.
    """
    token = _valid_token()
    client.cookies.clear()

    # Como o login.js chamava ANTES (header do localStorage): 200 -> ia p/ /hub.
    old = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert old.status_code == 200, "pre-condicao: o JWT do localStorage e valido"

    # Como o login.js chama AGORA (mesma credencial que /hub exige): 401.
    client.cookies.clear()
    new = client.get("/api/auth/me", follow_redirects=False)
    assert new.status_code == 401, (
        f"sem cookie, a checagem do login tem de falhar como /hub falha: {new.status_code}"
    )

    # E /hub concorda: mesma resposta para o mesmo estado.
    client.cookies.clear()
    hub = client.get(PROTECTED, follow_redirects=False)
    assert hub.status_code == 302, f"/hub: {hub.status_code}"


# ─── 8/9/10. Login e logout de ponta a ponta ─────────────────────────

def test_valid_login_sets_cookie_and_reaches_hub():
    _login()
    assert client.cookies.get("access_token"), "login precisa instalar o cookie de sessao"
    assert client.get(PROTECTED, follow_redirects=False).status_code == 200


def test_logout_makes_session_unusable():
    _login()
    assert client.post("/api/auth/logout").status_code == 200
    assert not client.cookies.get("access_token"), "logout precisa remover o cookie"

    hub = client.get(PROTECTED, follow_redirects=False)
    assert hub.status_code == 302, f"apos logout /hub deve recusar: {hub.status_code}"
    me = client.get("/api/auth/me", follow_redirects=False)
    assert me.status_code == 401, f"apos logout /api/auth/me deve recusar: {me.status_code}"


# ─── 11. Sem cadeia ilimitada de redirects ───────────────────────────

def test_no_unbounded_redirect_chain():
    client.cookies.clear()
    r = client.get(PROTECTED, follow_redirects=True)
    assert r.status_code == 200, f"a cadeia deveria terminar no login: {r.status_code}"
    assert r.url.path == "/login", f"terminou em {r.url.path}"
    assert len(r.history) == 1, f"mais de um salto para estabilizar: {len(r.history)}"


# ─── AUDIT-2026-08-WG (F-495). O `?next=` vale para TODA pagina ──────

def test_todas_as_paginas_protegidas_preservam_o_next():
    """
    Das onze paginas protegidas, so `/gestao/pendencias` passava `next_url` a
    mao; as outras dez devolviam o operador ao /hub. Sessao expirada em
    /pipeline significava navegar de novo ate onde ele estava.

    O default certo mora em `page_login_redirect`, nao nos call sites — corrigir
    dez chamadas seria o mesmo defeito esperando para voltar na decima primeira.
    Este teste percorre as rotas de pagina REGISTRADAS no app: uma pagina nova
    que esqueca o `next` cai aqui sozinha.
    """
    from urllib.parse import parse_qs, urlparse

    from app.main import app as _app

    ignoradas = {"/", "/login"}
    rotas = sorted({
        r.path for r in _app.routes
        if getattr(r, "path", "").startswith("/")
        and "{" not in getattr(r, "path", "")
        and "GET" in getattr(r, "methods", set())
        and not r.path.startswith(("/api", "/static", "/docs", "/redoc", "/openapi"))
        and r.path not in ignoradas
    })
    assert len(rotas) >= 8, f"esperava a maioria das paginas protegidas, achei {rotas}"

    client.cookies.clear()
    sem_next = []
    for path in rotas:
        r = client.get(path, follow_redirects=False)
        if r.status_code != 302:
            continue                      # pagina publica: nao e assunto deste teste
        destino = r.headers.get("location", "")
        alvo = parse_qs(urlparse(destino).query).get("next", [None])[0]
        if alvo != path:
            sem_next.append(f"{path} -> {destino}")
    assert not sem_next, ("paginas que perdem o destino no redirect de login: "
                          + ", ".join(sem_next))


def test_next_nunca_carrega_query_string():
    """
    So o PATH entra no `next`. A query pode ter filtro ou id de cliente, e o
    `next` vai para a barra de enderecos, para o historico e para o log de
    acesso do proxy.
    """
    client.cookies.clear()
    r = client.get("/leads?open=123&termo=fulano", follow_redirects=False)
    assert r.status_code == 302, f"esperava redirect, veio {r.status_code}"
    destino = r.headers.get("location", "")
    assert "open=123" not in destino and "fulano" not in destino,         f"a query vazou para o next: {destino}"
    assert "next=/leads" in destino, f"o path deveria estar preservado: {destino}"


# ─── 14. Autorizacao nao afrouxou ────────────────────────────────────

def test_inactive_user_gets_no_page_access():
    _set_active(ADMIN_EMAIL, False)
    try:
        me, hub = _agree(f"Bearer {_valid_token()}")
        assert me == 401, f"usuario inativo nao pode passar em /api/auth/me: {me}"
        assert hub == 302, f"usuario inativo nao pode abrir /hub: {hub}"
    finally:
        _set_active(ADMIN_EMAIL, True)


# ─── 15/17. Cache: autenticacao nunca pode vir de resposta obsoleta ──

def test_auth_routes_are_no_store():
    _cookie(f"Bearer {_valid_token()}")
    for path in ("/login", "/hub", "/api/auth/me"):
        r = client.get(path, follow_redirects=False)
        assert "no-store" in r.headers.get("cache-control", ""), (
            f"{path} sem no-store: {r.headers.get('cache-control')!r}"
        )
    client.cookies.clear()
    r = client.get(PROTECTED, follow_redirects=False)
    assert "no-store" in r.headers.get("cache-control", ""), \
        "o 302 para /login tambem nao pode ser cacheado"


def test_no_service_worker_serving_auth():
    """Nao ha Service Worker no CRM — auth nunca e satisfeita por cache stale."""
    hits = []
    for folder in ("static", "templates", "app"):
        for f in (ROOT / folder).rglob("*"):
            if f.is_file() and f.suffix in (".js", ".html", ".py"):
                if "serviceWorker" in f.read_text(encoding="utf-8", errors="ignore"):
                    hits.append(str(f.relative_to(ROOT)))
    assert not hits, f"Service Worker registrado — reavaliar cache de auth: {hits}"


# ─── Frontend: o login valida a MESMA credencial que a pagina exige ──

def test_login_js_validates_with_the_session_cookie():
    js = (ROOT / "static/js/login.js").read_text(encoding="utf-8")
    check = js.split("document.addEventListener")[1].split("const form")[0]
    assert "credentials: 'same-origin'" in check, \
        "a checagem do login precisa mandar o cookie de sessao"
    assert "Authorization" not in check, (
        "a checagem do login nao pode usar o JWT do localStorage — foi essa "
        "divergencia que criou o loop /login <-> /hub"
    )


def test_login_js_has_one_shot_redirect_guard():
    js = (ROOT / "static/js/login.js").read_text(encoding="utf-8")
    auth = (ROOT / "static/js/auth.js").read_text(encoding="utf-8")
    assert "HOP_KEY" in auth and "sessionStorage.removeItem(this.HOP_KEY)" in auth, \
        "clearAuth/requireAuth precisam zerar a guarda"
    assert "sessionStorage.getItem(Auth.HOP_KEY)" in js, "falta a guarda one-shot"
    assert "sessionStorage.setItem(Auth.HOP_KEY" in js, "a guarda nunca e marcada"


def test_login_js_next_is_same_origin_only():
    js = (ROOT / "static/js/login.js").read_text(encoding="utf-8")
    assert "function safeNext()" in js
    assert js.count("safeNext() || '/hub'") == 2, \
        "os dois redirects do login precisam passar pelo safeNext()"
    assert "new URL(raw, window.location.origin)" in js
    assert "url.origin === window.location.origin" in js


# ─── Comportamental: roda o login.js no node com DOM stubado ─────────
# Mesmo caminho dos testes de JS ja existentes no repo (PRs #27/#29/#30).

_HARNESS = r"""
const fs = require('fs');
const scen = JSON.parse(process.argv[1]);
let cleared = 0;
const nav = [];
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
  clearAuth() { cleared++; delete local.crm_access_token; delete session.crm_hub_hop; },
};
global.window = { location: { search: scen.search || '', origin: 'https://crm.test' } };
Object.defineProperty(global.window.location, 'href', {
  get: () => '', set: v => { nav.push(v); },
});
global.fetch = () => Promise.resolve({ ok: scen.me === 200, status: scen.me });
let handler = null;
global.document = {
  addEventListener: (ev, cb) => { if (ev === 'DOMContentLoaded') handler = cb; },
  getElementById: () => null,
};
eval(fs.readFileSync('static/js/login.js', 'utf8'));
handler();
setTimeout(() => {
  console.log(JSON.stringify({ nav, cleared, hop: !!session.crm_hub_hop }));
}, 0);
"""


def _run_login_js(**scen):
    import json
    import shutil
    import subprocess
    node = shutil.which("node")
    if node is None:
        return None
    p = subprocess.run([node, "-e", _HARNESS, "--", json.dumps(scen)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(ROOT))
    assert p.returncode == 0, f"node falhou: {p.stderr.strip()[:400]}"
    return json.loads(p.stdout)


def test_login_js_incident_state_does_not_navigate():
    """Estado do incidente: localStorage cheio, sessao (cookie) invalida."""
    r = _run_login_js(token=True, hop=False, me=401)
    if r is None:
        return  # node ausente: os testes de grep ja cobrem a estrutura
    assert r["nav"] == [], f"nao pode navegar com sessao invalida: {r['nav']}"
    assert r["cleared"] == 1, "o estado local inconsistente tem de ser limpo"
    assert r["hop"] is False


def test_login_js_valid_session_goes_to_hub_once():
    r = _run_login_js(token=True, hop=False, me=200)
    if r is None:
        return
    assert r["nav"] == ["/hub"], f"sessao valida deveria ir ao hub: {r['nav']}"
    assert r["cleared"] == 0, "sessao valida nao pode ser limpa"
    assert r["hop"] is True, "a guarda one-shot precisa ser marcada"


def test_login_js_second_arrival_breaks_the_loop():
    """Voltou ao /login com a guarda marcada = backend e frontend discordam."""
    r = _run_login_js(token=True, hop=True, me=200)
    if r is None:
        return
    assert r["nav"] == [], f"a segunda tentativa criaria o loop: {r['nav']}"
    assert r["cleared"] == 1, "o estado local incoerente tem de ser limpo"


def test_login_js_external_next_is_ignored():
    r = _run_login_js(token=True, hop=False, me=200,
                      search="?next=https%3A%2F%2Fevil.com%2Fx")
    if r is None:
        return
    assert r["nav"] == ["/hub"], f"open redirect: {r['nav']}"


# ─── 12. Rate limit continua protegendo o login (por ultimo: consome) ─

def test_login_rate_limit_still_active():
    client.cookies.clear()
    codes = [
        client.post("/api/auth/login",
                    json={"email": "x@x", "password": "wrong"}).status_code
        for _ in range(6)
    ]
    assert 429 in codes, f"rate limit do login sumiu: {codes}"


# globals() preserva a ordem de definicao (3.7+): o teste de rate limit fica por
# ultimo, depois dos logins reais, como no pytest.
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
