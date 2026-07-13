# -*- coding: utf-8 -*-
"""
PERPETUA-PRODUCTION-DRIFT-01 — regressão dos hotfixes consolidados da VPS.

Prova, em processo e sem nenhuma dependência externa, que:
  1. GET /api/leads/segment NÃO retorna 500 por TypeError com leads no
     resultado (era o bug: `_build_lead_response(l, db)` com a assinatura de
     1 parâmetro — hotfixado direto na VPS em 2026-07-08 e consolidado aqui).
  2. A resposta preserva a estrutura {total, skip, limit, leads} e os
     filtros/paginação continuam funcionando (search, skip, limit).
  3. Guard estático: nenhuma chamada `_build_lead_response(l, db)` volta ao
     arquivo (tripwire da regressão sem depender de dados).
  4. docker-compose.yml: o serviço `crm` recebe INTERNAL_AI_AUTH_SECRET por
     EXPANSÃO de variável (`${INTERNAL_AI_AUTH_SECRET:-}`), exatamente uma vez,
     somente no serviço crm, sem valor literal/hardcoded — e o YAML continua
     parseável. O .env real NUNCA é lido.

NÃO toca produção. Sem Gemini (GEMINI_API_KEY vazio), sem rede, sem n8n
(lead criado direto no SQLite descartável de scratch/, sem passar pela rota
que dispara automações), sem dados reais.

Rodar:  python tests/test_leads_segment_drift.py
   ou:  python -m pytest tests/test_leads_segment_drift.py
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import os  # noqa: E402

pathlib.Path("scratch").mkdir(exist_ok=True)
os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": "sqlite:///./scratch/leads_segment_test.db",
    "SEED_INITIAL_ADMIN": "true",
    "ADMIN_INITIAL_EMAIL": "admin@local.test",
    "ADMIN_INITIAL_PASSWORD": "LocalSmoke123!",
    # GEMINI vazio de propósito: nenhum teste pode chamar o Gemini de verdade.
    "GEMINI_API_KEY": "",
})

ROOT = pathlib.Path(__file__).resolve().parent.parent
ADMIN_EMAIL = "admin@local.test"
ADMIN_PASSWORD = "LocalSmoke123!"

_DB_INITIALIZED = False


def _client():
    # Mesmo padrão da suíte test_perpetua_internal_auth.py: deleta o DB
    # descartável UMA vez (Windows trava o arquivo entre clients) e reusa.
    global _DB_INITIALIZED
    db = pathlib.Path("scratch/leads_segment_test.db")
    if not _DB_INITIALIZED and db.exists():
        try:
            db.unlink()
        except PermissionError:
            pass
    _DB_INITIALIZED = True
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _seed_leads():
    """Insere leads direto no banco descartável — sem passar pela rota POST
    (que poderia disparar integrações) e garantindo resultado não vazio no
    segment, condição necessária para reproduzir o bug antigo."""
    from app.database import SessionLocal
    from app.models.lead import Lead
    db = SessionLocal()
    try:
        if db.query(Lead).count() == 0:
            db.add(Lead(nome="Lead Teste Um", email="um@teste.local",
                        whatsapp="+56000000001", destinos=["Atacama"]))
            db.add(Lead(nome="Lead Teste Dois", email="dois@teste.local",
                        whatsapp="+56000000002", destinos=["Uyuni"]))
            db.commit()
    finally:
        db.close()


def _login(client):
    r = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, f"login falhou: {r.status_code} {r.text[:200]}"


# ── 1. Regressão principal: segment com leads não retorna 500 ──────────────

def test_segment_retorna_200_com_leads():
    with _client() as client:
        _seed_leads()
        _login(client)
        r = client.get("/api/leads/segment")
        assert r.status_code == 200, (
            f"segment quebrou (era o bug do drift: TypeError por "
            f"_build_lead_response(l, db)): {r.status_code} {r.text[:300]}"
        )
        body = r.json()
        for key in ("total", "skip", "limit", "leads"):
            assert key in body, f"campo '{key}' ausente da resposta"
        assert body["total"] >= 2, "esperava os leads semeados no total"
        assert len(body["leads"]) >= 2, "esperava leads na lista (condição do bug)"
        lead = body["leads"][0]
        for key in ("id", "nome"):
            assert key in lead, f"campo '{key}' ausente no LeadResponse"


def test_segment_preserva_filtros_e_paginacao():
    with _client() as client:
        _seed_leads()
        _login(client)
        r = client.get("/api/leads/segment", params={"search": "Lead Teste Um", "skip": 0, "limit": 1})
        assert r.status_code == 200, f"segment com filtros quebrou: {r.status_code}"
        body = r.json()
        assert body["skip"] == 0 and body["limit"] == 1, "skip/limit não ecoados"
        assert body["total"] >= 1, "filtro search não encontrou o lead semeado"
        assert len(body["leads"]) == 1, "limit=1 não respeitado"
        assert "Um" in body["leads"][0]["nome"]

        r2 = client.get("/api/leads/segment", params={"destino": "Atacama"})
        assert r2.status_code == 200, f"filtro destino quebrou: {r2.status_code}"
        assert r2.json()["total"] >= 1


# ── 2. Guard estático do código (tripwire da regressão) ────────────────────

def test_guard_estatico_sem_chamada_com_db():
    src = (ROOT / "app" / "routers" / "leads.py").read_text(encoding="utf-8")
    assert "_build_lead_response(l, db)" not in src, (
        "regressão do drift: chamada com 'db' voltou ao leads.py"
    )
    assert "def _build_lead_response(lead: Lead)" in src, (
        "assinatura de _build_lead_response mudou — revisar o guard"
    )


# ── 3. Guard estático do docker-compose (drift B) ──────────────────────────

COMPOSE_PATH = ROOT / "docker-compose.yml"
EXPECTED_LINE = "- INTERNAL_AI_AUTH_SECRET=${INTERNAL_AI_AUTH_SECRET:-}"


def _crm_block():
    """Bloco do serviço crm: de '  crm:' até o próximo serviço no mesmo nível."""
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    m = re.search(r"^  crm:\n(.*?)(?=^  \w[\w-]*:|\Z)", text, re.M | re.S)
    assert m, "bloco do serviço crm não encontrado no compose"
    return text, m.group(0)


def test_compose_crm_recebe_internal_ai_auth_secret():
    text, crm = _crm_block()
    assert EXPECTED_LINE in crm, (
        "serviço crm sem INTERNAL_AI_AUTH_SECRET=${INTERNAL_AI_AUTH_SECRET:-}"
    )
    assert text.count("INTERNAL_AI_AUTH_SECRET") == 2, (
        "esperava a variável exatamente 1x no compose (nome + expansão na mesma linha)"
    )
    # Somente por expansão — nunca literal (nenhum valor hardcoded)
    for m in re.finditer(r"INTERNAL_AI_AUTH_SECRET=([^\s]+)", text):
        assert m.group(1).startswith("${"), "valor literal detectado para o segredo"
    # Somente no serviço crm
    fora = text.replace(crm, "")
    assert "INTERNAL_AI_AUTH_SECRET" not in fora, (
        "INTERNAL_AI_AUTH_SECRET vazou para outro serviço do compose"
    )


def test_compose_continua_parseavel():
    import yaml
    data = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    env = data["services"]["crm"]["environment"]
    assert any(e.startswith("INTERNAL_AI_AUTH_SECRET=") for e in env), (
        "environment do crm sem INTERNAL_AI_AUTH_SECRET após parse YAML"
    )
    for svc in ("postgres", "n8n", "conversas"):
        other_env = data["services"].get(svc, {}).get("environment", []) or []
        assert not any(str(e).startswith("INTERNAL_AI_AUTH_SECRET") for e in other_env), (
            f"variável indevida no serviço {svc}"
        )


def test_env_example_documenta_variavel():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "INTERNAL_AI_AUTH_SECRET" in env_example, (
        ".env.example deveria documentar INTERNAL_AI_AUTH_SECRET (nome, sem valor)"
    )


ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    failures = 0
    for fn in ALL_TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {fn.__name__}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(ALL_TESTS) - failures}/{len(ALL_TESTS)} testes OK")
    sys.exit(1 if failures else 0)
