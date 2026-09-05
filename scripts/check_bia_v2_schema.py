#!/usr/bin/env python
"""
check_bia_v2_schema.py — gate de PRONTIDAO de schema para BIA V2 (risco R3).

Rodado por um HUMANO antes de virar `V2_MODE=live` (conversas/app/config.py).
So faz INTROSPECCAO read-only via `sqlalchemy.inspect()` — nunca executa DDL,
nunca chama nenhum script de `migrations/mNNN_*.py`, nunca escreve no banco.
Confirma que o(s) banco(s) real(is) ja tem TODOS os objetos que a V2 precisa:
tabelas, colunas, UNIQUE, CHECK e o indice parcial `ix_handoff_operations_pendentes`
na forma NULLS FIRST corrigida pela m020 (R15/R16 dependem dele para reclamar
trabalho pendente sem Seq Scan a cada ciclo — ver o docstring da m020).

TOPOLOGIA DE PRODUCAO — UM banco, nao dois
--------------------------------------------
`docker-compose.yml` mostra CRM e Conversas com a MESMA `DATABASE_URL` literal
(`postgresql://.../crm_atacama` no mesmo host `postgres:5432`, mesmo usuario) —
os dois servicos compartilham o MESMO catalogo PostgreSQL em producao (ja
documentado em `app/models/lead_handoff_operation.py` e nas migrations
m011/m013/m017). Por isso este script usa, por padrao, UMA UNICA URL para os
dois lados: `--database-url`/`DATABASE_URL`. `--conversas-database-url`/
`CONVERSAS_DATABASE_URL` existem so para o caso — nao usado em producao hoje —
de alguem apontar os dois servicos para bancos separados; se omitido, cai na
mesma URL do lado CRM.

O QUE E VERIFICADO (nunca inventado — cada item vem da migration que o criou)
------------------------------------------------------------------------------
Conversas:
  - tabela `conversation_events` (m013): colunas + UNIQUE(event_id) +
    indices `ix_conversation_events_conversation_id`/`_created_at`.
  - tabela `conversations` (m014/m015): colunas `state`/`state_version`/
    `closed_at`; CHECK `ck_conversations_estado_humano` (decisao 5 da Fase 4).
  - tabela `handoff_operations` (m016/m019/m020): colunas, UNIQUE(event_id),
    CHECK `ck_handoff_operations_status`, indice parcial
    `ix_handoff_operations_pendentes` — colunas `[next_attempt_at, id]` NOS
    DOIS dialetos (m020), e no PostgreSQL especificamente `NULLS FIRST` de
    verdade no catalogo (`pg_indexes.indexdef`), nao so a forma superada da
    m019 (que teria as colunas certas mas NULLS LAST implicito).

CRM:
  - tabela `lead_handoff_operations` (m017/m018): colunas + UNIQUE(event_id).

O que NAO e verificado, DE PROPOSITO — mesma fronteira que TODA migration
desta familia (m013/m014/m016/m017/m019) ja declara: nunca tenta provar
equivalencia de TIPO de coluna entre SQLite e PostgreSQL (exigiria logica
fragil por dialeto), nem valores de DEFAULT (fora do escopo deste gate de
prontidao — quem precisa disso e o gate DA PROPRIA migration, nao este).

MECANISMO CAS (`app/routers/v2_lead_domain.py`) — NENHUM indice extra
------------------------------------------------------------------------
`_carregar_lead_sob_lock` faz `SELECT ... FOR UPDATE` filtrando por
`Lead.id == lead_id` — busca pela CHAVE PRIMARIA de `leads`, que existe
estruturalmente desde que a tabela exista (nao e um objeto que uma migration
desta familia acrescenta, e portanto nao entra na lista de itens verificaveis
aqui). O CAS depende do UNIQUE em `lead_handoff_operations.event_id` (m017,
ja verificado acima) para detectar colisao via `IntegrityError` — nenhum
indice adicional alem dos ja listados.

Uso
---
    DATABASE_URL=postgresql://... python scripts/check_bia_v2_schema.py
    python scripts/check_bia_v2_schema.py --database-url postgresql://...
    python scripts/check_bia_v2_schema.py --allow-sqlite   # so dev/teste local

Saida (stdout) e codigo de saida:
    READY   -> todos os itens presentes                          -> exit 0
    BLOCKED -> lista item a item o que falta                      -> exit 1
    erro de uso/conexao (URL ausente, banco inalcancavel)          -> exit 2

Nunca imprime a URL de conexao com credencial em claro — so host/porta/nome do
banco (senha sempre mascarada; ver `_mask`).
"""
import argparse
import os
import sys
from typing import Literal, Sequence
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.reflection import Inspector


