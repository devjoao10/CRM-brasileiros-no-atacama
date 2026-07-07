"""
WP-QA-01 — Regressão de segurança por varredura estática (greps versionados).

Falha se um padrão proibido reaparecer. Sem rede, sem banco, sem app rodando.

Rodar:  python tests/test_security_greps.py
   ou:  python -m pytest tests/test_security_greps.py
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _files(*globs):
    out = []
    for g in globs:
        out += [p for p in ROOT.glob(g) if p.is_file()]
    return out


def _read(p):
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def test_no_localhost_5678_in_templates():
    bad = [p for p in _files("templates/**/*.html") if "localhost:5678" in _read(p)]
    assert not bad, f"localhost:5678 reintroduzido em: {[str(p) for p in bad]}"


def test_no_insecure_seeds_or_test_users():
    pat = re.compile(r"admin123|create_test_user|admin@teste")
    bad = [p for p in _files("app/**/*.py", "templates/**/*.html", "migrations/**/*.py", "scripts/**/*")
           if pat.search(_read(p))]
    assert not bad, f"seed/usuario inseguro encontrado em: {[str(p) for p in bad]}"


def test_no_raw_destructive_sql():
    pat = re.compile(r"\bDROP\s+TABLE\b|\bDELETE\s+FROM\b", re.IGNORECASE)
    bad = [p for p in _files("app/**/*.py", "migrations/**/*.py") if pat.search(_read(p))]
    assert not bad, f"SQL destrutivo cru encontrado em: {[str(p) for p in bad]}"


def test_admin_guards_present():
    total = sum(_read(p).count("require_admin") for p in _files("app/**/*.py", "app/*.py"))
    assert total >= 13, f"guards require_admin abaixo do esperado: {total}"


def test_ai_chat_uses_dompurify():
    ai = _read(ROOT / "templates" / "ai.html")
    assert "dompurify" in ai.lower(), "ai.html: DOMPurify ausente (SEC-XSS-01)"
    assert "DOMPurify.sanitize(marked.parse(content))" in ai, "ai.html: sanitize do markdown ausente"


def test_esc_escapes_quotes():
    # SEC-XSS-02: esc()/escapeHtml() devem escapar aspas simples
    samples = ["templates/leads.html", "templates/tags.html", "templates/segmentacao.html"]
    for s in samples:
        t = _read(ROOT / s)
        assert "&#39;" in t, f"{s}: esc() nao escapa aspas simples (regressao SEC-XSS-02)"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"OK {name}")
    print("OK: todos os greps de seguranca passaram")
