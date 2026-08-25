"""
m011 — AUDIT-2026-08-W2E: as constraints que faltavam atras dos "PKs de mentira".

Cinco lugares do sistema tratavam um par de colunas como chave primaria em
Python (SELECT-entao-INSERT) enquanto o banco nao tinha constraint nenhuma
atras disso. Entre o SELECT e o INSERT cabe outra requisicao — e o resultado
sao linhas duplicadas que a aplicacao NAO sabe reconciliar.

Esta migration cria (aditivo, sem destruir nada) quatro INDICES UNICOS:

  uq_conversations_whatsapp                        conversations (whatsapp)
      F1 — find-or-create por numero em routers/webhook.py:367-378 e
      routers/conversations.py:362-386. Duas primeiras mensagens do mesmo
      numero chegando juntas criavam DUAS conversas; como todo leitor usa
      `.first()`, metade das mensagens do cliente caia numa thread invisivel.

  uq_funnel_entries_lead_funnel                    funnel_entries (lead_id, funnel_id)
      F2 — regra "um lead uma vez por funil" existia so como 409 em Python
      (routers/pipeline.py:574-592). Duplicata => `locate_lead` escolhe uma
      entry arbitrariamente e as duas derivam para etapas diferentes.

  uq_operational_card_assignees_card_user          operational_card_assignees (card_id, user_id)
      F3 — duplicata notifica em dobro para sempre e e IRREMOVIVEL pela API:
      `remove_assignee` apaga uma linha so.

  uq_operational_card_field_values_card_definition operational_card_field_values (card_id, definition_id)
      F4 — read-modify-write via `.first()` sem ORDER BY: o detalhe do card
      passa a alternar entre os dois valores.

E, SOMENTE no PostgreSQL, aplica os `server_default` de F5 em bancos que ja
existem (`ALTER COLUMN ... SET DEFAULT` — nunca toca uma linha sequer):
`leads.campos_personalizados`, `leads.status_venda`, `leads.is_active`,
`messages.send_attempts`. Sao colunas NOT NULL cujo default so existia na ORM,
entao todo INSERT vindo de psql/n8n/COPY era rejeitado. No SQLite este passo e
PULADO — o dialeto nao tem ALTER COLUMN; la o default chega pelo create_all.

O QUE ESTA MIGRATION SE RECUSA A FAZER
--------------------------------------
1. **Deduplicar.** Se encontrar linhas colidindo, ela NAO cria o indice e NAO
   apaga nada: imprime os ids e a chave que colidem e sai com codigo 2. Decidir
   qual das duas conversas do cliente e a boa e decisao de operador, nao de
   script. Reconcilie na mao e rode de novo.
2. **Rodar as cegas.** Recusa a rodar com DATABASE_URL ausente ou apontando
   para SQLite, a menos que venha `--allow-sqlite` explicito. As migrations
   m005/m008/m009 resolvem a URL pelo config do CONVERSAS, cujo default e um
   arquivo SQLite local: rodadas sem a env exportada elas migram um arquivo
   descartavel e imprimem sucesso. Este script nao repete isso.
3. **Mentir.** Qualquer excecao inesperada sai com codigo != 0 e a linha "OK"
   nunca e impressa. O m001 envolvia todo o DDL num unico `engine.begin()`
   engolindo excecao por statement: no PostgreSQL o primeiro erro aborta a
   transacao, tudo depois falha em silencio, o commit degrada para ROLLBACK e
   o script ainda imprime OK. Aqui cada objeto tem a SUA `engine.begin()`, de
   modo que uma falha no objeto 3 deixa 1 e 2 corretamente aplicados e o
   relatorio diz exatamente quais passaram.
4. **Fingir que verificou.** m009/m010 detectam UNIQUE ausente, concatenam a
   string ":AUSENTE (verificar manualmente)" na lista de acoes e saem com 0.
   Aqui uma verificacao que falha e falha.

POR QUE INDICE UNICO E NAO `ADD CONSTRAINT UNIQUE`
-------------------------------------------------
`CREATE UNIQUE INDEX IF NOT EXISTS` e a UNICA forma que existe, com a mesma
sintaxe e o mesmo nome, no SQLite e no PostgreSQL — e e exatamente o que os
models emitem via `Index(..., unique=True)`. Assim os DOIS donos de schema
deste sistema (o `create_all()` do startup e este script) produzem DDL
identico, que e a raiz de metade do drift auditado.

Nao se usa `CREATE UNIQUE INDEX CONCURRENTLY`: ele nao roda dentro de bloco
transacional, o que quebraria a garantia de transacao-por-objeto acima, e um
CONCURRENTLY que falha deixa um indice INVALID que so sai com DROP — proibido
aqui. As quatro tabelas sao pequenas (conversations tem ~81 linhas em
producao), entao o CREATE INDEX comum toma um lock BREVE de escrita na tabela
e acaba. Se algum dia uma delas crescer para milhoes de linhas, ai sim vale
pagar o custo do CONCURRENTLY + verificacao de `pg_index.indisvalid`.

Migration MANUAL e IDEMPOTENTE — **nao roda no startup**. Rodar de novo e
no-op e diz que foi no-op.

ESCOPO: as quatro tabelas vivem no MESMO PostgreSQL em producao (CRM e
Conversas compartilham o banco). Em dev, onde sao dois arquivos SQLite
distintos, rode uma vez por arquivo: tabela ausente e reportada como ausente e
ignorada, nunca como erro.

Uso (LOCAL / STAGING):
    DATABASE_URL=sqlite:///./crm.db python migrations/m011_audit_unique_constraints.py --allow-sqlite

Uso (PRODUCAO):
    DATABASE_URL=postgresql://... python migrations/m011_audit_unique_constraints.py

Codigos de saida: 0 = aplicado/no-op | 2 = duplicatas, reconciliar na mao
                  1 = recusa (URL ausente/SQLite sem flag) ou falha inesperada

PRODUCAO: somente apos backup verificado + aprovacao humana (migrations/README.md).
"""
import logging
import os
import sys