# ─── Mascaramento de credenciais (mesmo idioma de scripts/check_readonly_db.py) ───
def _mask(url: str) -> str:
    """Retorna a URL com a senha substituida por '***'. Nunca vaza credencial.

    Duas defesas contra URL malformada (por exemplo, um '://' digitado errado):
    1. Se `urlsplit()` nao reconhecer NEM scheme NEM netloc, a string inteira
       cai em `path` sem estrutura nenhuma — nao confia em `urlunsplit()`.
    2. Mesmo com um scheme reconhecido, `urlsplit()` so preenche `netloc` (e
       portanto `password`) quando o restante comeca com exatamente '//'. Uma
       URL com UMA barra faltando (`postgresql:/user:senha@host/db`) tem
       scheme reconhecido mas netloc vazio — a credencial cai em `path` e
       sairia sem mascarar. Detecta isso checando se ha um '@' fora da parte
       de query/fragment que NAO esta dentro do netloc que conseguimos
       reconhecer (query/fragment sao excluidos do check porque um '@' ali
       nao pode ser credencial por sintaxe de URI, e excluir evita falso
       positivo desnecessario numa URL legitima).
    """
    if not url:
        return "(vazia)"
    try:
        parts = urlsplit(url)
        sem_query_nem_fragment = url.split("?", 1)[0].split("#", 1)[0]
        netloc_reconhecido = parts.netloc or ""
        if (not parts.scheme and not parts.netloc) or (
            "@" in sem_query_nem_fragment and "@" not in netloc_reconhecido
        ):
            raise ValueError("URL malformada — credencial pode nao estar no netloc reconhecido")
        if parts.password:
            netloc = parts.netloc.replace(f":{parts.password}@", ":***@", 1)
            parts = parts._replace(netloc=netloc)
        return urlunsplit(parts)
    except Exception:
        # Fallback conservador: nao arriscar vazar — corta no '@'.
        return url.split("@")[-1] if "@" in url else "(url nao parseavel)"


# ─── Contrato de cada tabela — nomes/colunas SEMPRE copiados da migration que ───
# ─── os introduziu (nunca inventados). Ver o docstring do modulo para a fonte. ───

_CONVERSATION_EVENTS_TABELA = "conversation_events"
_CONVERSATION_EVENTS_COLUNAS = (
    "id", "event_id", "event_type", "conversation_id", "lead_id", "message_id",
    "whatsapp_msg_id", "state_before", "state_after", "action", "target_user_id",
    "model", "model_attempt", "duration_ms", "result", "error_code", "payload",
    "created_at",
)  # m013
_CONVERSATION_EVENTS_UNIQUE_EVENT_ID = ["event_id"]
_CONVERSATION_EVENTS_INDICES = {
    "ix_conversation_events_conversation_id": ["conversation_id"],
    "ix_conversation_events_created_at": ["created_at"],
}  # m013

_CONVERSATIONS_TABELA = "conversations"
_CONVERSATIONS_COLUNAS_V2 = ("state", "state_version", "closed_at")  # m014
_CONVERSATIONS_CHECK_ESTADO_HUMANO = "ck_conversations_estado_humano"  # m015
_CONVERSATIONS_CHECK_ESTADO_HUMANO_TOKENS = (
    "WAITING_HUMAN", "HUMAN_ACTIVE", "is_bot_active", "atendente_id",
    "responsavel_id", "queued_at",
)  # m015 — mesmos 6 tokens que `_verificar_pos_ddl` da m015 exige

