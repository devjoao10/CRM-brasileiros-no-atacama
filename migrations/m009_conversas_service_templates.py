"""
m009 — CONV-CURATION-01: curadoria de templates do atendimento (CONVERSAS).

Cria (aditivo, sem destruir nada):
  - tabela `service_templates` — quais templates APROVADOS PELA META o CRM
    autoriza a aparecer no seletor do atendimento.
      id             INTEGER PK
      name           VARCHAR(512) NOT NULL
      language       VARCHAR(10)  NOT NULL
      created_at     TIMESTAMP(TZ)
  - UNIQUE (name, language) — a identidade de um template na Meta e o PAR,
    nunca o nome sozinho (a mesma conta pode ter o mesmo nome em varios idiomas).

BOOTSTRAP DELIBERADAMENTE VAZIO. A migration NAO insere linha nenhuma.

Isso e a decisao central deste pacote, nao um esquecimento: a conta tem 34
templates APPROVED, entre eles `alerta_novo_lead`, `alerta_crm`,
`notificacao_crm`, `hello_world` e `teste`. Autorizar todos no bootstrap
preservaria exatamente o problema que este pacote existe para resolver —
templates internos oferecidos a clientes. Autorizar por heuristica de nome
(prefixo `alerta_`, etc.) seria adivinhar finalidade a partir de string.

Consequencia operacional, ACEITA E EXPLICITA: apos o deploy, o seletor do
atendimento comeca VAZIO e um administrador precisa liberar em Templates >
"Disponiveis no atendimento" antes do primeiro uso. A UI diz isso ao operador
com todas as letras, e o composer permanece BLOQUEADO (fail closed) — nunca
liberando texto livre como consolo.

Nao toca `message_templates`, nao altera status vindo da Meta, nao apaga dados.

Migration MANUAL e IDEMPOTENTE — **nao roda no startup**. Bancos novos ja
nascem completos via `Base.metadata.create_all()` no lifespan do Conversas.
Este script reconcilia bancos EXISTENTES (dev/staging/prod).

ATENCAO — este e o app CONVERSAS (nao o CRM): o script insere `conversas/` no
inicio do sys.path para que `app.*` resolva para conversas/app (mesma tecnica
dos tests/test_conversas_*). Rodar em PROCESSO PROPRIO.

Ordem em bancos antigos: m003 -> m004 -> m005 -> m006 -> m007 -> m008 -> m009.

Uso (LOCAL / STAGING):
    python migrations/m009_conversas_service_templates.py

PRODUCAO: somente apos backup verificado + aprovacao humana (migrations/README.md).
"""
import logging
import pathlib
import sys

# `app.*` deve resolver para conversas/app — ver docstring.
_CONVERSAS_DIR = pathlib.Path(__file__).resolve().parent.parent / "conversas"
sys.path.insert(0, str(_CONVERSAS_DIR))

from sqlalchemy import create_engine, inspect  # noqa: E402

from app.config import DATABASE_URL  # noqa: E402 — config do CONVERSAS
from app.database import Base  # noqa: E402
from app.models.template import ServiceTemplate  # noqa: E402,F401 — registra a tabela

logger = logging.getLogger("migrations.m009")


def run(engine=None):
    engine = engine or create_engine(DATABASE_URL)
    insp = inspect(engine)
    actions = []

    if "service_templates" in insp.get_table_names():
        actions.append("service_templates:already-present")
    else:
        # create_all com tables=[...] cria SO esta tabela: nenhuma outra tabela
        # do metadata e tocada, mesmo que exista drift em alguma delas.
        Base.metadata.create_all(bind=engine, tables=[ServiceTemplate.__table__])
        actions.append("service_templates:created")

    # Conferencia explicita do UNIQUE composto — se a tabela veio de um banco
    # antigo sem a constraint, isso aparece no log em vez de passar silencioso.
    uniques = {u["name"] for u in inspect(engine).get_unique_constraints("service_templates")}
    actions.append(
        "uq_service_templates_name_language:present"
        if "uq_service_templates_name_language" in uniques
        else "uq_service_templates_name_language:AUSENTE (verificar manualmente)"
    )

    # Bootstrap vazio e INTENCIONAL — ver docstring. Reportado para nao parecer
    # que a migration falhou em popular alguma coisa.
    with engine.connect() as conn:
        total = conn.exec_driver_sql("SELECT COUNT(*) FROM service_templates").scalar()
    actions.append(f"autorizacoes-existentes:{total} (bootstrap vazio e proposital)")

    return actions


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    safe = DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL
    print(f"[m009] alvo (conversas): {safe}")
    print("[m009] acoes:", run())
    print("[m009] OK (idempotente)")
