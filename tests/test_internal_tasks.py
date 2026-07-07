"""
WP-GI — Contrato da API de Pendências Internas (/api/internal/tasks).

Prova, com o app em processo (TestClient + SQLite descartável):
- 401 sem autenticação;
- criação (201) e status derivado (§8.6): pendente / atrasada / concluida;
- recorrência "rolling" (§8.5): concluir avança due_date e mantém pendente;
- GI-04: criar pendência para OUTRO usuário gera notificação `internal_task`
  na tabela do sino global;
- permissões: quem não é criador/responsável/admin não arquiva (403), mas o
  responsável conclui a própria pendência.

Tokens gerados em processo (create_access_token) — não consome o rate limit.

Rodar:  python tests/test_internal_tasks.py   ou   python -m pytest tests/test_internal_tasks.py
"""
import os
import pathlib
import sys
from datetime import date, timedelta

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ADMIN_EMAIL = "admin@local.test"
USER2_EMAIL = "gi_user2@local.test"


def _client():
    os.environ.update({
        "ENVIRONMENT": "development",
        "DATABASE_URL": "sqlite:///./scratch/gi_test.db",
        "SEED_INITIAL_ADMIN": "true",
        "ADMIN_INITIAL_EMAIL": ADMIN_EMAIL,
        "ADMIN_INITIAL_PASSWORD": "LocalSmoke123!",
    })
    pathlib.Path("scratch").mkdir(exist_ok=True)
    from fastapi.testclient import TestClient  # requer httpx
    from app.main import app
    return TestClient(app)


def _headers(email: str) -> dict:
    from app.auth import create_access_token
    return {"Authorization": f"Bearer {create_access_token({'sub': email, 'role': 'x'})}"}


def _ensure_user2() -> int:
    from app.auth import hash_password
    from app.database import SessionLocal
    from app.models.user import User
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == USER2_EMAIL).first()
        if not u:
            u = User(nome="Usuária GI", email=USER2_EMAIL,
                     hashed_password=hash_password("LocalSmoke123!"),
                     role="user", is_active=True)
            db.add(u)
            db.commit()
            db.refresh(u)
        return u.id
    finally:
        db.close()


def _admin_id() -> int:
    from app.database import SessionLocal
    from app.models.user import User
    db = SessionLocal()
    try:
        return db.query(User).filter(User.email == ADMIN_EMAIL).first().id
    finally:
        db.close()


def test_internal_tasks_full_contract():
    with _client() as client:
        # 1) sem auth -> 401
        r = client.get("/api/internal/tasks")
        assert r.status_code == 401, f"sem token deveria ser 401: {r.status_code}"

        admin_h = _headers(ADMIN_EMAIL)
        user2_id = _ensure_user2()
        user2_h = _headers(USER2_EMAIL)
        admin_id = _admin_id()
        today = date.today()

        # 2) admin cria pendência para user2 -> 201, pendente
        r = client.post("/api/internal/tasks", headers=admin_h, json={
            "title": "Conferir vouchers da semana",
            "description": "Checar reservas confirmadas",
            "assignee_id": user2_id,
            "due_date": (today + timedelta(days=2)).isoformat(),
        })
        assert r.status_code == 201, r.text
        t1 = r.json()
        assert t1["effective_status"] == "pendente" and t1["status"] == "pendente"
        assert t1["assignee_id"] == user2_id and t1["creator_nome"]

        # 3) GI-04: notificação internal_task criada para user2
        from app.database import SessionLocal
        from app.models.operational.notification import OperationalNotification
        db = SessionLocal()
        try:
            notif = (db.query(OperationalNotification)
                     .filter(OperationalNotification.user_id == user2_id,
                             OperationalNotification.event_type == "internal_task")
                     .order_by(OperationalNotification.id.desc()).first())
            assert notif is not None, "notificacao internal_task nao criada"
            assert "Conferir vouchers" in notif.message
            assert notif.card_id is None
        finally:
            db.close()

        # 4) pendência vencida -> effective_status atrasada (derivado, nunca gravado)
        r = client.post("/api/internal/tasks", headers=admin_h, json={
            "title": "Tarefa vencida",
            "assignee_id": admin_id,
            "due_date": (today - timedelta(days=1)).isoformat(),
        })
        assert r.status_code == 201
        t2 = r.json()
        assert t2["effective_status"] == "atrasada" and t2["status"] == "pendente"

        # 5) concluir pontual -> concluida
        r = client.post(f"/api/internal/tasks/{t2['id']}/complete", headers=admin_h)
        assert r.status_code == 200 and r.json()["effective_status"] == "concluida"

        # 6) recorrente diária: concluir avança a data e mantém pendente
        r = client.post("/api/internal/tasks", headers=admin_h, json={
            "title": "Backup diário do sistema",
            "assignee_id": admin_id,
            "task_type": "recorrente",
            "recurrence": "diaria",
            "due_date": today.isoformat(),
        })
        assert r.status_code == 201
        rec = r.json()
        r = client.post(f"/api/internal/tasks/{rec['id']}/complete", headers=admin_h)
        assert r.status_code == 200
        rec2 = r.json()
        assert rec2["status"] == "pendente", "recorrente nao deve concluir em definitivo"
        assert rec2["due_date"] == (today + timedelta(days=1)).isoformat(), \
            f"due_date deveria avancar 1 dia: {rec2['due_date']}"
        assert rec2["last_completed_at"], "ocorrencia concluida deve ser registrada"

        # 7) permissões: user2 nao arquiva pendencia criada pelo admin p/ admin
        r = client.post(f"/api/internal/tasks/{rec['id']}/archive", headers=user2_h)
        assert r.status_code == 403, f"user2 nao deveria arquivar: {r.status_code}"
        # ...mas conclui a pendencia da qual é responsável
        r = client.post(f"/api/internal/tasks/{t1['id']}/complete", headers=user2_h)
        assert r.status_code == 200 and r.json()["effective_status"] == "concluida"

        # 8) validação: recorrente sem recurrence -> 422
        r = client.post("/api/internal/tasks", headers=admin_h, json={
            "title": "Invalida", "assignee_id": admin_id, "task_type": "recorrente",
        })
        assert r.status_code == 422, f"recorrente sem recurrence deveria 422: {r.status_code}"


if __name__ == "__main__":
    try:
        test_internal_tasks_full_contract()
    except ImportError as e:
        print("SKIP (dependencia ausente p/ TestClient):", e)
        raise SystemExit(2)
    print("OK: contrato completo das pendencias internas (CRUD, status derivado, recorrencia, sino, permissoes)")
