"""
AUDIT-2026-08-W2G — Endurecimento de autorização/validação do CRM.

Prova comportamentalmente, com o app em processo (TestClient + SQLite
descartável), cada achado da wave 2G:

  F1  relatório da empresa inteira era legível por QUALQUER autenticado
      (o "apenas admin" existia só no navegador) -> agora 403;
  F2  dono da tarefa era definível pelo corpo da requisição por qualquer um,
      tanto no create quanto no update (setattr cego) -> agora só admin;
      lead_id inexistente batia na FK e virava 500 -> agora 404;
  F3  role string livre contra coluna Enum: "superadmin" COMMITAVA no SQLite e
      toda leitura ORM daquela linha passava a levantar LookupError -> 422;
  F4  formato de e-mail só era validado no create; no update passava "nope" e a
      conta perdia o login (o e-mail é o `sub` do JWT) -> 422;
  F5  `delete_user` proíbe auto-desativação, mas o PUT fazia a mesma coisa sem
      nenhuma guarda, e nada protegia o último admin -> 400 nos dois casos;
  F6  GET /api/users/verify-click era inalcançável (declarado depois de
      GET /{user_id}: o Starlette casava a rota parametrizada e devolvia 422);
  F7/F8 create concorrente devolvia 500 onde a API documenta 409; equipe não
      tinha nenhuma unicidade de nome;
  F9  ids de membro inexistentes eram descartados em silêncio -> 404 nomeando;
  F11 paginação de tarefas sem desempate em coluna nullable/não única.

Tokens gerados em processo (create_access_token) — não consome o rate limit.

Rodar:  python tests/test_crm_authz_hardening.py
"""
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ADMIN_EMAIL = "admin@local.test"
# Domínio real de propósito: `EmailStr` recusa TLDs reservados (.test/.local),
# e o e-mail deste usuário passa pelo schema no teste de duplicata (F7).
USER_EMAIL = "w2g_user@example.com"
DB_FILE = ROOT / "scratch" / "w2g_authz_test.db"


def _client():
    os.environ.update({
        "ENVIRONMENT": "development",
        "DATABASE_URL": f"sqlite:///{DB_FILE.as_posix()}",
        "SEED_INITIAL_ADMIN": "true",
        "ADMIN_INITIAL_EMAIL": ADMIN_EMAIL,
        "ADMIN_INITIAL_PASSWORD": "LocalSmoke123!",
    })
    (ROOT / "scratch").mkdir(exist_ok=True)
    if DB_FILE.exists():
        DB_FILE.unlink()
    from fastapi.testclient import TestClient  # requer httpx
    from app.main import app

    # `create_user` faz validate_email(check_deliverability=True), ou seja,
    # consulta DNS/MX. Aqui só interessa o CONTRATO (409 x 500), então a
    # deliverability é desligada — a validação de FORMATO, que é o objeto do
    # F4, continua ativa e é exercida pelo Pydantic (EmailStr).
    import app.routers.users as users_router
    _real_validate = users_router.validate_email
    users_router.validate_email = lambda email, **kw: _real_validate(
        email, check_deliverability=False
    )
    return TestClient(app)


def _headers(email: str) -> dict:
    from app.auth import create_access_token
    return {"Authorization": f"Bearer {create_access_token({'sub': email})}"}


def _ensure_user(email: str, role: str) -> int:
    from app.auth import hash_password
    from app.database import SessionLocal
    from app.models.user import User
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == email).first()
        if not u:
            u = User(nome="Usuario W2G", email=email,
                     hashed_password=hash_password("LocalSmoke123!"),
                     role=role, is_active=True)
            db.add(u)
            db.commit()
            db.refresh(u)
        return u.id
    finally:
        db.close()


def _user_id(email: str) -> int:
    from app.database import SessionLocal
    from app.models.user import User
    db = SessionLocal()
    try:
        return db.query(User).filter(User.email == email).first().id
    finally:
        db.close()


def _task_owner(task_id: int):
    """Lê o dono direto do banco — a resposta da API não é prova suficiente."""
    from app.database import SessionLocal
    from app.models.task import Task
    db = SessionLocal()
    try:
        return db.query(Task).filter(Task.id == task_id).first().user_id
    finally:
        db.close()


