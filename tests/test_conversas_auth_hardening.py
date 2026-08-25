"""
AUDIT-2026-08-W1B — Endurecimento de autenticacao/sessao do Conversas.

Cobre, comportamentalmente, as 8 falhas confirmadas na auditoria:

  F1  config.py NAO pode ter chave de assinatura hardcoded: fora de development
      e sem SECRET_KEY o import EXPLODE (o Conversas compartilha a chave e a
      tabela `users` com o CRM — a constante publica antiga tornava qualquer
      admin forjavel nos DOIS servicos).
  F2  pagina protegida com cookie GARBAGE responde 302 /login E APAGA o cookie
      (antes: qualquer valor renderizava o shell -> loop /login <-> /).
  F3  POST /api/auth/login grava o cookie no SERVIDOR, HttpOnly.
  F4  POST /api/auth/logout existe e expira o cookie.
  F5  GET /api/auth/me/validate exige credencial (401 sem, 200 com).
  F6  GET /api/auth/me devolve o usuario (antes: 500 sempre).
  F7  GET /api/config e restrito a admin e NAO devolve o verify token em claro.
  F8  toda resposta carrega X-Frame-Options: DENY + Content-Security-Policy.

Credenciais e tokens abaixo sao FIXTURES locais (nada de segredo real).
Roda standalone:  python tests/test_conversas_auth_hardening.py
"""
import hashlib
import os
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_auth_hardening_test.db"
if DB_FILE.exists():
    DB_FILE.unlink()

ADMIN_EMAIL = "admin.hardening@bna.local"
USER_EMAIL = "user.hardening@bna.local"
FIXTURE_PASS = "senha-de-teste-local"  # fixture, nao e segredo real
VERIFY_TOKEN_FIXTURE = "verify-token-fixture-nao-vazar"

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_FILE.as_posix()}"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["CONVERSAS_SEED_DEV_DATA"] = "false"
os.environ["META_APP_SECRET"] = ""
os.environ["N8N_AGENT_ENABLED"] = "false"

sys.path.insert(0, str(CONVERSAS_DIR))

# `Jinja2Templates(directory="templates")` e `StaticFiles(directory="static")` sao
# RELATIVOS ao cwd; em producao o Conversas roda de dentro de conversas/. Sem o
# chdir, o app serviria os templates/estaticos do CRM (que existem na raiz) — o
# teste passaria olhando para o app errado. Todos os paths daqui sao absolutos.
os.chdir(CONVERSAS_DIR)

from fastapi.testclient import TestClient  # noqa: E402
from jose import jwt  # noqa: E402

import app.main as main  # noqa: E402
import app.routers.auth as auth_router  # noqa: E402
from app.config import SECRET_KEY, ALGORITHM  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.auth import User  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


def token_for(email, minutes=60, **claims):
    """Token de SESSAO, montado como o servico monta.

    AUDIT-2026-08-orq: ganhou `typ: "access"` porque o Conversas passou a exigir
    esse claim (conversas/app/auth.py::_get_user_from_jwt), como o CRM ja exigia.
    O helper precisa produzir o que a aplicacao produz — senao o teste valida uma
    forma de token que nao existe. `claims` permite montar de proposito um token
    de OUTRO proposito, para os checks logo abaixo.
    """
    exp = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    corpo = {"sub": email, "typ": "access", "exp": exp}
    corpo.update(claims)
    return jwt.encode(corpo, SECRET_KEY, algorithm=ALGORITHM)


def set_cookie(client, value):
    """Zera o jar e planta exatamente um cookie de sessao."""
    client.cookies.clear()
    if value is not None:
        client.cookies.set("access_token", value)


def expired_cookie_header(response):
    """True se a resposta manda o navegador DESCARTAR o access_token."""
    for raw in response.headers.get_list("set-cookie"):
        if raw.startswith("access_token=") and ("Max-Age=0" in raw or "01 Jan 1970" in raw):
            return True
    return False


Base.metadata.create_all(bind=engine)

