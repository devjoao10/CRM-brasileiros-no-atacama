"""
m012 — AUDIT-2026-08-WA: separa "atribuido a um atendente" de "atendido por ele".

Adiciona (aditivo, sem destruir nada):
  - conversations.primeira_resposta_humana_at  TIMESTAMP(TZ) NULL

POR QUE ESTA COLUNA EXISTE
--------------------------
Ate aqui o inbox classificava a conversa por `atendente_id IS NULL`: sem
atendente = fila, com atendente = "meus atendimentos". Isso torna *atribuir*
sinonimo de *atender*, e a regra operacional real e o contrario:

    a conversa continua na FILA DE ESPERA enquanto NENHUM humano tiver
    respondido, mesmo que ela ja tenha dono.

Abrir a conversa, visualizar, outro atendente abrir — nada disso e atendimento.
O evento que encerra a espera e a PRIMEIRA MENSAGEM OUTBOUND HUMANA. Como
`messages` nao guarda autoria (nao ha `sender_user_id`; Bia, auto-resposta e
humano passam pelo mesmo `record_outbound_message`), o instante e gravado aqui,
na conversa, pelas rotas que sabem quem e o `current_user`.

BACKFILL — criterio conservador e declarado
-------------------------------------------
Nao existe autoria historica: nao da para saber se a mensagem outbound antiga
foi da Bia ou de um humano. Marcar demais tiraria conversas da fila que ainda
esperam; marcar de menos devolve para a fila conversas ja atendidas — e o
segundo erro e recuperavel (o atendente responde e ela sai), o primeiro nao
(o cliente fica invisivel).

Por isso so recebem `primeira_resposta_humana_at` as conversas que satisfazem
TODAS as condicoes abaixo — o estado em que o sistema antigo ja as considerava
"em atendimento", nao "na fila":

  - status aberto ('aberta' ou 'aguardando')
  - is_bot_active = FALSE          (fora do universo da Bia)
  - atendente_id IS NOT NULL       (alguem assumiu de fato)
  - existe pelo menos uma linha em `messages` com direction = 'outbound'

O valor gravado e `conversations.created_at` (nao inventamos o instante da
resposta; so precisamos de "nao e NULL"). O `WHERE primeira_resposta_humana_at
IS NULL` torna o backfill idempotente.

Conversas na fila ficam com NULL — que e exatamente o que a nova regra quer.

E o backfill ZERA `queued_at` nas linhas que marca (AUDIT-2026-08-WF2).
`aplicar_estado_humano` (conversas/app/services/atendimento.py) declara um
invariante unico: `primeira_resposta_humana_at NOT NULL` => `queued_at NULL`.
Gravar so a primeira metade produzia em massa o estado que o codigo trata como
impossivel — conversa "ja atendida" ocupando lugar na FILA DE ESPERA. A
reconciliacao e um UPDATE SEPARADO, e nao um `SET` a mais no backfill, porque
tambem precisa consertar as linhas que uma rodada ANTERIOR desta migration ja
deixou assim: o `WHERE primeira_resposta_humana_at IS NULL` que torna o backfill
idempotente e exatamente o que as excluiria.

O QUE ESTA MIGRATION SE RECUSA A FAZER
--------------------------------------
**Dizer "NO-OP" sobre um banco que nao e este** (AUDIT-2026-08-WF2). O gate de
`_resolve_target` so olha a STRING da URL — recusa URL ausente e SQLite, e nunca
abriu o banco para conferir o alvo. Apontada para um PostgreSQL vazio (base
recem-provisionada, nome de banco trocado, replica vazia), ela imprimia
"conversations:table-absent" seguido de "OK — NO-OP (ja estava aplicada)" e
saia 0. O operador marcava o runbook como feito e producao seguia sem
`primeira_resposta_humana_at` — com o inbox classificando TODA conversa como
"na fila". Sem a tabela `conversations` nao ha o que verificar: recusa.

Migration MANUAL e IDEMPOTENTE — **nao roda no startup**. Bancos novos ja
nascem completos via `Base.metadata.create_all()` no lifespan do Conversas.

ATENCAO — este e o app CONVERSAS (nao o CRM): o script insere `conversas/` no
inicio do sys.path para que `app.*` resolva para conversas/app. Deve rodar em
PROCESSO PROPRIO, nunca importado junto das migrations do CRM.

Ordem em bancos antigos: m003 -> ... -> m008 -> m011 -> m012.

Uso (LOCAL / STAGING):
    DATABASE_URL=postgresql://... python migrations/m012_conversas_primeira_resposta_humana.py
    python migrations/m012_conversas_primeira_resposta_humana.py --allow-sqlite

PRODUCAO: somente apos backup verificado + aprovacao humana (migrations/README.md).
"""
import logging
import os
import pathlib
import sys