def test_w2g_authz_hardening():
    with _client() as client:
        admin_h = _headers(ADMIN_EMAIL)
        admin_id = _user_id(ADMIN_EMAIL)
        user_id = _ensure_user(USER_EMAIL, "user")
        user_h = _headers(USER_EMAIL)

        # ── F1: relatório consolidado é só de admin ─────────────────────────
        r = client.get("/api/analytics/reports", headers=user_h)
        assert r.status_code == 403, \
            f"F1: nao-admin nao pode ler /api/analytics/reports: {r.status_code} {r.text[:200]}"
        r = client.get("/api/analytics/reports", headers=admin_h)
        assert r.status_code == 200, f"F1: admin continua lendo o relatorio: {r.text[:200]}"
        r = client.get("/api/analytics/reports", headers=admin_h,
                       params={"start_date": "1900-01-01", "end_date": "2026-12-31"})
        assert r.status_code == 400, f"F13: janela sem teto deveria ser 400: {r.status_code}"

        # ── F2: dono da tarefa não é definível pelo chamador comum ──────────
        r = client.post("/api/tasks", headers=user_h,
                        json={"titulo": "Tarefa roubada", "user_id": admin_id})
        assert r.status_code == 403, \
            f"F2: nao-admin nao pode escolher o dono no create: {r.status_code} {r.text[:200]}"

        r = client.post("/api/tasks", headers=user_h, json={"titulo": "Tarefa propria"})
        assert r.status_code == 201, r.text
        minha = r.json()
        assert _task_owner(minha["id"]) == user_id, \
            "F2: tarefa criada sem user_id deve ficar com quem criou"

        # ...e nem no update (o setattr cego era a parte irreversível)
        r = client.put(f"/api/tasks/{minha['id']}", headers=user_h,
                       json={"user_id": admin_id})
        assert r.status_code == 403, \
            f"F2: nao-admin nao pode reatribuir a tarefa: {r.status_code} {r.text[:200]}"
        assert _task_owner(minha["id"]) == user_id, \
            "F2: dono nao pode ter mudado apos o 403"

        # admin continua podendo delegar (o controle não virou bloqueio geral)
        r = client.put(f"/api/tasks/{minha['id']}", headers=admin_h,
                       json={"user_id": admin_id})
        assert r.status_code == 200 and _task_owner(minha["id"]) == admin_id, \
            f"F2: admin deve conseguir reatribuir: {r.status_code} {r.text[:200]}"

        # lead inexistente: 404 do contrato, nunca o 500 da violação de FK
        r = client.post("/api/tasks", headers=admin_h,
                        json={"titulo": "Com lead fantasma", "lead_id": 999999})
        assert r.status_code == 404, \
            f"F2: lead_id inexistente deveria ser 404: {r.status_code} {r.text[:200]}"

        # ── F3: role desconhecido morre no 422, nunca no banco ──────────────
        r = client.post("/api/users", headers=admin_h, json={
            "nome": "Super Hacker", "email": "super_w2g@example.com",
            "password": "LocalSmoke123!", "role": "superadmin",
        })
        assert r.status_code == 422, \
            f"F3: role 'superadmin' deveria ser 422 (era 500/linha ilegivel): {r.status_code}"
        assert any("role" in d.get("loc", []) for d in r.json()["detail"]), \
            f"F3: o 422 tem que ser SOBRE o campo role, nao sobre outro campo: {r.json()}"

        # ── F4: formato de e-mail validado TAMBÉM no update ─────────────────
        r = client.put(f"/api/users/{user_id}", headers=admin_h, json={"email": "nope"})
        assert r.status_code == 422, \
            f"F4: e-mail invalido no update deveria ser 422: {r.status_code} {r.text[:200]}"

        # ── F5: admin não se auto-desativa nem se rebaixa; último admin fica ─
        r = client.put(f"/api/users/{admin_id}", headers=admin_h, json={"is_active": False})
        assert r.status_code == 400, \
            f"F5: auto-desativacao via PUT deveria ser 400: {r.status_code} {r.text[:200]}"
        r = client.put(f"/api/users/{admin_id}", headers=admin_h, json={"role": "user"})
        assert r.status_code == 400, \
            f"F5: rebaixar o ultimo admin deveria ser 400: {r.status_code} {r.text[:200]}"
        assert "administrador" in r.json()["detail"].lower(), \
            f"F5: mensagem deveria citar o ultimo administrador: {r.json()}"
        # ...e o admin continua admin de fato
        r = client.get(f"/api/users/{admin_id}", headers=admin_h)
        assert r.status_code == 200 and r.json()["role"] == "admin" and r.json()["is_active"]
        # edições inócuas do próprio cadastro seguem passando
        r = client.put(f"/api/users/{admin_id}", headers=admin_h, json={"nome": "Admin W2G"})
        assert r.status_code == 200, f"F5: renomear a si mesmo deve continuar valendo: {r.text[:200]}"

        # ── F6: /verify-click chega ao handler (não é engolida por /{user_id})
        r = client.get("/api/users/verify-click", params={"token": "token-invalido"})
        assert r.status_code != 422, \
            "F6: /verify-click ainda esta caindo na rota GET /{user_id} (422 de coercao)"
        assert r.status_code == 400 and "text/html" in r.headers.get("content-type", ""), \
            f"F6: deveria devolver o HTML de link invalido: {r.status_code} {r.headers.get('content-type')}"

        # ── F7/F8: duplicata é sempre 409, nunca 500 ────────────────────────
        assert client.post("/api/tags", headers=admin_h,
                           json={"nome": "TagW2G", "cor": "#123456"}).status_code == 201
        r = client.post("/api/tags", headers=admin_h, json={"nome": "TagW2G", "cor": "#654321"})
        assert r.status_code == 409, f"F7: tag duplicada deveria ser 409: {r.status_code}"

        assert client.post("/api/teams", headers=admin_h,
                           json={"nome": "EquipeW2G"}).status_code == 201
        r = client.post("/api/teams", headers=admin_h, json={"nome": "EquipeW2G"})
        assert r.status_code == 409, \
            f"F8: equipe duplicada deveria ser 409 (nao havia checagem alguma): {r.status_code}"

        r = client.post("/api/users", headers=admin_h, json={
            "nome": "Duplicado", "email": USER_EMAIL, "password": "LocalSmoke123!",
        })
        assert r.status_code == 409, f"F7: usuario duplicado deveria ser 409: {r.status_code}"

        # ── F9: id de membro inexistente é nomeado no 404 ───────────────────
        team_id = client.get("/api/teams", headers=admin_h).json()["teams"][0]["id"]
        r = client.post(f"/api/teams/{team_id}/members", headers=admin_h,
                        json=[user_id, 999999])
        assert r.status_code == 404, \
            f"F9: id inexistente nao pode ser descartado em silencio: {r.status_code} {r.text[:200]}"
        assert "999999" in r.text, f"F9: o 404 precisa nomear o id ausente: {r.text[:200]}"
        # a lista válida continua funcionando
        r = client.post(f"/api/teams/{team_id}/members", headers=admin_h, json=[user_id])
        assert r.status_code == 200, r.text

        # ── F11: paginação estável com datas de vencimento colidindo ────────
        mesma_data = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        for i in range(6):
            r = client.post("/api/tasks", headers=admin_h,
                            json={"titulo": f"Colisao {i}", "data_vencimento": mesma_data})
            assert r.status_code == 201, r.text
        # ...e uma sem data, para fixar o lugar dos NULLs (SQLite x PostgreSQL)
        assert client.post("/api/tasks", headers=admin_h,
                           json={"titulo": "Sem data"}).status_code == 201

        def _ids(**params):
            r = client.get("/api/tasks", headers=admin_h, params=params)
            assert r.status_code == 200, r.text
            return [t["id"] for t in r.json()]

        primeira, segunda = _ids(limit=100), _ids(limit=100)
        assert primeira == segunda, \
            f"F11: duas chamadas identicas mudaram a ordem: {primeira} != {segunda}"
        pag = _ids(limit=3, skip=0) + _ids(limit=3, skip=3) + _ids(limit=3, skip=6)
        assert pag == primeira[:len(pag)], \
            f"F11: paginacao repetiu/pulou tarefas: {pag} vs {primeira}"
        assert len(set(pag)) == len(pag), f"F11: id duplicado entre paginas: {pag}"


if __name__ == "__main__":
    try:
        test_w2g_authz_hardening()
    except ImportError as e:
        print("SKIP (dependencia ausente p/ TestClient):", e)
        raise SystemExit(2)
    print("OK: W2G — F1 relatorio admin-only, F2 dono da tarefa, F3 role enum, "
          "F4 email no update, F5 ultimo admin, F6 verify-click, F7/F8 409, "
          "F9 membro inexistente, F11 paginacao estavel")
