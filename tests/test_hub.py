"""
WP-UX-02 — Hub de Setores: render + gate de rota.

- Render: hub.html estende base.html SEM sidebar, com os 3 cards de setor e logout.
- Rota: /hub exige cookie (mesmo gate `_require_cookie` das demais páginas);
  sem cookie -> 302 /login; com cookie -> 200.
  O teste de rota NÃO usa /api/auth/login (evita interferir no rate limit 5/min
  compartilhado com test_rate_limit.py) — o gate de página só checa presença
  do cookie, então um valor dummy prova o comportamento real.

Rodar:  python tests/test_hub.py   ou   python -m pytest tests/test_hub.py
"""
import os
import pathlib
import sys

import jinja2

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _render_hub() -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(ROOT / "templates")), autoescape=True
    )
    return env.get_template("hub.html").render(user={"nome": "Teste", "role": "admin"})


def test_hub_renders_without_sidebar():
    html = _render_hub()
    assert "<!DOCTYPE html>" in html and "{% " not in html
    assert html.count('class="sidebar"') == 0, "hub nao deve ter sidebar"
    assert 'class="top-header"' in html, "hub mantem topbar global"
    assert 'href="/dashboard"' in html, "card Comercial ausente"
    assert 'href="/operational/boards"' in html, "card Operacional ausente"
    assert 'href="/gestao/pendencias"' in html, "card Gestao Interna ausente"
    assert 'id="logoutBtn"' in html, "hub precisa de logout"
    assert html.index("/static/js/auth.js") < html.index("/static/js/layout.js")
    assert "localhost:5678" not in html


def _client():
    os.environ.update({
        "ENVIRONMENT": "development",
        "DATABASE_URL": "sqlite:///./scratch/hub_test.db",
        "SEED_INITIAL_ADMIN": "true",
        "ADMIN_INITIAL_EMAIL": "admin@local.test",
        "ADMIN_INITIAL_PASSWORD": "LocalSmoke123!",
    })
    pathlib.Path("scratch").mkdir(exist_ok=True)
    from fastapi.testclient import TestClient  # requer httpx
    from app.main import app
    return TestClient(app)


def test_hub_route_requires_cookie():
    with _client() as client:
        r = client.get("/hub", follow_redirects=False)
        assert r.status_code == 302, f"sem cookie deveria redirecionar: {r.status_code}"
        assert "/login" in r.headers.get("location", "")

        client.cookies.set("access_token", "Bearer dummy")
        r = client.get("/hub")
        assert r.status_code == 200, f"com cookie deveria servir o hub: {r.status_code}"
        assert "Hub de Setores" in r.text


if __name__ == "__main__":
    test_hub_renders_without_sidebar()
    print("OK test_hub_renders_without_sidebar")
    try:
        test_hub_route_requires_cookie()
        print("OK test_hub_route_requires_cookie")
    except ImportError as e:
        print("SKIP rota (dependencia ausente p/ TestClient):", e)
        raise SystemExit(2)
    print("OK: hub render + rota")