from sqlalchemy import create_engine, inspect, text

logger = logging.getLogger("migrations.m011")

# (nome do indice, tabela, colunas da chave, finding da auditoria)
_UNIQUE_TARGETS = [
    ("uq_conversations_whatsapp",
     "conversations", ("whatsapp",), "F1"),
    ("uq_funnel_entries_lead_funnel",
     "funnel_entries", ("lead_id", "funnel_id"), "F2"),
    ("uq_operational_card_assignees_card_user",
     "operational_card_assignees", ("card_id", "user_id"), "F3"),
    ("uq_operational_card_field_values_card_definition",
     "operational_card_field_values", ("card_id", "definition_id"), "F4"),
]

# F5 — (tabela, coluna, expressao do DEFAULT). SO PostgreSQL: SQLite nao tem
# ALTER COLUMN. SET DEFAULT afeta INSERTs futuros e NENHUMA linha existente.
_DEFAULT_TARGETS = [
    ("leads", "campos_personalizados", "'{}'"),
    ("leads", "status_venda", "'em_negociacao'"),
    ("leads", "is_active", "true"),
    ("messages", "send_attempts", "0"),
]


class DuplicateRowsError(RuntimeError):
    """Chave duplicada encontrada: o indice unico NAO pode ser criado."""


def _index_present(insp, table, index_name):
    """
    Procura o objeto nos DOIS lugares porque o mesmo nome pode existir como
    indice unico (o que este script cria) ou como constraint UNIQUE com indice
    de apoio homonimo (o que um `ADD CONSTRAINT` antigo teria criado). Qualquer
    um dos dois ja garante a unicidade — nao ha o que fazer.
    """
    if any(ix["name"] == index_name and ix.get("unique") for ix in insp.get_indexes(table)):
        return True
    return any(uc["name"] == index_name for uc in insp.get_unique_constraints(table))


def _find_duplicates(conn, table, cols):
    """
    Linhas cuja chave se repete. EXISTS correlacionado em vez de GROUP BY +
    agregacao de ids: `string_agg`/`group_concat` tem nomes diferentes por
    dialeto e este script precisa ser identico nos dois.

    Colunas NULL nao colidem em UNIQUE, e `d.c = t.c` tambem e falso para NULL
    — os dois criterios coincidem, entao nao ha falso positivo.
    """
    where = " AND ".join(f"d.{c} = t.{c}" for c in cols)
    sel = ", ".join(f"t.{c}" for c in cols)
    sql = (
        f"SELECT t.id, {sel} FROM {table} t "
        f"WHERE EXISTS (SELECT 1 FROM {table} d WHERE {where} AND d.id <> t.id) "
        f"ORDER BY {sel}, t.id"
    )
    return conn.execute(text(sql)).fetchall()


def _report_duplicates(table, cols, rows):
    """Relatorio para o operador: exatamente quais linhas colidem e em que chave."""
    lines = [
        f"[m011] ABORTADO — {table}: {len(rows)} linhas colidem em ({', '.join(cols)}).",
        "[m011] A migration NAO apaga nada: escolher qual linha sobrevive e",
        "[m011] decisao de negocio, nao de script. Linhas envolvidas:",
    ]
    for row in rows:
        key = ", ".join(f"{c}={v!r}" for c, v in zip(cols, row[1:]))
        lines.append(f"[m011]   id={row[0]}  ({key})")
    lines.append(f"[m011] Reconcilie manualmente e rode de novo: SELECT * FROM {table} WHERE id IN (...)")
    return "\n".join(lines)


