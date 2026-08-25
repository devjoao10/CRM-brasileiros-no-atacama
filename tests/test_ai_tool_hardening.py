# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-W1C — regressão de hardening das ferramentas da Perpétua.

Prova, em processo e sem Gemini/rede, que os buracos confirmados na auditoria
continuam fechados. As ferramentas são chamadas DIRETAMENTE (não pelo endpoint
de chat), porque o objetivo é provar o guard, não o modelo:

  F1 — call_internal_api recusa paths que escapam do loopback (o bypass real era
       um `@` inicial: urlsplit("http://127.0.0.1:8000@evil/x").hostname == "evil").
  F4 — call_internal_api recusa /api/auth/ (rota que cunha API Key permanente)
       e qualquer DELETE, ANTES de assinar o HMAC interno.
  ok — um path legítimo (/api/leads) continua ACEITO pelo guard (sem over-block).
  F2 — generate_excel_document / generate_pdf_document com filename "../../evil"
       gravam DENTRO do diretório de uploads e em nenhum outro lugar.
  F3 — run_select_query recusa qualquer query que referencie `users`/`chat_messages`
       (hashes de senha e API keys), mas continua respondendo sobre `leads`.
  F6 — update_lead_status recusa status fora da whitelist e recusa rodar sem
       contexto de usuário.
  F7 — motivo_perda em lead que JÁ tem `campos_personalizados` não-vazio persiste
       de verdade (antes: mutação in-place do dict JSON era descartada pelo
       SQLAlchemy e a ferramenta ainda assim respondia "sucesso").

NÃO toca produção: SQLite descartável em scratch/, GEMINI_API_KEY vazio de
propósito, uploads em tempfile. Sem rede externa — todos os guards de rede
rejeitam antes de qualquer socket.

Rodar:  python tests/test_ai_tool_hardening.py
   ou:  python -m pytest tests/test_ai_tool_hardening.py