_HANDOFF_OPERATIONS_TABELA = "handoff_operations"
_HANDOFF_OPERATIONS_COLUNAS = (
    "id", "event_id", "conversation_id", "target_user_id", "status", "error_code",
    "created_at", "updated_at",  # m016
    "expected_responsavel_id", "attempt_count", "next_attempt_at", "reconciled_at",
    "error_class",  # m019
)
_HANDOFF_OPERATIONS_UNIQUE_EVENT_ID = ["event_id"]  # m016
_HANDOFF_OPERATIONS_CHECK_STATUS = "ck_handoff_operations_status"  # m016
_HANDOFF_OPERATIONS_CHECK_STATUS_TOKENS = ("REQUESTED", "COMPLETED", "FAILED")  # m016
_HANDOFF_OPERATIONS_INDICE_PENDENTES = "ix_handoff_operations_pendentes"  # m019, colunas corrigidas pela m020
_HANDOFF_OPERATIONS_INDICE_PENDENTES_COLUNAS = ["next_attempt_at", "id"]  # m020, nos DOIS dialetos

_LEAD_HANDOFF_OPERATIONS_TABELA = "lead_handoff_operations"
_LEAD_HANDOFF_OPERATIONS_COLUNAS = (
    "id", "event_id", "lead_id", "target_user_id", "created_at",  # m017
    "previous_responsavel_id",  # m018
)
_LEAD_HANDOFF_OPERATIONS_UNIQUE_EVENT_ID = ["event_id"]  # m017


# ─── Helpers de introspeccao — genericos, reusados por toda checagem abaixo ───

def _tabela_ausente(insp: Inspector, tabela: str) -> bool:
    return tabela not in insp.get_table_names()


def _colunas_faltando(insp: Inspector, tabela: str, obrigatorias) -> list:
    existentes = {c["name"] for c in insp.get_columns(tabela)}
    return [f"missing column: {tabela}.{c}" for c in obrigatorias if c not in existentes]


def _tem_unique(insp: Inspector, tabela: str, colunas: list) -> bool:
    """Tolerante por FORMA (indice unico OU constraint unique), nao por nome —
    mesmo criterio que `_verificar_pos_ddl` de m013/m016/m017/m018 ja usa."""
    indices = insp.get_indexes(tabela)
    uniques = insp.get_unique_constraints(tabela)
    return (
        any(ix.get("unique") and ix.get("column_names") == colunas for ix in indices)
        or any(uc.get("column_names") == colunas for uc in uniques)
    )


def _extrair_check_de_ddl_sqlite(ddl: str, nome: str):
    """Extrai o texto ENTRE OS PARENTESES do `CHECK (...)` associado a
    `CONSTRAINT <nome>` dentro do DDL bruto (`sqlite_master.sql`), usando
    contagem de parenteses BALANCEADA — a expressao real tem parenteses
    aninhados, e um regex nao-guloso simples pararia no PRIMEIRO ')',
    cortando a expressao pela metade. Devolve `None` se o marcador nao
    aparecer no DDL."""
    marcador = f"CONSTRAINT {nome}"
    pos = ddl.find(marcador)
    if pos == -1:
        return None
    pos_check = ddl.find("CHECK", pos)
    if pos_check == -1:
        return None
    pos_abre = ddl.find("(", pos_check)
    if pos_abre == -1:
        return None
    profundidade = 0
    for i in range(pos_abre, len(ddl)):
        if ddl[i] == "(":
            profundidade += 1
        elif ddl[i] == ")":
            profundidade -= 1
            if profundidade == 0:
                return ddl[pos_abre + 1:i]
    return None