def _apply_unique_indexes(engine, actions):
    dialect = engine.dialect.name
    tables = set(inspect(engine).get_table_names())

    for index_name, table, cols, finding in _UNIQUE_TARGETS:
        if table not in tables:
            # Banco novo (ou o outro SQLite de dev): o create_all cria a tabela
            # ja com o indice, vindo do `Index(unique=True)` no model.
            actions.append(f"{finding} {table}:table-absent (create_all cria ja com o indice)")
            continue

        insp = inspect(engine)  # reinspeciona: o cache nao ve o que acabamos de criar
        if _index_present(insp, table, index_name):
            actions.append(f"{finding} {index_name}:already-present")
            continue

        # Duplicatas ANTES do DDL: com elas o CREATE INDEX falharia com um erro
        # do driver que nao diz QUAIS linhas colidem — inutil para o operador.
        with engine.connect() as conn:
            dupes = _find_duplicates(conn, table, cols)
        if dupes:
            raise DuplicateRowsError(_report_duplicates(table, cols, dupes))

        # Transacao POR OBJETO (ver docstring): falha aqui nao desfaz os
        # indices ja criados nem mascara os proximos.
        with engine.begin() as conn:
            conn.execute(text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} "
                f"ON {table} ({', '.join(cols)})"
            ))

        # Conferencia REAL do que foi criado — nao um "verificar manualmente".
        if not _index_present(inspect(engine), table, index_name):
            raise RuntimeError(
                f"{index_name} nao existe depois do CREATE UNIQUE INDEX em {table} "
                f"(dialeto={dialect}). Nada foi revertido; investigue antes de repetir."
            )
        actions.append(f"{finding} {index_name}:created ({table}.{'+'.join(cols)})")


def _apply_server_defaults(engine, actions):
    if engine.dialect.name != "postgresql":
        actions.append("F5 server-defaults:skipped (SQLite nao tem ALTER COLUMN; vem do create_all)")
        return

    tables = set(inspect(engine).get_table_names())
    for table, column, default_sql in _DEFAULT_TARGETS:
        if table not in tables:
            actions.append(f"F5 {table}.{column}:table-absent")
            continue
        cols = {c["name"]: c for c in inspect(engine).get_columns(table)}
        if column not in cols:
            actions.append(f"F5 {table}.{column}:column-absent")
            continue
        if cols[column].get("default") is not None:
            actions.append(f"F5 {table}.{column}:already-has-default")
            continue
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} SET DEFAULT {default_sql}"))
        actions.append(f"F5 {table}.{column}:default-set ({default_sql})")


def run(engine, actions=None):
    """
    Devolve a lista de acoes. Levanta DuplicateRowsError (dados a reconciliar)
    ou RuntimeError (o mundo nao esta como deveria) — nunca engole nada.

    `actions` e recebida de fora de proposito: quando isto levanta no meio, o
    chamador ainda precisa saber QUAIS objetos ja foram aplicados. Um script que
    falha sem dizer o que ja fez obriga o operador a descobrir na mao.
    """
    actions = [] if actions is None else actions
    _apply_unique_indexes(engine, actions)
    _apply_server_defaults(engine, actions)
    return actions


def _resolve_target(argv):
    """
    DATABASE_URL vem do AMBIENTE, nunca de app.config: os defaults de config
    apontam para arquivos SQLite locais e transformariam um deploy esquecido
    em "sucesso" contra um banco descartavel (defeito real de m005/m008/m009).
    """
    allow_sqlite = "--allow-sqlite" in argv
    url = os.getenv("DATABASE_URL", "").strip()

    if not url:
        return None, allow_sqlite, (
            "[m011] RECUSADO — DATABASE_URL nao esta definida.\n"
            "[m011] Exporte-a explicitamente; este script nao adivinha alvo.\n"
            "[m011]   DATABASE_URL=postgresql://... python migrations/m011_audit_unique_constraints.py"
        )
    if url.startswith("sqlite") and not allow_sqlite:
        return None, allow_sqlite, (
            f"[m011] RECUSADO — DATABASE_URL aponta para SQLite ({url}).\n"
            "[m011] Producao e PostgreSQL. Para rodar em dev/teste de proposito,\n"
            "[m011] passe --allow-sqlite."
        )
    return url, allow_sqlite, None


def main(argv):
    logging.basicConfig(level=logging.INFO)
    url, _allow_sqlite, refusal = _resolve_target(argv)
    if refusal:
        print(refusal)
        return 1

    safe = url.split("@")[-1] if "@" in url else url
    print(f"[m011] alvo: {safe}")

    actions = []

    def report():
        for action in actions:
            print(f"[m011]   {action}")

    try:
        run(create_engine(url), actions)
    except DuplicateRowsError as exc:
        report()  # o que ja passou fica aplicado — o operador precisa saber o que e
        print(str(exc))
        return 2
    except Exception as exc:  # noqa: BLE001 — qualquer coisa inesperada e falha RUIDOSA
        report()
        logger.exception("[m011] falha inesperada")
        print(f"[m011] FALHOU: {type(exc).__name__}: {exc}")
        return 1

    report()

    changed = [a for a in actions if ":created" in a or ":default-set" in a]
    if changed:
        print(f"[m011] OK — {len(changed)} objeto(s) aplicado(s) (idempotente)")
    else:
        print("[m011] OK — NO-OP (nada a fazer; ja estava tudo aplicado)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
