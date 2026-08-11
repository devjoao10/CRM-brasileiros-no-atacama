# -*- coding: utf-8 -*-
"""
HARDEN-L4 — filtro de destino compila SQL válido no PostgreSQL.

`leads.destinos` é coluna `json` (não `jsonb`) — confirmado na VPS. O operador
`@>` do PostgreSQL só existe para `jsonb`, então `destinos @> '["X"]'` levanta
`operator does not exist: json @> unknown`. A correção faz o cast na EXPRESSÃO
da query (`CAST(destinos AS JSONB) @> ...`), sem alterar schema nem dados.

Isso passava despercebido porque a suíte roda em SQLite, que toma o outro ramo
de `_json_list_contains` — o ramo PostgreSQL nunca era executado.

Prova, compilando a expressão nos dois dialetos (sem banco PostgreSQL):
  1. Ramo PostgreSQL gera CAST(... AS JSONB) @>  — e NUNCA `json @>` cru.
  2. Vale para as duas cópias da função (leads.py e segments.py).
  3. Ramo SQLite permanece inalterado (cast para texto + ILIKE).

Rodar:  python tests/test_leads_destino_filter_dialect.py
   ou:  python -m pytest tests/test_leads_destino_filter_dialect.py
"""
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import os  # noqa: E402

pathlib.Path("scratch").mkdir(exist_ok=True)
os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": "sqlite:///./scratch/destino_filter_test.db",
    "SEED_INITIAL_ADMIN": "false",
    "GEMINI_API_KEY": "",
})

from sqlalchemy.dialects import postgresql, sqlite  # noqa: E402

import app.routers.leads as leads_mod  # noqa: E402
import app.routers.segments as segments_mod  # noqa: E402
from app.models.lead import Lead  # noqa: E402

MODULOS = (("leads.py", leads_mod), ("segments.py", segments_mod))


def _sql(modulo, dialect, is_sqlite):
    """Compila _json_list_contains do módulo forçando o ramo desejado."""
    original = modulo.IS_SQLITE
    modulo.IS_SQLITE = is_sqlite
    try:
        expr = modulo._json_list_contains(Lead.destinos, "Atacama")
        return str(expr.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
    finally:
        modulo.IS_SQLITE = original


# ── 1 e 2. Ramo PostgreSQL: cast para JSONB, nunca `json @>` cru ──────────

def test_ramo_postgres_faz_cast_para_jsonb():
    for nome, modulo in MODULOS:
        sql = _sql(modulo, postgresql.dialect(), is_sqlite=False)
        assert "JSONB" in sql.upper(), f"{nome}: esperava cast para JSONB, veio: {sql}"
        assert "@>" in sql, f"{nome}: esperava o operador @>, veio: {sql}"
        # tripwire: a coluna crua nunca pode aparecer colada no @>
        assert not re.search(r"leads\.destinos\s*@>", sql), (
            f"{nome}: SQL inválido — `json @> ...` sem cast: {sql}"
        )


# ── 3. Ramo SQLite permanece como era ─────────────────────────────────────

def test_ramo_sqlite_inalterado():
    for nome, modulo in MODULOS:
        sql = _sql(modulo, sqlite.dialect(), is_sqlite=True).upper()
        assert "LIKE" in sql, f"{nome}: ramo SQLite deveria usar LIKE/ILIKE, veio: {sql}"
        assert "@>" not in sql, f"{nome}: ramo SQLite não pode usar @>, veio: {sql}"


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