_s = SessionLocal()
_s.query(User).delete()
_s.add_all([
    User(nome="Admin", email=ADMIN_EMAIL, role="admin", is_active=True,
         hashed_password=hashlib.sha256(FIXTURE_PASS.encode()).hexdigest()),
    User(nome="Atendente", email=USER_EMAIL, role="user", is_active=True,
         hashed_password=hashlib.sha256(FIXTURE_PASS.encode()).hexdigest()),
])
_s.commit()
_s.close()

# follow_redirects=False: o 302 para /login E o comportamento sob teste.
client = TestClient(main.app, follow_redirects=False)
client.__enter__()

ADMIN_TOKEN = token_for(ADMIN_EMAIL)
USER_TOKEN = token_for(USER_EMAIL)


# ============ F1 — SECRET_KEY sem fallback publico ============
print("AUDIT-2026-08-W1B / F1 — SECRET_KEY nao tem fallback hardcoded")

_probe = (
    "import app.config as c; "
    "print('KEY=' + str(c.SECRET_KEY))"
)


def import_config(env_overrides):
    env = dict(os.environ)
    env.pop("SECRET_KEY", None)
    env["PYTHONPATH"] = str(CONVERSAS_DIR)
    env.update(env_overrides)
    # AUDIT-2026-08-orq: `text=True, encoding="utf-8", errors="replace"` sem `encoding` decodifica com o codec
    # PADRAO DA PLATAFORMA. No Windows isso e cp1252, e a mensagem de erro que
    # este proprio teste verifica tem cadeado e acentos: a decodificacao estoura
    # UnicodeDecodeError, `stderr` volta None e o check seguinte morre com
    # TypeError em vez de reprovar com mensagem. O teste passava no Linux do CI e
    # falhava na maquina de quem escreve o codigo — o pior lugar para falhar.
    return subprocess.run(
        [sys.executable, "-c", _probe],
        cwd=str(CONVERSAS_DIR), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


prod = import_config({"ENVIRONMENT": "production"})
check(prod.returncode != 0, "sem SECRET_KEY fora de development, o import FALHA")
check("SECRET_KEY" in prod.stderr, "a falha diz explicitamente qual variavel falta")

# ENVIRONMENT ausente = producao para efeito de segredo? Nao: o default do projeto
# e "development". O que NAO pode acontecer e um valor previsivel em prod.
staging = import_config({"ENVIRONMENT": "staging"})
check(staging.returncode != 0, "ambiente 'staging' tambem exige SECRET_KEY explicita")

dev1 = import_config({"ENVIRONMENT": "development"})
dev2 = import_config({"ENVIRONMENT": "development"})
check(dev1.returncode == 0 and dev2.returncode == 0, "em development o import segue funcionando")
check("dev-secret-key-change-me" not in dev1.stdout,
      "a constante publica antiga NAO e mais usada como chave")
check(dev1.stdout.strip() != dev2.stdout.strip(),
      "em development a chave e ALEATORIA por processo (dois imports diferem)")

src = (CONVERSAS_DIR / "app" / "config.py").read_text(encoding="utf-8")
check("dev-secret-key-change-me" not in src, "a constante sumiu do fonte")


# ============ F2 — gate das paginas valida o JWT de verdade ============
print("\nAUDIT-2026-08-W1B / F2 — cookie invalido nao renderiza o shell")

PAGES = ["/", "/templates", "/settings"]

for page in PAGES:
    set_cookie(client, "lixo-que-nao-e-jwt")
    r = client.get(page)
    check(r.status_code == 302, f"{page} com cookie GARBAGE -> 302 (got {r.status_code})")
    check(r.headers.get("location") == "/login", f"{page} redireciona para /login")
    check(expired_cookie_header(r), f"{page} APAGA o cookie invalido na resposta")
    check("<html" not in r.text.lower(), f"{page} nao vaza o shell HTML no 302")

# token expirado: assinatura valida, `exp` no passado — o caso real do loop
set_cookie(client, f"Bearer {token_for(USER_EMAIL, minutes=-5)}")
r_exp = client.get("/")
check(r_exp.status_code == 302 and expired_cookie_header(r_exp),
      "token EXPIRADO -> 302 + cookie apagado")

# usuario desativado nao entra por pagina (o JWT continua assinado e no prazo)
_s = SessionLocal()
_u = _s.query(User).filter(User.email == USER_EMAIL).first()
_u.is_active = False
_s.commit()
_s.close()
set_cookie(client, f"Bearer {USER_TOKEN}")
check(client.get("/").status_code == 302, "usuario is_active=False -> 302 (nao renderiza)")
_s = SessionLocal()
_u = _s.query(User).filter(User.email == USER_EMAIL).first()
_u.is_active = True
_s.commit()
_s.close()

set_cookie(client, None)
r_none = client.get("/")
check(r_none.status_code == 302, "sem cookie -> 302 /login")

# ============ F2b — token VALIDO renderiza ============
print("\nAUDIT-2026-08-W1B / F2 — sessao valida continua abrindo o app")
for page in PAGES:
    set_cookie(client, f"Bearer {USER_TOKEN}")
    r_ok = client.get(page)
    check(r_ok.status_code == 200, f"{page} com token VALIDO -> 200 (got {r_ok.status_code})")

# aceita o cookie sem o prefixo "Bearer " (mesmo formato de get_current_user)
set_cookie(client, USER_TOKEN)
check(client.get("/").status_code == 200, "cookie sem prefixo 'Bearer ' tambem vale")

set_cookie(client, None)
check(client.get("/login").status_code == 200, "/login segue publico")


# ============ F3 — cookie emitido pelo SERVIDOR, HttpOnly ============
print("\nAUDIT-2026-08-W1B / F3 — cookie de sessao vem do servidor")

_prev_flag = auth_router.CONVERSAS_SEED_DEV_DATA
auth_router.CONVERSAS_SEED_DEV_DATA = True  # usa o ramo de auth local
set_cookie(client, None)
r_login = client.post("/api/auth/login", json={"email": USER_EMAIL, "password": FIXTURE_PASS})
check(r_login.status_code == 200, f"login local 200 (got {r_login.status_code})")
_sc = [h for h in r_login.headers.get_list("set-cookie") if h.startswith("access_token=")]
check(bool(_sc), "login responde com Set-Cookie access_token")
check(bool(_sc) and "HttpOnly" in _sc[0], "cookie e HttpOnly (JS nao le)")
check(bool(_sc) and "Path=/" in _sc[0], "cookie vale para todo o site")
check(bool(_sc) and "lax" in _sc[0].lower(), "cookie e SameSite=Lax")

login_html = (CONVERSAS_DIR / "templates" / "login.html").read_text(encoding="utf-8")
check("document.cookie = `access_token" not in login_html
      and "document.cookie = 'access_token" not in login_html,
      "login.html NAO escreve mais o cookie de sessao via JS")

# o cookie emitido pelo servidor abre a pagina protegida
check(client.get("/").status_code == 200, "cookie do servidor da acesso ao shell")


# ============ F4 — logout no servidor apaga o cookie ============
print("\nAUDIT-2026-08-W1B / F4 — logout server-side")
r_logout = client.post("/api/auth/logout")
check(r_logout.status_code == 200, f"POST /api/auth/logout existe (got {r_logout.status_code})")
check(expired_cookie_header(r_logout), "logout expira o cookie access_token")
check(client.get("/").status_code == 302, "apos o logout a pagina protegida volta a 302")

auth_js = (CONVERSAS_DIR / "static" / "js" / "auth.js").read_text(encoding="utf-8")
check("/api/auth/logout" in auth_js, "auth.js::logout() chama a rota de logout")
check("response.ok" in auth_js, "auth.js::logout() confere response.ok antes de redirecionar")
auth_router.CONVERSAS_SEED_DEV_DATA = _prev_flag


# ============ F5/F6 — /me e /me/validate ============
print("\nAUDIT-2026-08-W1B / F5+F6 — rotas de identidade validam de verdade")
set_cookie(client, None)
check(client.get("/api/auth/me/validate").status_code == 401,
      "/api/auth/me/validate SEM credencial -> 401")
check(client.get("/api/auth/me/validate",
                 headers={"Authorization": "Bearer lixo"}).status_code == 401,
      "/api/auth/me/validate com token invalido -> 401")
r_val = client.get("/api/auth/me/validate", headers={"Authorization": f"Bearer {USER_TOKEN}"})
check(r_val.status_code == 200, f"/api/auth/me/validate com token valido -> 200 (got {r_val.status_code})")
check(r_val.json().get("email") == USER_EMAIL, "/me/validate devolve a identidade resolvida")

set_cookie(client, f"Bearer {USER_TOKEN}")
check(client.get("/api/auth/me/validate").status_code == 200, "/me/validate aceita o cookie")
set_cookie(client, None)

check(client.get("/api/auth/me").status_code == 401, "/api/auth/me SEM credencial -> 401")
r_me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {USER_TOKEN}"})
check(r_me.status_code == 200, f"/api/auth/me com token valido -> 200 (got {r_me.status_code})")
check(r_me.json().get("email") == USER_EMAIL, "/api/auth/me devolve o usuario correto")
check("hashed_password" not in r_me.text, "/api/auth/me nao vaza o hash da senha")


# ============ F7 — /api/config e admin-only e mascara o verify token ============
print("\nAUDIT-2026-08-W1B / F7 — verify token nunca sai em claro")
admin_h = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
user_h = {"Authorization": f"Bearer {USER_TOKEN}"}

r_put = client.put("/api/config", headers=admin_h,
                   json={"meta_verify_token": VERIFY_TOKEN_FIXTURE,
                         "meta_access_token": "access-token-fixture"})
check(r_put.status_code == 200, f"admin grava a config (got {r_put.status_code})")
check(VERIFY_TOKEN_FIXTURE not in r_put.text, "resposta do PUT nao ecoa o verify token")

check(client.get("/api/config").status_code == 401, "GET /api/config sem credencial -> 401")
check(client.get("/api/config", headers=user_h).status_code == 403,
      "GET /api/config como NAO-admin -> 403")

r_cfg = client.get("/api/config", headers=admin_h)
check(r_cfg.status_code == 200, f"GET /api/config como admin -> 200 (got {r_cfg.status_code})")
body = r_cfg.json()
check(VERIFY_TOKEN_FIXTURE not in r_cfg.text, "verify token NAO aparece em claro na resposta")
check("meta_verify_token" not in body, "campo meta_verify_token removido do schema")
check(body.get("has_verify_token") is True, "has_verify_token expoe apenas a PRESENCA")
check(body.get("has_access_token") is True, "has_access_token preservado")
check("access-token-fixture" not in r_cfg.text, "access token segue mascarado")

# o valor continua gravavel (write-only, como senha)
_s = SessionLocal()
from app.models.api_config import ApiConfig  # noqa: E402
_cfg_row = _s.query(ApiConfig).filter(ApiConfig.id == 1).first()
check(_cfg_row is not None and _cfg_row.meta_verify_token == VERIFY_TOKEN_FIXTURE,
      "o verify token FOI persistido (mascarado na leitura, nao perdido)")
_s.close()


# ============ F8 — headers de seguranca em TODA resposta ============
print("\nAUDIT-2026-08-W1B / F8 — headers de seguranca")
set_cookie(client, None)
amostra = {
    "GET /login": client.get("/login"),
    "GET /api/health": client.get("/api/health"),
    "GET / (302)": client.get("/"),
    "GET /api/config (401)": client.get("/api/config"),
    "GET /static/js/auth.js": client.get("/static/js/auth.js"),
}
for nome, resp in amostra.items():
    check(resp.headers.get("X-Frame-Options") == "DENY", f"{nome} -> X-Frame-Options: DENY")
    csp = resp.headers.get("Content-Security-Policy", "")
    check(bool(csp), f"{nome} -> tem Content-Security-Policy")
    check("frame-ancestors 'none'" in csp, f"{nome} -> CSP proibe enquadramento")
    check("object-src 'none'" in csp and "base-uri 'self'" in csp,
          f"{nome} -> CSP tem object-src/base-uri")
    check(resp.headers.get("X-Content-Type-Options") == "nosniff", f"{nome} -> nosniff")
    check(bool(resp.headers.get("Referrer-Policy")), f"{nome} -> Referrer-Policy")

check(amostra["GET /login"].headers.get("Cache-Control") == "no-store",
      "/login e no-store (resposta depende da sessao)")
check(amostra["GET / (302)"].headers.get("Cache-Control") == "no-store",
      "302 para /login e no-store")
check(client.post("/api/auth/logout").headers.get("Cache-Control") == "no-store",
      "/api/auth/* e no-store")

# CORS: curinga com credenciais nunca mais
check("*" not in main._allowed_origins,
      "CORS nao usa '*' (incompativel com allow_credentials=True)")
r_cors = client.get("/api/health", headers={"Origin": "https://site-malicioso.example"})
check(r_cors.headers.get("access-control-allow-origin") is None,
      "Origin desconhecida NAO e ecoada em Access-Control-Allow-Origin")


# ============ PROPOSITO DO TOKEN ============
# AUDIT-2026-08-orq: os DOIS servicos assinam com a MESMA SECRET_KEY, e o CRM
# emite um token de VERIFICACAO DE E-MAIL (app/routers/users.py) que viaja na
# QUERY STRING de um link — logo, vaza para log de acesso, historico e Referer.
# W1-A fez o CRM recusar qualquer token sem `typ: "access"`. O Conversas nao
# checava proposito nenhum: assinatura valida + `sub` bastava. Enquanto isso
# valeu, o link de verificacao de e-mail do CRM ERA uma sessao valida do
# Conversas — o inbox de WhatsApp inteiro. Estes checks travam as duas pontas.
print()
print("AUDIT-2026-08-orq — so token de SESSAO abre sessao")

_alvo = "/api/conversations"

_r_ok = client.get(_alvo, headers={"Authorization": f"Bearer {token_for(ADMIN_EMAIL)}"})
check(_r_ok.status_code == 200,
      f"token de sessao (typ=access) continua entrando (veio {_r_ok.status_code})")

# Sem `typ` — a forma exata que o Conversas emitia antes desta correcao.
_sem_typ = jwt.encode(
    {"sub": ADMIN_EMAIL, "exp": datetime.now(timezone.utc) + timedelta(minutes=60)},
    SECRET_KEY, algorithm=ALGORITHM)
_r1 = client.get(_alvo, headers={"Authorization": f"Bearer {_sem_typ}"})
check(_r1.status_code == 401,
      f"token SEM `typ` e recusado (veio {_r1.status_code})")

# O token de verificacao de e-mail do CRM, na forma exata que ele tem.
_verify = jwt.encode(
    {"sub": ADMIN_EMAIL, "type": "verify_email",
     "exp": datetime.now(timezone.utc) + timedelta(minutes=60)},
    SECRET_KEY, algorithm=ALGORITHM)
_r2 = client.get(_alvo, headers={"Authorization": f"Bearer {_verify}"})
check(_r2.status_code == 401,
      f"token de verificacao de e-mail do CRM NAO abre sessao aqui (veio {_r2.status_code})")

# Qualquer outro proposito tambem nao.
_r3 = client.get(_alvo, headers={"Authorization": f"Bearer {token_for(ADMIN_EMAIL, typ='refresh')}"})
check(_r3.status_code == 401,
      f"token com `typ` diferente de access e recusado (veio {_r3.status_code})")

# E o que o proprio servico emite tem que passar — senao o login quebra.
_emitido = auth_router._create_token(ADMIN_EMAIL)
import jose.jwt as _jj
check(_jj.get_unverified_claims(_emitido).get("typ") == "access",
      "o token que o proprio Conversas emite carrega typ=access")
_r4 = client.get(_alvo, headers={"Authorization": f"Bearer {_emitido}"})
check(_r4.status_code == 200,
      f"o token emitido pelo proprio servico entra (veio {_r4.status_code})")


# --- Resultado ---
client.__exit__(None, None, None)
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS CHECKS DE ENDURECIMENTO DE AUTH PASSARAM")
