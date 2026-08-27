# Harness strategies

Gerado pelo aia-harness para um projeto **Python** e corrigido na auditoria pos-init:
os defaults de ecossistema (ruff/mypy/pytest/build) NAO existem neste repo.

## Lint & format
- Nao configurado. `ruff` nao e dependencia nem esta no PATH.

## Compilation / typecheck
- Nao configurado. `mypy` nao e dependencia nem esta no PATH.

## Language server
- Sugestao (opt-in, `.lsp.json` nao foi instalado): `npx -y pyright-langserver --stdio`.

## Unit testing
- Cada teste e um programa executavel: `python tests/test_<nome>.py`.
- `pytest` NAO e dependencia. O CI (`.github/workflows/test.yml`) roda um processo por arquivo,
  em dois jobs: CRM (Python 3.11, `requirements.txt`) e Conversas (Python 3.12,
  `conversas/requirements.txt`), separados pela string `CONVERSAS_DIR` dentro do arquivo de teste.

## Canonical command reference
- **Install (CRM):** `pip install -r requirements.txt`
- **Install (Conversas):** `pip install -r conversas/requirements.txt` (ambiente separado)
- **Test:** `python tests/test_<nome>.py`
- **Run/Dev (CRM):** `uvicorn app.main:app --reload --port 8000`
- **Run/Dev (Conversas):** `uvicorn app.main:app --reload --port 8001` (a partir de `conversas/`)
- **Stack completa:** `docker compose up -d`
