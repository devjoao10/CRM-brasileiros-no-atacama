"""
m017 — BIA-V2 Fase 6: tabela `lead_handoff_operations` (CRM).

Cria (aditivo, sem destruir nada):
  - tabela `lead_handoff_operations` — livro-razao de idempotencia de
    `POST /api/v2/lead-domain/handoff`. Uma linha por `event_id` ja aplicado.
      id              INTEGER/SERIAL PK
      event_id        VARCHAR(36)  NOT NULL  (forma canonica do UUID)
      lead_id         INTEGER      NOT NULL
      target_user_id  INTEGER      NOT NULL
      created_at      TIMESTAMP(TZ) DEFAULT CURRENT_TIMESTAMP
  - uq_lead_handoff_operations_event_id  UNIQUE (event_id)

O UNIQUE nao e enfeite: e o mecanismo da idempotencia. Sem ele o endpoint
aceita o mesmo `event_id` duas vezes e move o lead duas vezes.

O nome da tabela e do indice sao os MESMOS que
`app/models/lead_handoff_operation.py` declara, para que `create_all()` (bancos
novos) e esta migration (bancos existentes) emitam schema identico — mesmo
motivo documentado na m011 e na m013.

POR QUE m017 E NAO m016
-----------------------
A File Structure do plano reserva `migrations/m016_handoff_operations.py` para
a tabela do lado do CONVERSAS (`handoff_operations`, o estado da saga). As duas
fases correm em paralelo e, em producao, os dois servicos compartilham o MESMO
PostgreSQL: usar o numero e o nome do outro lado colidiria de verdade, nao so
no diff. Numeros nao precisam ser contiguos — cada script e independente e
idempotente.

SEM ForeignKey em `lead_id`/`target_user_id`, de proposito: livro-razao
append-only precisa sobreviver a delecao da linha que descreve (mesma razao
documentada na m013 para `conversation_events`). A existencia de lead e usuario
e verificada pelo endpoint, que devolve 404.

DDL PORTAVEL, NAO POR DIALETO
------------------------------
`CREATE TABLE IF NOT EXISTS` e `CREATE UNIQUE INDEX IF NOT EXISTS` tem sintaxe
IDENTICA em SQLite e PostgreSQL. Só o TIPO de `id` e de `created_at` muda por
dialeto (mesmo padrao de `_TYPE_BY_DIALECT` da m012/m013); o restante e uma
unica string.

ESTA MIGRATION E DO CRM, NAO DO CONVERSAS: ao contrario da m013, ela NAO
insere `conversas/` no `sys.path` e nao importa `app.*` — resolve o alvo so
pela env var, como a m011.

REVERSAO: remover a tabela (o CRM V1 nunca a referencia, entao a reversao e
total e sem efeito colateral). A operacao e manual, com backup e aprovacao.

Migration MANUAL e IDEMPOTENTE — **nao roda no startup**. Bancos novos ja
nascem completos via `Base.metadata.create_all()` no lifespan do CRM.

Uso (LOCAL / STAGING):
    DATABASE_URL=sqlite:///./crm.db python migrations/m017_lead_handoff_operations.py --allow-sqlite

Uso (PRODUCAO):
    DATABASE_URL=postgresql://... python migrations/m017_lead_handoff_operations.py

Codigos de saida: 0 = aplicado/no-op | 1 = recusa ou falha inesperada

PRODUCAO: somente apos backup verificado + aprovacao humana (migrations/README.md).
"""
import logging
import os
import sys

from sqlalchemy import create_engine, inspect, text

logger = logging.getLogger("migrations.m017")


class WrongTargetError(RuntimeError):
    """O alvo nao tem a tabela `leads`: banco errado, nao 'NO-OP'."""


_TABELA = "lead_handoff_operations"
_UNIQUE_EVENT_ID = "uq_lead_handoff_operations_event_id"

# Espelha as colunas do model — usado SO pelo gate pos-DDL, para provar
# presenca (nao tipo: ver `_verificar_pos_ddl`).
_COLUNAS_OBRIGATORIAS = ["id", "event_id", "lead_id", "target_user_id", "created_at"]

_ID_TYPE_BY_DIALECT = {"postgresql": "SERIAL", "default": "INTEGER"}
_TIMESTAMP_TYPE_BY_DIALECT = {"postgresql": "TIMESTAMP WITH TIME ZONE", "default": "TIMESTAMP"}


