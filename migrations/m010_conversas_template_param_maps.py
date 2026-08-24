"""
m010 — CONV-TPLMAP-01: mapeamento {{n}} -> @VARIAVEL de templates (CONVERSAS).

Cria (aditivo, sem destruir nada):
  - tabela `template_param_maps` — de que variavel interna sai o valor de cada
    parametro posicional do BODY de um template da Meta.
      id          INTEGER PK
      name        VARCHAR(512) NOT NULL
      language    VARCHAR(10)  NOT NULL
      position    INTEGER      NOT NULL   -- o n de {{n}}, base 1
      token       VARCHAR(61)  NOT NULL   -- "@PRIMEIRONOMECLIENTE"
      created_at  TIMESTAMP(TZ)
  - UNIQUE (name, language, position) — a identidade de um template e o PAR
    (name, language), e cada posicao admite NO MAXIMO um mapeamento. A regra
    fica no BANCO, nao numa verificacao em Python que o proximo endpoint
    esquece de chamar.

BOOTSTRAP DELIBERADAMENTE VAZIO. A migration NAO insere linha nenhuma.

Sem mapeamento, `_build_template_send` exige exatamente os mesmos parametros
que exigia antes deste pacote — o comportamento manual atual e preservado byte
a byte. Popular por heuristica (ex.: "todo {{1}} deve ser o primeiro nome")
transformaria templates antigos em automaticos por adivinhacao, exatamente o
que o pacote proibe. Mapear e ato explicito do administrador em
Templates > Variaveis do template.

Nao toca `message_templates`, nao toca `service_templates`, nao fala com a Meta,
nao altera BODY aprovado nenhum, nao apaga dados.

Migration MANUAL e IDEMPOTENTE — **nao roda no startup**. Bancos novos ja
nascem completos via `Base.metadata.create_all()` no lifespan do Conversas.
Este script reconcilia bancos EXISTENTES (dev/staging/prod).

ATENCAO — este e o app CONVERSAS (nao o CRM): o script insere `conversas/` no
inicio do sys.path para que `app.*` resolva para conversas/app (mesma tecnica
dos tests/test_conversas_*). Rodar em PROCESSO PROPRIO.

Ordem em bancos antigos: m003 -> m004 -> m005 -> m006 -> m007 -> m008 -> m009 -> m010.

Uso (LOCAL / STAGING):
    python migrations/m010_conversas_template_param_maps.py

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
from app.models.template import TemplateParamMap  # noqa: E402,F401 — registra a tabela

logger = logging.getLogger("migrations.m010")


def run(engine=None):
    engine = engine or create_engine(DATABASE_URL)
    insp = inspect(engine)
    actions = []

    if "template_param_maps" in insp.get_table_names():
        actions.append("template_param_maps:already-present")
    else:
        # create_all com tables=[...] cria SO esta tabela: nenhuma outra tabela
        # do metadata e tocada, mesmo que exista drift em alguma delas.
        Base.metadata.create_all(bind=engine, tables=[TemplateParamMap.__table__])
        actions.append("template_param_maps:created")

    # Conferencia explicita do UNIQUE composto — sem ele duas linhas poderiam
    # mapear a MESMA posicao e o envio escolheria uma por acaso.
    uniques = {u["name"] for u in inspect(engine).get_unique_constraints("template_param_maps")}
    actions.append(
        "uq_template_param_maps_key:present"
        if "uq_template_param_maps_key" in uniques
        else "uq_template_param_maps_key:AUSENTE (verificar manualmente)"
    )

    # Bootstrap vazio e INTENCIONAL — ver docstring.
    with engine.connect() as conn:
        total = conn.exec_driver_sql("SELECT COUNT(*) FROM template_param_maps").scalar()
    actions.append(f"mapeamentos-existentes:{total} (bootstrap vazio e proposital)")

    return actions


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    safe = DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL
    print(f"[m010] alvo (conversas): {safe}")
    print("[m010] acoes:", run())
    print("[m010] OK (idempotente)")
