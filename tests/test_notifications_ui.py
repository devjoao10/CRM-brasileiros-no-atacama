"""
WP-UX-04 — Sino global de notificações: render + contrato da API.

- Render: o sino (bell/badge/dropdown) vem da _topbar.html via base.html,
  presente em página de setor E no hub; notifications.js carrega DEPOIS
  de auth.js/layout.js.
- API: /api/operational/notifications exige auth (401 sem token) e responde
  a lista/read-all com JWT válido. O token é gerado direto com
  app.auth.create_access_token (mesmo SECRET_KEY do processo) para não
  consumir o rate limit do login compartilhado com test_rate_limit.py.

Rodar:  python tests/test_notifications_ui.py   ou   python -m pytest tests/test_notifications_ui.py
"""
import os
import pathlib
import sys

import jinja2

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ADMIN_EMAIL = "admin@local.test"


def _render(name: str) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(ROOT / "templates")), autoescape=True
    )
    ctx = {"page_title": "T", "active_nav": "dashboard",
           "user": {"nome": "Teste", "role": "admin"}}
    return env.get_template(name).render(**ctx)


def test_bell_present_everywhere():
    for name in ("dashboard.html", "hub.html", "operational/boards.html"):
        html = _render(name)
        assert 'id="notifBell"' in html, f"{name}: sino ausente"
        assert 'id="notifDropdown"' in html, f"{name}: dropdown ausente"
        assert 'id="notifMarkAll"' in html, f"{name}: 'marcar tudo' ausente"
        assert "/static/js/notifications.js" in html, f"{name}: script ausente"
        assert html.index("/static/js/layout.js") < html.index("/static/js/notifications.js"), \
            f"{name}: ordem de scripts errada"


def _client():
    os.environ.update({
        "ENVIRONMENT": "development",
        "DATABASE_URL": "sqlite:///./scratch/notif_test.db",
        "SEED_INITIAL_ADMIN": "true",
        "ADMIN_INITIAL_EMAIL": ADMIN_EMAIL,
        "ADMIN_INITIAL_PASSWORD": "LocalSmoke123!",
    })
    pathlib.Path("scratch").mkdir(exist_ok=True)
    from fastapi.testclient import TestClient  # requer httpx
    from app.main import app
    return TestClient(app)


def test_notifications_api_contract():
    with _client() as client:
        # sem auth -> 401
        r = client.get("/api/operational/notifications")
        assert r.status_code == 401, f"sem token deveria ser 401: {r.status_code}"

        # com JWT valido (gerado em processo, sem passar pelo login)
        from app.auth import create_access_token
        token = create_access_token({"sub": ADMIN_EMAIL, "role": "admin"})
        headers = {"Authorization": f"Bearer {token}"}

        r = client.get("/api/operational/notifications", headers=headers)
        assert r.status_code == 200, f"lista deveria ser 200: {r.status_code}"
        assert isinstance(r.json(), list)

        r = client.post("/api/operational/notifications/read-all", headers=headers)
        assert r.status_code == 200, f"read-all deveria ser 200: {r.status_code}"


if __name__ == "__main__":
    test_bell_present_everywhere()
    print("OK test_bell_present_everywhere")
    try:
        test_notifications_api_contract()
        print("OK test_notifications_api_contract")
    except ImportError as e:
        print("SKIP API (dependencia ausente p/ TestClient):", e)
        raise SystemExit(2)
    print("OK: sino render + contrato da API")