def _buscar_check_constraint(insp: Inspector, engine: Engine, tabela: str, nome: str) -> dict | None | Literal["unsupported"]:
    """Devolve o dict da constraint pelo NOME, `None` se ausente, ou o sentinel
    "unsupported" se o dialeto nao reflete CHECK nenhum.

    FALLBACK SQLITE, e por que ele existe: o dialeto SQLite do SQLAlchemy
    reflete CHECK constraints com um parser proprio sobre o DDL bruto de
    `sqlite_master.sql` — e esse parser devolve LISTA VAZIA quando a expressao
    do CHECK contem quebra de linha (confirmado empiricamente contra este
    SQLAlchemy: uma `CheckConstraint` IDENTICA a de
    `conversas/app/models/conversation.py` — cuja `_CHECK_ESTADO_HUMANO_SQL` e
    uma string Python multi-linha — some de `get_check_constraints()` mesmo
    com `CONSTRAINT <nome> CHECK (...)` presente e valido no DDL). Sem este
    fallback, este gate reportaria BLOCKED contra um schema SQLite legitimo
    construido pelo PROPRIO model real — falso negativo, nao falso positivo,
    mas ainda assim errado. PostgreSQL nao tem este problema (reflete pelo
    catalogo `pg_constraint`, insensivel a formatacao) — o fallback so
    dispara no SQLite, e so quando a reflexao estruturada veio vazia."""
    try:
        constraints = insp.get_check_constraints(tabela)
    except NotImplementedError:
        return "unsupported"
    for c in constraints:
        if c.get("name") == nome:
            return c
    if engine.dialect.name != "sqlite":
        return None
    with engine.connect() as conn:
        ddl = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:t"),
            {"t": tabela},
        ).scalar()
    if not ddl:
        return None
    expressao = _extrair_check_de_ddl_sqlite(ddl, nome)
    if expressao is None:
        return None
    return {"name": nome, "sqltext": expressao}


class _DialectoNaoSuportaCheckConstraint(Exception):
    """Sinaliza que o dialeto do banco nao suporta reflexao de CHECK constraint.

    NAO significa "constraint confirmada ausente" (isso seria BLOCKED, exit 1)
    — significa "nao foi possivel verificar" (exit 2, ver contrato de saida no
    docstring do modulo). `main()` precisa mapear esta excecao para exit 2 e
    NUNCA deixa-la cair na lista `faltando` tratada como BLOCKED.
    """


def _tokens_faltando_no_check(
    constraint: dict | None | Literal["unsupported"],
    tokens_obrigatorios: Sequence[str],
    nome_constraint: str,
    tabela: str,
) -> list:
    if constraint == "unsupported":
        raise _DialectoNaoSuportaCheckConstraint(
            f"cannot verify check constraint: {nome_constraint} on {tabela} "
            f"(dialect does not support check-constraint reflection)"
        )
    if constraint is None:
        return [f"missing check constraint: {nome_constraint} on {tabela}"]
    expressao = str(constraint.get("sqltext") or "")
    faltando = [t for t in tokens_obrigatorios if t not in expressao]
    if faltando:
        return [
            f"check constraint {nome_constraint} on {tabela} exists but its expression is "
            f"missing token(s) {faltando} (found: {expressao!r})"
        ]
    return []


# ─── Um grupo de checagem por tabela ───

def check_conversation_events(insp: Inspector) -> list[str]:
    if _tabela_ausente(insp, _CONVERSATION_EVENTS_TABELA):
        return [f"missing table: {_CONVERSATION_EVENTS_TABELA}"]

    faltando = _colunas_faltando(insp, _CONVERSATION_EVENTS_TABELA, _CONVERSATION_EVENTS_COLUNAS)

    if not _tem_unique(insp, _CONVERSATION_EVENTS_TABELA, _CONVERSATION_EVENTS_UNIQUE_EVENT_ID):
        faltando.append(
            f"missing unique constraint: {_CONVERSATION_EVENTS_TABELA}(event_id) "
            f"(expected uq_conversation_events_event_id)"
        )

    indices_por_nome = {ix.get("name"): ix for ix in insp.get_indexes(_CONVERSATION_EVENTS_TABELA)}
    for nome, colunas in _CONVERSATION_EVENTS_INDICES.items():
        indice = indices_por_nome.get(nome)
        if indice is None:
            faltando.append(f"missing index: {nome} on {_CONVERSATION_EVENTS_TABELA}({','.join(colunas)})")
        elif indice.get("column_names") != colunas:
            faltando.append(
                f"wrong index columns: {nome} expected {colunas}, found {indice.get('column_names')}"
            )
    return faltando