"""
import json
import os
import pathlib
import sys
import tempfile

# raiz do repo no sys.path (permite `python tests/test_ai_tool_hardening.py`)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# Ambiente DEVE ser definido ANTES de importar app.config / app.main
# (mesmo padrão de tests/test_perpetua_internal_auth.py).
pathlib.Path("scratch").mkdir(exist_ok=True)
_DB_PATH = pathlib.Path("scratch/ai_tool_hardening_test.db")
if _DB_PATH.exists():
    try:
        _DB_PATH.unlink()
    except PermissionError:
        pass

os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": "sqlite:///./scratch/ai_tool_hardening_test.db",
    # Nenhum teste aqui precisa de admin semeado.
    "SEED_INITIAL_ADMIN": "false",
    "INTERNAL_AI_AUTH_SECRET": "test-audit-w1c-secret-DO-NOT-USE-abc123",
    # GEMINI vazio de propósito: nenhum teste pode chamar o Gemini de verdade.
    "GEMINI_API_KEY": "",
})

import app.main  # noqa: E402,F401 — registra TODOS os models no metadata
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models.lead import Lead  # noqa: E402
from app.services import ai_tools  # noqa: E402

Base.metadata.create_all(bind=engine)


class _FakeUser:
    id = 4242
    email = "auditor@local.test"
    role = "agent"


def _with_context():
    ai_tools.set_ai_user_context(_FakeUser())


def _no_context():
    ai_tools.clear_ai_user_context()


# ── F1. SSRF: paths que escapam do loopback são recusados ──────────────
def test_f1_ssrf_paths_refused():
    bad_paths = [
        "@evil.example.com/steal",   # o bypass real (hostname vira evil.example.com)
        "//evil.com/x",              # protocol-relative
        "\\\\evil.com",              # UNC / backslash
        "http://evil",               # esquema absoluto
        "/api/../x",                 # traversal
        "/api/leads@evil.com/x",     # @ no meio do path
        "/api/le ads",               # espaço (caractere fora da whitelist)
        "/api/leads\n/x",            # controle
    ]
    for path in bad_paths:
        assert ai_tools._validate_internal_call("GET", path) is not None, \
            f"guard aceitou path perigoso: {path!r}"
        # E pela ferramenta de verdade: erro, sem assinar e sem abrir socket.
        _with_context()
        try:
            out = json.loads(ai_tools.call_internal_api("GET", path))
        finally:
            _no_context()
        assert "error" in out, f"call_internal_api não recusou {path!r}: {out}"
        assert "status" not in out, f"call_internal_api chegou a executar {path!r}: {out}"


# ── F4. /api/auth/ e DELETE são negados antes de assinar ───────────────
def test_f4_auth_routes_and_delete_refused():
    assert ai_tools._validate_internal_call("POST", "/api/auth/token") is not None
    assert ai_tools._validate_internal_call("GET", "/api/auth/me") is not None
    assert ai_tools._validate_internal_call("POST", "/API/AUTH/token") is not None
    for method in ("DELETE", "delete"):
        assert ai_tools._validate_internal_call(method, "/api/leads/1") is not None, \
            f"{method} não foi bloqueado"

    _with_context()
    try:
        out = json.loads(ai_tools.call_internal_api("POST", "/api/auth/token"))
        out_del = json.loads(ai_tools.call_internal_api("DELETE", "/api/leads/1"))
    finally:
        _no_context()
    assert "error" in out and "status" not in out, out
    assert "error" in out_del and "status" not in out_del, out_del


# ── ok. Path legítimo continua aceito pelo guard (sem over-block) ──────
def test_legitimate_paths_still_accepted_by_guard():
    for method, path in [
        ("GET", "/api/leads"),
        ("POST", "/api/leads"),
        ("PUT", "/api/leads/12"),
        ("GET", "/api/leads?limit=10&status_venda=venda"),
        ("GET", "/api/analytics/dashboard"),
    ]:
        assert ai_tools._validate_internal_call(method, path) is None, \
            f"guard bloqueou path legítimo: {method} {path}"


# ── F2. Escrita de documento confinada ao diretório de uploads ─────────
def _assert_confined(generator, kwargs, extension):
    with tempfile.TemporaryDirectory(prefix="audit_w1c_") as sandbox:
        uploads = os.path.join(sandbox, "uploads")
        os.makedirs(uploads)
        old = ai_tools.UPLOAD_DIR
        ai_tools.UPLOAD_DIR = uploads
        try:
            payload = json.loads(generator(filename="../../evil", **kwargs))
        finally:
            ai_tools.UPLOAD_DIR = old

        assert payload.get("success") is True, f"esperava sucesso, veio: {payload}"
        name = payload["filename"]
        assert "/" not in name and "\\" not in name and ".." not in name, name
        assert name.endswith(extension), name
        assert os.path.isfile(os.path.join(uploads, name)), \
            f"arquivo não foi gravado em uploads/: {name}"
        # Nada escapou: o sandbox só contém o diretório de uploads.
        assert sorted(os.listdir(sandbox)) == ["uploads"], os.listdir(sandbox)
        assert sorted(os.listdir(uploads)) == [name], os.listdir(uploads)


def test_f2_excel_write_stays_inside_upload_dir():
    _assert_confined(
        ai_tools.generate_excel_document,
        {"sheet_name": "Leads", "headers": "Nome|Email", "rows": "Joao|j@x.test"},
        ".xlsx",
    )


def test_f2_pdf_write_stays_inside_upload_dir():
    _assert_confined(
        ai_tools.generate_pdf_document,
        {"title": "Relatorio", "content": "linha um\nlinha dois"},
        ".pdf",
    )


# ── F3. SQL não alcança credenciais, mas continua útil ────────────────
def test_f3_select_on_users_refused():
    for query in [
        "SELECT email, hashed_password FROM users",
        "select api_key from Users",
        "SELECT l.nome FROM leads l JOIN users u ON u.id = l.id",
        "SELECT content FROM chat_messages",
    ]:
        out = json.loads(ai_tools.run_select_query(query))
        assert "error" in out, f"query sensível não foi bloqueada: {query!r} -> {out}"
        assert "bloqueada" in out["error"].lower(), out
        # Não pode ter vindo linha nenhuma.
        assert not isinstance(out, list)


def test_f3_benign_select_on_leads_still_works():
    db = SessionLocal()
    try:
        db.add(Lead(nome="Lead Benigno F3", campos_personalizados={}))
        db.commit()
    finally:
        db.close()

    out = json.loads(ai_tools.run_select_query("SELECT id, nome FROM leads"))
    assert isinstance(out, list), f"SELECT benigno foi bloqueado: {out}"
    assert any(r["nome"] == "Lead Benigno F3" for r in out), out


# ── F6. Whitelist de status e exigência de contexto de usuário ─────────
def test_f6_bogus_status_refused():
    lead_id = _new_lead({"origem": "auditoria"})
    _with_context()
    try:
        out = json.loads(ai_tools.update_lead_status(lead_id, status_venda="arquivado_pela_ia"))
    finally:
        _no_context()
    assert "error" in out, out
    assert "status_venda" in out["error"], out
    assert _reload(lead_id).status_venda != "arquivado_pela_ia"


def test_f6_write_tools_refuse_without_user_context():
    lead_id = _new_lead({})
    _no_context()
    for out_raw in [
        ai_tools.update_lead_status(lead_id, status_venda="perda"),
        ai_tools.create_task(lead_id, "t", "d", "2026-09-01T10:00:00"),
        ai_tools.add_tag_to_lead(lead_id, "tag-da-injecao"),
        ai_tools.create_lead("Lead Sem Contexto"),
    ]:
        out = json.loads(out_raw)
        assert "error" in out, out
        assert "contexto" in out["error"].lower(), out
    assert _reload(lead_id).status_venda != "perda"


# ── F7. motivo_perda persiste em dict JSON já povoado ─────────────────
def test_f7_cancel_reason_persists_on_populated_json():
    lead_id = _new_lead({"origem": "instagram", "obs": "cliente antigo"})
    _with_context()
    try:
        out = json.loads(ai_tools.update_lead_status(
            lead_id, status_venda="perda", cancel_reason="preço acima do orçamento"
        ))
    finally:
        _no_context()
    assert out.get("success") is True, out

    lead = _reload(lead_id)
    assert lead.status_venda == "perda"
    # ANTES do fix (F7) isto falhava: a mutação in-place do dict JSON não era
    # rastreada pelo SQLAlchemy, o UPDATE não saía e a ferramenta mentia sucesso.
    assert lead.campos_personalizados.get("motivo_perda") == "preço acima do orçamento", \
        f"motivo_perda descartado: {lead.campos_personalizados}"
    # Os campos que já existiam continuam lá.
    assert lead.campos_personalizados.get("origem") == "instagram"


# ── helpers de banco ──────────────────────────────────────────────────
def _new_lead(campos):
    db = SessionLocal()
    try:
        lead = Lead(nome="Lead Auditoria W1C", campos_personalizados=campos)
        db.add(lead)
        db.commit()
        db.refresh(lead)
        return lead.id
    finally:
        db.close()


def _reload(lead_id):
    """Recarrega o lead em uma sessão NOVA (sem identity map contaminado)."""
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        db.expunge_all()
        return lead
    finally:
        db.close()


if __name__ == "__main__":
    tests = [
        test_f1_ssrf_paths_refused,
        test_f4_auth_routes_and_delete_refused,
        test_legitimate_paths_still_accepted_by_guard,
        test_f2_excel_write_stays_inside_upload_dir,
        test_f2_pdf_write_stays_inside_upload_dir,
        test_f3_select_on_users_refused,
        test_f3_benign_select_on_leads_still_works,
        test_f6_bogus_status_refused,
        test_f6_write_tools_refuse_without_user_context,
        test_f7_cancel_reason_persists_on_populated_json,
    ]
    failures = 0
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e}")
        else:
            print(f"OK {fn.__name__}")
    if failures:
        print(f"FALHOU: {failures} teste(s) de AUDIT-2026-08-W1C")
        raise SystemExit(1)
    print("OK: AUDIT-2026-08-W1C — todos os testes passaram")