# `app.*` deve resolver para conversas/app — ver docstring.
_CONVERSAS_DIR = pathlib.Path(__file__).resolve().parent.parent / "conversas"
sys.path.insert(0, str(_CONVERSAS_DIR))

from sqlalchemy import create_engine, inspect, text  # noqa: E402

logger = logging.getLogger("migrations.m012")


class WrongTargetError(RuntimeError):
    """O alvo nao tem a tabela `conversations`: banco errado, nao 'NO-OP'."""


_COLUMN = "primeira_resposta_humana_at"
_TYPE_BY_DIALECT = {"postgresql": "TIMESTAMP WITH TIME ZONE", "default": "TIMESTAMP"}
_INDEX = "ix_conversations_primeira_resposta_humana_at"


def run(engine=None, actions=None):
    """
    Devolve a lista de acoes aplicadas. Levanta RuntimeError se o mundo nao
    ficou como deveria depois do DDL — nunca engole nada, nunca imprime OK
    sobre um estado que nao verificou (defeito real de m009/m010, ver b6e97dd).

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

    if "conversations" not in insp.get_table_names():
        # AUDIT-2026-08-WF2 — ver docstring. Banco NOVO nasce completo pelo
        # create_all() e nao precisa deste script; banco EXISTENTE sem
        # `conversations` quer dizer ALVO ERRADO, e nao "nada a fazer".
        raise WrongTargetError(
            "[m012] RECUSADO — o alvo nao tem a tabela `conversations`.\n"
            "[m012] Banco NOVO nasce completo pelo create_all() do Conversas e nao\n"
            "[m012] precisa deste script. Banco EXISTENTE sem essa tabela quer dizer\n"
            "[m012] ALVO ERRADO (DATABASE_URL para outra base, nome trocado, replica\n"
            "[m012] vazia). Confira DATABASE_URL: aqui nao existe 'NO-OP' honesto."
        )

    dialect = engine.dialect.name
    existing = {c["name"] for c in insp.get_columns("conversations")}

    with engine.begin() as conn:
        if _COLUMN in existing:
            actions.append(f"{_COLUMN}:already-present")
        else:
            ddl_type = _TYPE_BY_DIALECT.get(dialect, _TYPE_BY_DIALECT["default"])
            conn.execute(text(f"ALTER TABLE conversations ADD COLUMN {_COLUMN} {ddl_type}"))
            actions.append(f"{_COLUMN}:added ({ddl_type})")

        conn.execute(
            text(f"CREATE INDEX IF NOT EXISTS {_INDEX} ON conversations ({_COLUMN})")
        )
        actions.append(f"{_INDEX}:ensured")

        # Backfill conservador — criterio na docstring. Idempotente pelo
        # `IS NULL`: rodar de novo nao remarca nem sobrescreve nada.
        result = conn.execute(
            text(
                f"UPDATE conversations SET {_COLUMN} = created_at "
                f"WHERE {_COLUMN} IS NULL "
                "  AND status IN ('aberta', 'aguardando') "
                "  AND is_bot_active = :off "
                "  AND atendente_id IS NOT NULL "
                "  AND EXISTS (SELECT 1 FROM messages m "
                "              WHERE m.conversation_id = conversations.id "
                "                AND m.direction = 'outbound')"
            ),
            {"off": False},
        )
        actions.append(f"backfill-em-atendimento:{result.rowcount}")

        # AUDIT-2026-08-WF2 — segunda metade do invariante (ver docstring):
        # quem ja tem `primeira_resposta_humana_at` NAO espera mais na fila.
        # Separado do backfill de proposito: tambem conserta o que a versao
        # anterior desta migration deixou no estado impossivel.
        if "queued_at" in existing:
            fila = conn.execute(text(
                f"UPDATE conversations SET queued_at = NULL "
                f"WHERE {_COLUMN} IS NOT NULL AND queued_at IS NOT NULL"
            ))
            actions.append(f"fila-consistente:{fila.rowcount}")
        else:
            # m008 e pre-requisito declarado. Dizer isso e melhor do que
            # abortar o ALTER inteiro com "no such column: queued_at".
            actions.append("fila-consistente:PULADO (queued_at ausente; rode a m008 antes)")

    # GATE pos-DDL: nao basta o ALTER nao ter levantado. Reinspeciona.
    insp_pos = inspect(engine)
    if _COLUMN not in {c["name"] for c in insp_pos.get_columns("conversations")}:
        raise RuntimeError(
            f"{_COLUMN} AUSENTE depois do DDL — o inbox classificaria toda "
            "conversa como 'na fila'. Migration abortada."
        )
    actions.append(f"{_COLUMN}:verificado")
    return actions


def _resolve_target(argv):
    """
    DATABASE_URL vem do AMBIENTE, nunca de app.config: os defaults de config
    apontam para arquivos SQLite locais e transformariam um deploy esquecido em
    "sucesso" contra um banco descartavel (defeito real de m005/m008/m009).
    """
    allow_sqlite = "--allow-sqlite" in argv
    url = os.getenv("DATABASE_URL", "").strip()

    if not url and allow_sqlite:
        # Modo dev explicito: cai no DATABASE_URL do proprio Conversas.
        from app.config import DATABASE_URL as CONVERSAS_URL  # noqa: E402

        return CONVERSAS_URL, allow_sqlite, None
    if not url:
        return None, allow_sqlite, (
            "[m012] RECUSADO — DATABASE_URL nao esta definida.\n"
            "[m012] Exporte-a explicitamente; este script nao adivinha alvo.\n"
            "[m012]   DATABASE_URL=postgresql://... python migrations/m012_conversas_primeira_resposta_humana.py"
        )
    if url.startswith("sqlite") and not allow_sqlite:
        return None, allow_sqlite, (
            f"[m012] RECUSADO — DATABASE_URL aponta para SQLite ({url}).\n"
            "[m012] Producao e PostgreSQL. Para rodar em dev/teste de proposito,\n"
            "[m012] passe --allow-sqlite."
        )
    return url, allow_sqlite, None


def main(argv):
    logging.basicConfig(level=logging.INFO)
    url, _allow_sqlite, refusal = _resolve_target(argv)
    if refusal:
        print(refusal)
        return 1

    safe = url.split("@")[-1] if "@" in url else url
    print(f"[m012] alvo (conversas): {safe}")

    actions = []

    def report():
        for action in actions:
            print(f"[m012]   {action}")

    try:
        run(create_engine(url), actions)
    except WrongTargetError as exc:
        report()
        print(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 — qualquer coisa inesperada e falha RUIDOSA
        report()
        logger.exception("[m012] falha inesperada")
        print(f"[m012] FALHOU: {type(exc).__name__}: {exc}")
        return 1

    report()
    # AUDIT-2026-08-WF2 — "NO-OP" so pode ser impresso quando NADA mudou: uma
    # rodada que backfillou ou reconciliou a fila escreveu dado, mesmo com a
    # coluna ja presente. Toda acao termina em ":<n>" quando mexeu em linhas.
    escritas = sum(int(v) for _, _, v in (a.rpartition(":") for a in actions) if v.isdigit())
    if any(":added" in a for a in actions):
        print("[m012] OK — coluna aplicada (idempotente)")
    elif escritas:
        print(f"[m012] OK — {escritas} linha(s) reconciliada(s) (idempotente)")
    else:
        print("[m012] OK — NO-OP (ja estava aplicada)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
