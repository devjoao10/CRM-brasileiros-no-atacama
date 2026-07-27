"""
CONV-VAR-01-HOTFIX-ADMIN-01 — reconhecimento do papel ADMIN no Conversas.

Bug de producao: usuario com `role = ADMIN` no PostgreSQL recebia 403 em
POST /api/variables.

Causa raiz: o CRM declara `users.role` como `SAEnum(UserRole)` e o SQLAlchemy
grava na coluna o NOME do membro ("ADMIN"), nao o `value` ("admin"). O
Conversas espelha a MESMA tabela declarando `role = Column(String(20))`,
entao le a string CRUA "ADMIN" — e a comparacao literal `role != "admin"`
negava acesso a administradores reais.

Esta suite exercita o `require_admin` REAL (so `get_current_user` e
substituido, para injetar usuarios com formatos diferentes de `role`).

Roda standalone:  python tests/test_conversas_admin_role.py
"""
import enum
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
SCRATCH = ROOT / "scratch"
SCRATCH.mkdir(exist_ok=True)
DB_FILE = SCRATCH / "conv_admin_role_test.db"
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
from app.auth import get_current_user, is_admin_role  # noqa: E402
from app.database import engine, Base  # noqa: E402

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


Base.metadata.create_all(bind=engine)


# ─── Formatos possiveis de `role` ─────────────────────────────────────
class _StrEnumUpper(str, enum.Enum):
    """Enum str cujo value ja vem em MAIUSCULAS."""
    ADMIN = "ADMIN"
    USER = "USER"


class _StrEnumLower(str, enum.Enum):
    """Formato real do CRM: `class UserRole(str, enum.Enum): ADMIN = 'admin'`."""
    ADMIN = "admin"
    USER = "user"


class _PlainEnum(enum.Enum):
    """
    Enum COMUM (nao str). `str(_PlainEnum.ADMIN)` seria '_PlainEnum.ADMIN' —
    so passa se a normalizacao extrair `.value` ANTES de comparar.
    """
    ADMIN = "ADMIN"
    USER = "USER"


class _User:
    def __init__(self, role, uid=1):
        self.id = uid
        self.nome = "Teste"
        self.email = "teste@bna.local"
        self.role = role
        self.is_active = True


CURRENT = {"user": _User("admin")}
main.app.dependency_overrides[get_current_user] = lambda: CURRENT["user"]
client = TestClient(main.app)


def _as(role):
    CURRENT["user"] = _User(role)


# ============ 1. Normalizacao central (unitario) ============
print("ADMIN — normalizacao central is_admin_role()")

check(is_admin_role("ADMIN") is True, "string 'ADMIN' -> admin")
check(is_admin_role("admin") is True, "string 'admin' -> admin")
check(is_admin_role("Admin") is True, "string 'Admin' -> admin")
check(is_admin_role("  ADMIN  ") is True, "string com espacos -> admin")
check(is_admin_role(_StrEnumUpper.ADMIN) is True, "enum str com value 'ADMIN' -> admin")
check(is_admin_role(_StrEnumLower.ADMIN) is True, "enum str com value 'admin' -> admin")
check(is_admin_role(_PlainEnum.ADMIN) is True, "enum comum com value 'ADMIN' -> admin (usa .value)")

# NAO amplia para nenhum outro papel
for nao_admin in ("user", "USER", "manager", "MANAGER", "seller", "SELLER",
                  "administrador", "admin_readonly", "superadmin", "", "   ", None):
    check(is_admin_role(nao_admin) is False, f"papel {nao_admin!r} NAO e admin")
check(is_admin_role(_StrEnumUpper.USER) is False, "enum USER nao e admin")
check(is_admin_role(_PlainEnum.USER) is False, "enum comum USER nao e admin")


# ============ 2. HTTP — CRUD completo por formato de role ============
print("\nADMIN — CRUD de variaveis com cada formato de role")


def _ciclo_crud(rotulo, role, token):
    """POST -> PUT -> DELETE como administrador."""
    _as(role)
    r = client.post("/api/variables", json={
        "token": token, "name": "Teste", "kind": "fixed", "fixed_value": "valor"})
    check(r.status_code == 201, f"{rotulo}: POST /api/variables -> 201 (got {r.status_code})")
    if r.status_code != 201:
        return
    vid = r.json()["id"]
    r = client.put(f"/api/variables/{vid}", json={"fixed_value": "outro valor"})
    check(r.status_code == 200, f"{rotulo}: PUT /api/variables/{{id}} -> 200 (got {r.status_code})")
    r = client.delete(f"/api/variables/{vid}")
    check(r.status_code == 200, f"{rotulo}: DELETE /api/variables/{{id}} -> 200 (got {r.status_code})")


# O formato REAL de producao: string crua "ADMIN" lida da coluna espelhada.
_ciclo_crud("role string 'ADMIN' (producao)", "ADMIN", "@HOTFIXUPPER")
_ciclo_crud("role string 'admin'", "admin", "@HOTFIXLOWER")
_ciclo_crud("role enum value 'ADMIN'", _StrEnumUpper.ADMIN, "@HOTFIXENUMUP")
_ciclo_crud("role enum value 'admin'", _StrEnumLower.ADMIN, "@HOTFIXENUMLOW")