def check_conversations(insp: Inspector, engine: Engine) -> list[str]:
    if _tabela_ausente(insp, _CONVERSATIONS_TABELA):
        return [f"missing table: {_CONVERSATIONS_TABELA}"]

    faltando = _colunas_faltando(insp, _CONVERSATIONS_TABELA, _CONVERSATIONS_COLUNAS_V2)

    constraint = _buscar_check_constraint(insp, engine, _CONVERSATIONS_TABELA, _CONVERSATIONS_CHECK_ESTADO_HUMANO)
    faltando += _tokens_faltando_no_check(
        constraint, _CONVERSATIONS_CHECK_ESTADO_HUMANO_TOKENS,
        _CONVERSATIONS_CHECK_ESTADO_HUMANO, _CONVERSATIONS_TABELA,
    )
    return faltando


def check_handoff_operations(insp: Inspector, engine: Engine) -> list[str]:
    if _tabela_ausente(insp, _HANDOFF_OPERATIONS_TABELA):
        return [f"missing table: {_HANDOFF_OPERATIONS_TABELA}"]

    faltando = _colunas_faltando(insp, _HANDOFF_OPERATIONS_TABELA, _HANDOFF_OPERATIONS_COLUNAS)

    if not _tem_unique(insp, _HANDOFF_OPERATIONS_TABELA, _HANDOFF_OPERATIONS_UNIQUE_EVENT_ID):
        faltando.append(
            f"missing unique constraint: {_HANDOFF_OPERATIONS_TABELA}(event_id) "
            f"(expected uq_handoff_operations_event_id)"
        )

    constraint = _buscar_check_constraint(insp, engine, _HANDOFF_OPERATIONS_TABELA, _HANDOFF_OPERATIONS_CHECK_STATUS)
    faltando += _tokens_faltando_no_check(
        constraint, _HANDOFF_OPERATIONS_CHECK_STATUS_TOKENS,
        _HANDOFF_OPERATIONS_CHECK_STATUS, _HANDOFF_OPERATIONS_TABELA,
    )

    dialect = engine.dialect.name
    indices_por_nome = {ix.get("name"): ix for ix in insp.get_indexes(_HANDOFF_OPERATIONS_TABELA)}
    indice = indices_por_nome.get(_HANDOFF_OPERATIONS_INDICE_PENDENTES)
    if indice is None:
        faltando.append(
            f"missing index: {_HANDOFF_OPERATIONS_INDICE_PENDENTES} on {_HANDOFF_OPERATIONS_TABELA} "
            f"(expected columns {_HANDOFF_OPERATIONS_INDICE_PENDENTES_COLUNAS}, partial WHERE "
            f"status IN ('REQUESTED','FAILED'))"
        )
        return faltando  # sem o indice, nao ha o que reinspecionar abaixo

    colunas_indice = indice.get("column_names")
    if colunas_indice != _HANDOFF_OPERATIONS_INDICE_PENDENTES_COLUNAS:
        faltando.append(
            f"wrong index columns: {_HANDOFF_OPERATIONS_INDICE_PENDENTES} expected "
            f"{_HANDOFF_OPERATIONS_INDICE_PENDENTES_COLUNAS}, found {colunas_indice} — provavelmente a "
            f"forma SUPERADA da m019 (sem a coluna `id` de desempate); rode a m020"
        )

    opts = indice.get("dialect_options") or {}
    chave_where = f"{dialect}_where"
    if chave_where not in opts:
        faltando.append(
            f"index {_HANDOFF_OPERATIONS_INDICE_PENDENTES} exists but is not a PARTIAL index "
            f"({chave_where!r} missing from dialect_options) — would cover ALL rows instead of only "
            f"REQUESTED/FAILED"
        )

    if dialect == "postgresql":
        with engine.connect() as conn:
            indexdef = conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :nome"),
                {"nome": _HANDOFF_OPERATIONS_INDICE_PENDENTES},
            ).scalar_one_or_none()
        if indexdef is None or "NULLS FIRST" not in indexdef:
            faltando.append(
                f"missing index: {_HANDOFF_OPERATIONS_INDICE_PENDENTES} (expected NULLS FIRST in the "
                f"PostgreSQL catalog, found indexdef={indexdef!r}) — this is the SUPERSEDED m019 form "
                f"(NULLS LAST by default), not the m020 fix; R15/R16 would Seq Scan the whole table on "
                f"every poll cycle (see migrations/m020_handoff_operations_indice_pendentes_nulls_first.py)"
            )
    return faltando