def run(engine=None, actions=None):
    """
    Devolve a lista de acoes aplicadas. Levanta `WrongTargetError` (alvo nao e
    este banco) ou `RuntimeError` (o mundo nao ficou como deveria depois do
    DDL) — nunca engole nada, nunca imprime OK sobre estado que nao verificou.

    `actions` e recebida de fora de proposito: se isto levantar no meio, o
    chamador ainda precisa saber QUAIS objetos ja foram aplicados.
    """
    if engine is None:
        url = os.getenv("DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError(
                "DATABASE_URL nao definida — este script nao adivinha alvo. "
                "Passe um engine ou exporte a variavel."
            )
        engine = create_engine(url)

    actions = [] if actions is None else actions
    insp = inspect(engine)

    if "leads" not in insp.get_table_names():
        # Mesmo gate da m011/m013: banco NOVO nasce completo pelo create_all()
        # e nao precisa deste script; banco EXISTENTE sem `leads` quer dizer
        # ALVO ERRADO, nao "nada a fazer".
        raise WrongTargetError(
            "[m017] RECUSADO — o alvo nao tem a tabela `leads`.\n"
            "[m017] Banco NOVO nasce completo pelo create_all() do CRM e nao\n"
            "[m017] precisa deste script. Banco EXISTENTE sem essa tabela quer\n"
            "[m017] dizer ALVO ERRADO (DATABASE_URL para outra base, nome trocado,\n"
            "[m017] replica vazia). Confira DATABASE_URL: aqui nao existe 'NO-OP'\n"
            "[m017] honesto."
        )

    dialect = engine.dialect.name
    ja_existia = _TABELA in insp.get_table_names()
    id_type = _ID_TYPE_BY_DIALECT.get(dialect, _ID_TYPE_BY_DIALECT["default"])
    timestamp_type = _TIMESTAMP_TYPE_BY_DIALECT.get(
        dialect, _TIMESTAMP_TYPE_BY_DIALECT["default"]
    )

    with engine.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS {_TABELA} ("
            f"    id {id_type} PRIMARY KEY,"
            "    event_id VARCHAR(36) NOT NULL,"
            "    lead_id INTEGER NOT NULL,"
            "    target_user_id INTEGER NOT NULL,"
            f"    created_at {timestamp_type} DEFAULT CURRENT_TIMESTAMP"
            ")"
        ))
        actions.append(f"{_TABELA}:{'already-present' if ja_existia else 'created'}")

        conn.execute(text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_UNIQUE_EVENT_ID} "
            f"ON {_TABELA} (event_id)"
        ))
        actions.append(f"{_UNIQUE_EVENT_ID}:ensured")

    return _verificar_pos_ddl(engine, actions)


def _verificar_pos_ddl(engine, actions):
    """
    Prova cada garantia que `run()` promete.

    `CREATE TABLE IF NOT EXISTS` e `CREATE UNIQUE INDEX IF NOT EXISTS` sao
    NO-OP SILENCIOSO contra uma tabela/indice PRE-EXISTENTE ou PARCIAL — so a
    reinspecao pos-DDL (via `inspect()`, nunca lendo de volta o que o proprio
    DDL declarou) pega essa lacuna. Checa presenca das colunas e presenca da
    UNIQUE sobre `event_id`. Nunca tenta provar equivalencia de TIPO entre
    SQLite e PostgreSQL, o que exigiria logica fragil por dialeto.

    Extraida de `run()` para poder ser exercida sozinha contra um SQLite
    descartavel criado por `create_all()`, sem chamar `run()`/`main()`.
    """
    insp_pos = inspect(engine)
    if _TABELA not in insp_pos.get_table_names():
        raise RuntimeError(f"{_TABELA} AUSENTE depois do DDL — migration abortada.")

    colunas_pos = {c["name"] for c in insp_pos.get_columns(_TABELA)}
    faltando = [c for c in _COLUNAS_OBRIGATORIAS if c not in colunas_pos]
    if faltando:
        raise RuntimeError(
            f"colunas ausentes em {_TABELA} depois do DDL: {faltando} — tabela "
            "pre-existente/parcial (CREATE TABLE IF NOT EXISTS foi NO-OP contra "
            "ela). Migration abortada."
        )
    actions.append("colunas:verificadas")

    indices_pos = insp_pos.get_indexes(_TABELA)
    uniques_pos = insp_pos.get_unique_constraints(_TABELA)
    tem_unique = any(
        ix.get("unique") and ix.get("column_names") == ["event_id"] for ix in indices_pos
    ) or any(uc.get("column_names") == ["event_id"] for uc in uniques_pos)
    if not tem_unique:
        raise RuntimeError(
            "UNIQUE em event_id AUSENTE depois do DDL — o mesmo handoff poderia "
            "ser aplicado duas vezes. Migration abortada."
        )
    actions.append(f"{_UNIQUE_EVENT_ID}:verificado")

    return actions


def _resolve_target(argv):
    """
    DATABASE_URL vem do AMBIENTE, nunca de `app.config`: os defaults de config
    apontam para arquivos SQLite locais e transformariam um deploy esquecido em
    "sucesso" contra um banco descartavel (defeito real de m005/m008/m009).
    """
    allow_sqlite = "--allow-sqlite" in argv
    url = os.getenv("DATABASE_URL", "").strip()

    if not url:
        return None, allow_sqlite, (
            "[m017] RECUSADO — DATABASE_URL nao esta definida.\n"
            "[m017] Exporte-a explicitamente; este script nao adivinha alvo.\n"
            "[m017]   DATABASE_URL=postgresql://... python migrations/m017_lead_handoff_operations.py"
        )
    if url.startswith("sqlite") and not allow_sqlite:
        return None, allow_sqlite, (
            f"[m017] RECUSADO — DATABASE_URL aponta para SQLite ({url}).\n"
            "[m017] Producao e PostgreSQL. Para rodar em dev/teste de proposito,\n"
            "[m017] passe --allow-sqlite."
        )
    return url, allow_sqlite, None


def main(argv):
    logging.basicConfig(level=logging.INFO)
    url, _allow_sqlite, refusal = _resolve_target(argv)
    if refusal:
        print(refusal)
        return 1

    safe = url.split("@")[-1] if "@" in url else url
    print(f"[m017] alvo (crm): {safe}")

    actions = []

    def report():
        for action in actions:
            print(f"[m017]   {action}")

    try:
        run(create_engine(url), actions)
    except WrongTargetError as exc:
        report()
        print(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 — qualquer coisa inesperada e falha RUIDOSA
        report()
        logger.exception("[m017] falha inesperada")
        print(f"[m017] FALHOU: {type(exc).__name__}: {exc}")
        return 1

    report()
    if any(":created" in a for a in actions):
        print("[m017] OK — tabela e indice aplicados (idempotente)")
    else:
        print("[m017] OK — NO-OP (ja estava aplicada)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