# ============ 3. Nao administradores continuam bloqueados ============
print("\nADMIN — papeis nao administrativos seguem em 403")

for rotulo, role in [("vendedor 'user'", "user"), ("'USER'", "USER"),
                     ("'manager'", "manager"), ("'MANAGER'", "MANAGER"),
                     ("'seller'", "seller"), ("enum USER", _StrEnumLower.USER),
                     ("role vazio", ""), ("role None", None)]:
    _as(role)
    r = client.post("/api/variables", json={
        "token": "@NEGADO", "name": "x", "kind": "fixed", "fixed_value": "v"})
    check(r.status_code == 403, f"{rotulo}: POST -> 403 (got {r.status_code})")

# vendedor CONTINUA podendo ler e usar as variaveis (permissao inalterada)
_as("user")
check(client.get("/api/variables").status_code == 200, "vendedor LISTA variaveis -> 200")
check(client.get("/api/variables/catalog").status_code == 200, "vendedor le o catalogo -> 200")
check(client.post("/api/variables/preview", json={"text": "oi"}).status_code == 200,
      "vendedor usa o preview -> 200")

# vendedor tambem nao edita nem exclui
_as("ADMIN")
_r_alvo = client.post("/api/variables", json={
    "token": "@ALVO", "name": "Alvo", "kind": "fixed", "fixed_value": "v"})
check(_r_alvo.status_code == 201, f"admin cria a variavel alvo -> 201 (got {_r_alvo.status_code})")
# Sem guarda, uma regressao mataria a suite aqui (KeyError) e as secoes 4 e 5
# nunca rodariam — o relatorio de falha sairia incompleto.
vid = _r_alvo.json()["id"] if _r_alvo.status_code == 201 else 999999
_as("user")
check(client.put(f"/api/variables/{vid}", json={"name": "hack"}).status_code == 403,
      "vendedor NAO edita -> 403")
check(client.delete(f"/api/variables/{vid}").status_code == 403,
      "vendedor NAO exclui -> 403")


# ============ 4. Sem autenticacao -> 401 ============
print("\nADMIN — sem autenticacao")

main.app.dependency_overrides.pop(get_current_user)
anon = TestClient(main.app)
check(anon.get("/api/variables").status_code == 401, "GET sem autenticacao -> 401")
check(anon.post("/api/variables", json={
    "token": "@ANON", "name": "x", "kind": "fixed", "fixed_value": "v"}).status_code == 401,
    "POST sem autenticacao -> 401")
check(anon.put(f"/api/variables/{vid}", json={"name": "x"}).status_code == 401,
      "PUT sem autenticacao -> 401")
check(anon.delete(f"/api/variables/{vid}").status_code == 401,
      "DELETE sem autenticacao -> 401")
main.app.dependency_overrides[get_current_user] = lambda: CURRENT["user"]


# ============ 5. O guard central e o unico ponto de decisao ============
print("\nADMIN — guard centralizado")

auth_src = (CONVERSAS_DIR / "app" / "auth.py").read_text(encoding="utf-8")
check("def is_admin_role" in auth_src, "normalizacao central existe em conversas/app/auth.py")
check("is_admin_role(current_user.role)" in auth_src,
      "require_admin usa a normalizacao central")
check('current_user.role != "admin"' not in auth_src,
      "comparacao literal antiga removida")

# nenhuma rota reimplementa a checagem
import glob  # noqa: E402

# Procura ACESSO A ATRIBUTO comparado (`.role ==` / `.role !=`), que so
# aparece em codigo — prosa de docstring nao usa o ponto.
import re as _re  # noqa: E402

_DUP = _re.compile(r"\.role\s*(==|!=)")
duplicadas = []
for caminho in glob.glob(str(CONVERSAS_DIR / "app" / "routers" / "*.py")):
    for linha in pathlib.Path(caminho).read_text(encoding="utf-8").splitlines():
        if _DUP.search(linha):
            duplicadas.append(f"{pathlib.Path(caminho).name}: {linha.strip()}")
check(not duplicadas, f"nenhuma rota duplica a comparacao de papel (got {duplicadas})")
check(_DUP.search("if current_user.role != \"admin\":") is not None,
      "o detector de duplicacao realmente encontra o padrao que procura")

# as rotas administrativas continuam protegidas (contagem nao pode cair)
guards = sum(
    pathlib.Path(p).read_text(encoding="utf-8").count("Depends(require_admin)")
    for p in glob.glob(str(CONVERSAS_DIR / "app" / "routers" / "*.py"))
)
check(guards >= 7, f"guards require_admin preservados nas rotas (got {guards})")

# o teste NAO pode substituir o guard real (foi assim que o bug passou)
suite_src = (ROOT / "tests" / "test_conversas_variables.py").read_text(encoding="utf-8")
check("dependency_overrides[require_admin]" not in suite_src,
      "suite de variaveis exercita o require_admin REAL (sem duble)")


# --- Resultado ---
main.app.dependency_overrides.clear()
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE PAPEL ADMIN PASSARAM")