def check_lead_handoff_operations(insp: Inspector) -> list[str]:
    if _tabela_ausente(insp, _LEAD_HANDOFF_OPERATIONS_TABELA):
        return [f"missing table: {_LEAD_HANDOFF_OPERATIONS_TABELA}"]

    faltando = _colunas_faltando(insp, _LEAD_HANDOFF_OPERATIONS_TABELA, _LEAD_HANDOFF_OPERATIONS_COLUNAS)

    if not _tem_unique(insp, _LEAD_HANDOFF_OPERATIONS_TABELA, _LEAD_HANDOFF_OPERATIONS_UNIQUE_EVENT_ID):
        faltando.append(
            f"missing unique constraint: {_LEAD_HANDOFF_OPERATIONS_TABELA}(event_id) "
            f"(expected uq_lead_handoff_operations_event_id)"
        )
    return faltando


# ─── Agrupamento por app (lado Conversas / lado CRM) ───

def check_conversas_schema(engine: Engine) -> list[str]:
    insp = inspect(engine)
    faltando = []
    faltando += [f"[conversas] {m}" for m in check_conversation_events(insp)]
    faltando += [f"[conversas] {m}" for m in check_conversations(insp, engine)]
    faltando += [f"[conversas] {m}" for m in check_handoff_operations(insp, engine)]
    return faltando


def check_crm_schema(engine: Engine) -> list[str]:
    insp = inspect(engine)
    faltando = []
    faltando += [f"[crm] {m}" for m in check_lead_handoff_operations(insp)]
    return faltando


# ─── CLI ───

