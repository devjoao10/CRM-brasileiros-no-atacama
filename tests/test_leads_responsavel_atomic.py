# -*- coding: utf-8 -*-
"""
HARDEN-L3 — atomicidade da troca de responsável do lead.

PUT /api/leads/{id}/responsavel altera `leads.responsavel_id` e registra um
evento `responsavel_changed` em `lead_history`. Antes, eram DOIS commits: o
lead era persistido primeiro e o histórico depois, então uma falha no segundo
passo deixava o responsável trocado SEM rastro.

Prova que:
  1. Sucesso: responsável alterado E histórico criado.
  2. Falha ao criar o histórico: a troca de responsável é REVERTIDA
     (responsável original preservado, nenhum histórico gravado).

O caso 2 é o que distingue a correção: com dois commits, o lead já estaria
persistido quando o histórico falhasse.

NÃO toca produção, n8n, Meta nem rede. SQLite descartável em scratch/.

Rodar:  python tests/test_leads_responsavel_atomic.py
   ou:  python -m pytest tests/test_leads_responsavel_atomic.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import os  # noqa: E402

pathlib.Path("scratch").mkdir(exist_ok=True)
os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": "sqlite:///./scratch/leads_responsavel_test.db",
    "SEED_INITIAL_ADMIN": "true",
    "ADMIN_INITIAL_EMAIL": "admin@local.test",
    "ADMIN_INITIAL_PASSWORD": "LocalSmoke123!",
    "GEMINI_API_KEY": "",
})

ADMIN_EMAIL = "admin@local.test"
ADMIN_PASSWORD = "LocalSmoke123!"

_DB_INITIALIZED = False


def _client():
    # Mesmo padrão de test_leads_segment_drift.py: DB descartável apagado UMA
    # vez (Windows trava o arquivo entre clients) e reusado.
    global _DB_INITIALIZED
    db = pathlib.Path("scratch/leads_responsavel_test.db")
    if not _DB_INITIALIZED and db.exists():
        try:
            db.unlink()
        except PermissionError:
            pass
    _DB_INITIALIZED = True
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _login(client):
    r = client.post("/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200, f"login falhou: {r.status_code} {r.text[:200]}"
    return client.get("/api/auth/me").json()["id"]


def _novo_lead(nome):
    """Insere direto no banco — sem passar pela rota POST."""
    from app.database import SessionLocal
    from app.models.lead import Lead
    db = SessionLocal()
    try:
        lead = Lead(nome=nome, whatsapp="+56000000009")
        db.add(lead)
        db.commit()
        db.refresh(lead)
        return lead.id
    finally:
        db.close()


def _estado(lead_id):
    """(responsavel_id atual, nº de eventos responsavel_changed do lead)."""
    from app.database import SessionLocal
    from app.models.lead import Lead
    from app.models.pipeline import LeadHistory
    db = SessionLocal()
    try:
        return (
            db.query(Lead).filter(Lead.id == lead_id).first().responsavel_id,
            db.query(LeadHistory).filter(
                LeadHistory.lead_id == lead_id,
                LeadHistory.evento == "responsavel_changed",
            ).count(),
        )
    finally:
        db.close()


# ── 1. Sucesso: responsável alterado E histórico criado ────────────────────

def test_sucesso_grava_responsavel_e_historico():
    with _client() as client:
        admin_id = _login(client)
        lead_id = _novo_lead("Lead Atomicidade OK")
        assert _estado(lead_id) == (None, 0), "lead deve nascer sem responsável"

        r = client.put(f"/api/leads/{lead_id}/responsavel?responsavel_id={admin_id}")
        assert r.status_code == 200, f"esperava 200, veio {r.status_code}: {r.text[:200]}"

        resp_id, n_hist = _estado(lead_id)
        assert resp_id == admin_id, f"responsável não foi gravado (got {resp_id})"
        assert n_hist == 1, f"esperava 1 evento no histórico, veio {n_hist}"


# ── 2. Falha no histórico REVERTE a troca de responsável ───────────────────

def test_falha_no_historico_reverte_a_troca():
    import app.models.pipeline as pipeline

    with _client() as client:
        admin_id = _login(client)
        lead_id = _novo_lead("Lead Atomicidade Falha")
        antes = _estado(lead_id)
        assert antes == (None, 0), "lead deve nascer sem responsável"

        # Injeta falha exatamente na criação do LeadHistory. O router faz
        # `from app.models.pipeline import LeadHistory` dentro da função, então
        # o patch no módulo é resolvido na chamada.
        original = pipeline.LeadHistory

        def _explode(*a, **k):
            raise RuntimeError("falha simulada ao gravar historico")

        pipeline.LeadHistory = _explode
        try:
            try:
                client.put(f"/api/leads/{lead_id}/responsavel?responsavel_id={admin_id}")
            except RuntimeError:
                pass  # TestClient propaga a exceção do servidor — é o esperado
        finally:
            pipeline.LeadHistory = original

        depois = _estado(lead_id)
        assert depois == antes, (
            f"a troca deveria ter sido revertida junto com o histórico: "
            f"antes={antes} depois={depois}"
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
