---
name: new-migration
description: Cria um script de migration manual idempotente em migrations/ seguindo a convenção do projeto (numeração sequencial, run(engine=None), compat SQLite/PostgreSQL, gate de produção). Use quando precisar reconciliar schema de um banco JÁ EXISTENTE — coluna nova, tabela nova, índice novo.
disable-model-invocation: true
---

# Nova migration manual

Este projeto **não usa Alembic** (pendente em WP-DATA-02). Bancos novos nascem completos via
`Base.metadata.create_all()` a partir dos models; bancos existentes são reconciliados por scripts
idempotentes em `migrations/`, rodados à mão, **fora do startup**.

## Antes de escrever

1. **A coluna/tabela já existe no model?** Se não, crie primeiro em `app/models/` (CRM) ou
   `conversas/app/models/` (Conversas). O schema segue o model, nunca o contrário.
2. **Qual o próximo número?** `ls migrations/m*.py | sort | tail -1` e some 1. Nunca reutilize.
3. **É CRM ou Conversas?** Migration do Conversas precisa inserir `conversas/` no `sys.path` e
   importar `app.config` de lá — roda em processo próprio.
4. **Depende de outra migration?** Se referencia FK criada por uma anterior, declare a ordem no
   docstring (padrão: `m003 -> m004 -> m005`).

## Template

```python
"""
mNNN — <WP-ID>: <o que muda> em bancos JA existentes.

Migration MANUAL e IDEMPOTENTE — **nao roda no startup**. Bancos novos nascem
completos via create_all a partir dos models.

Ordem em bancos antigos: <mAAA -> mBBB -> mNNN, ou "independente">.

Uso (LOCAL / STAGING):
    python migrations/mNNN_<escopo>_<assunto>.py

PRODUCAO: somente apos backup verificado + aprovacao humana, ANTES do codigo.
"""
import logging
import sys

from sqlalchemy import create_engine, inspect, text

from app.config import DATABASE_URL

logger = logging.getLogger("migrations.mNNN")


def run(engine=None):
    engine = engine or create_engine(DATABASE_URL)
    inspector = inspect(engine)
    actions = []

    # IDEMPOTENCIA: inspecione antes de agir.
    cols = {c["name"] for c in inspector.get_columns("<tabela>")}
    if "<coluna>" not in cols:
        with engine.begin() as conn:
            # Aditiva e nullable: rollback = deixar a coluna sem uso.
            conn.execute(text('ALTER TABLE <tabela> ADD COLUMN <coluna> <TIPO> NULL'))
        actions.append("+<tabela>.<coluna>")

    if not actions:
        logger.info("nada a fazer — schema ja reconciliado")
    else:
        logger.info("aplicado: %s", ", ".join(actions))
    return actions


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(0 if run() is not None else 1)
```

Para o **Conversas**, troque o bloco de imports por:

```python
import pathlib
_CONVERSAS_DIR = pathlib.Path(__file__).resolve().parent.parent / "conversas"
sys.path.insert(0, str(_CONVERSAS_DIR))

from sqlalchemy import create_engine, inspect  # noqa: E402
from app.config import DATABASE_URL  # noqa: E402 — config do CONVERSAS
import app.models.conversation  # noqa: F401, E402 — registra a tabela para a FK
```

## Regras não negociáveis

- **Idempotente**: rodar duas vezes não pode ter efeito colateral. Sempre inspecione antes.
- **Compat dupla**: o mesmo script roda em SQLite (dev) e PostgreSQL (prod). Evite sintaxe
  exclusiva de um dos dois; se precisar, ramifique.
- **Aditiva e nullable**: sem `DROP COLUMN`, sem `NOT NULL` sem default.
- **Nada no `lifespan`**: não acrescente `ALTER TABLE` em `app/main.py`.
- **Produção é ação humana**: backup verificado + validação de integridade + aprovação explícita.
  Agentes de IA não executam isso.

## Depois de escrever

1. Rode local: `python migrations/mNNN_<escopo>_<assunto>.py` — e rode **duas vezes**, para
   provar a idempotência.
2. Acrescente a linha na tabela `## Scripts` de `migrations/README.md`.
3. Se a mudança afeta um `*Response`, atualize o schema Pydantic correspondente.