def _resolve_urls(argv: Sequence[str] | None) -> tuple[str | None, str | None, bool, str | None]:
    """
    DATABASE_URL vem do AMBIENTE ou de --database-url, nunca de app.config: os
    defaults de config apontam para arquivos SQLite locais e transformariam um
    operador que esqueceu de exportar a variavel em um "READY" contra um banco
    descartavel — mesmo motivo documentado em toda migration desta familia
    (`_resolve_target`).
    """
    parser = argparse.ArgumentParser(
        prog="check_bia_v2_schema.py",
        description=(
            "Gate de leitura (introspeccao) que confirma se o banco real tem "
            "tudo que a BIA V2 precisa antes de V2_MODE=live."
        ),
    )
    parser.add_argument(
        "--database-url", default=None,
        help="URL do banco do CRM (lead_handoff_operations). Default: env DATABASE_URL.",
    )
    parser.add_argument(
        "--conversas-database-url", default=None,
        help=(
            "URL do banco do Conversas (conversation_events/conversations/"
            "handoff_operations). Default: mesma URL do CRM — topologia de "
            "producao, ver docstring do modulo."
        ),
    )
    parser.add_argument(
        "--allow-sqlite", action="store_true",
        help="Permite apontar para SQLite (uso local/teste; producao e sempre PostgreSQL).",
    )
    args = parser.parse_args(argv)

    database_url = (args.database_url or os.getenv("DATABASE_URL", "")).strip()
    if not database_url:
        return None, None, False, (
            "[check_bia_v2_schema] RECUSADO — DATABASE_URL nao esta definida.\n"
            "[check_bia_v2_schema] Exporte-a explicitamente (ou passe --database-url); este\n"
            "[check_bia_v2_schema] script nao adivinha alvo.\n"
            "[check_bia_v2_schema]   DATABASE_URL=postgresql://... python scripts/check_bia_v2_schema.py"
        )

    conversas_url = (args.conversas_database_url or os.getenv("CONVERSAS_DATABASE_URL", "")).strip()
    conversas_url = conversas_url or database_url

    if not args.allow_sqlite:
        for nome_var, url in (("DATABASE_URL", database_url), ("CONVERSAS_DATABASE_URL", conversas_url)):
            if url.startswith("sqlite"):
                return None, None, False, (
                    f"[check_bia_v2_schema] RECUSADO — {nome_var} aponta para SQLite ({url}).\n"
                    "[check_bia_v2_schema] Producao e PostgreSQL. Para rodar em dev/teste de\n"
                    "[check_bia_v2_schema] proposito, passe --allow-sqlite."
                )

    return database_url, conversas_url, args.allow_sqlite, None


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    database_url, conversas_url, _allow_sqlite, refusal = _resolve_urls(argv)
    if refusal:
        print(refusal)
        return 2

    print("=== BIA V2 — gate de prontidao de schema (READ-ONLY, sem DDL) ===")
    print(f"DATABASE_URL (crm)      : {_mask(database_url)}")
    if conversas_url == database_url:
        print("CONVERSAS_DATABASE_URL  : (mesma URL do CRM — banco compartilhado, topologia de producao)")
    else:
        print(f"CONVERSAS_DATABASE_URL  : {_mask(conversas_url)}")
    print()

    crm_engine = None
    conversas_engine = None
    try:
        crm_engine = create_engine(database_url, pool_pre_ping=True)
        conversas_engine = (
            crm_engine if conversas_url == database_url
            else create_engine(conversas_url, pool_pre_ping=True)
        )
        faltando = []
        faltando += check_crm_schema(crm_engine)
        faltando += check_conversas_schema(conversas_engine)
    except _DialectoNaoSuportaCheckConstraint as exc:
        # "Nao foi possivel verificar" (exit 2) — NUNCA confundir com BLOCKED
        # (exit 1, que significa "confirmado que falta"). Ver docstring da
        # excecao e o contrato de 3 saidas no docstring do modulo.
        print(f"[FALHOU] {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 — falha de conexao/introspeccao nunca vira traceback cru
        # Mensagem segura: so o TIPO do erro + dica generica — NUNCA {exc} cru.
        # make_url() do SQLAlchemy embute a URL COMPLETA (senha em claro
        # incluida) na mensagem de ArgumentError quando a URL e malformada;
        # interpolar {exc} aqui vazaria a credencial no stdout. Mesmo idioma
        # de scripts/check_readonly_db.py:75-81.
        print(f"[FALHOU] nao foi possivel inspecionar o schema ({type(exc).__name__}).")
        print("         Verifique DATABASE_URL/CONVERSAS_DATABASE_URL, rede e credenciais.")
        print("         (Detalhe completo do erro foi omitido para nao vazar credenciais.)")
        return 2
    finally:
        if crm_engine is not None:
            crm_engine.dispose()
        if conversas_engine is not None and conversas_engine is not crm_engine:
            conversas_engine.dispose()

    if faltando:
        print(f"BLOCKED — {len(faltando)} item(ns) faltando:")
        for item in faltando:
            print(f"  - {item}")
        return 1

    print("READY — todos os itens de schema que a BIA V2 precisa estao presentes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
