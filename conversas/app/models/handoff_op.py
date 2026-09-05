"""
BIA-V2 Fase 6 — registro das operacoes de handoff BIA -> humano.

Uma linha por OPERACAO (nao por evento): e o log de INTENCAO que torna o
handoff reconciliavel. `app/v2/handoff.py` grava `REQUESTED` e COMITA antes de
falar com o CRM; so depois da confirmacao do CRM aplica o estado local e marca
`COMPLETED`. Uma linha parada em `REQUESTED` significa exatamente "o processo
morreu entre a chamada ao CRM e o commit local" — e e por ela que uma segunda
chamada com o MESMO `event_id` retoma de onde parou, em vez de comecar do zero
(ver o docstring de `app/v2/handoff.py`).

CONCORRENCIA NA RETOMADA (achado F2 da revisao da Fase 6) — NAO resolvida por
um status transitorio novo. Uma primeira versao desta correcao introduzia
`RESUMING` como CLAIM atomico antes de prosseguir; foi abandonada porque um
crash exatamente entre o claim e o commit final deixaria a operacao PRESA em
`RESUMING` para sempre (nada reclama de volta um `RESUMING` orfao) — o mesmo
crash que a retomada existe para tornar recuperavel. A correcao real vive em
`app/v2/handoff.py` (`_reservar_desfecho_ou_ceder_para_vencedor`): a exclusao
mutua acontece no COMMIT final de cada tentativa, condicionado a `status !=
'COMPLETED'` na mesma transacao que aplica o efeito local — nao antes dele.
Ver o docstring daquele modulo, secao "CONCORRENCIA NO MESMO event_id".

`event_id` E A CHAVE DE IDEMPOTENCIA DA OPERACAO, com UNIQUE de verdade no
banco. E o mesmo valor enviado ao CRM como idempotency key, entao os dois lados
concordam sobre "esta operacao ja foi aplicada?" sem precisar comparar efeitos.
Nao e o `event_id` de nenhuma linha de `conversation_events`: os eventos da
trilha (`HANDOFF_REQUESTED`, `HUMAN_SELECTED`, `HANDOFF_COMPLETED`,
`HANDOFF_FAILED`) geram os proprios `uuid4()`, porque UMA operacao produz
VARIOS eventos e um UNIQUE nao caberia em todos.

SEM ForeignKey, pelo mesmo motivo de `ConversationEvent`: este registro e
auditoria/reconciliacao e precisa sobreviver a delecao do que descreve.
`target_user_id` aponta para a tabela `users`, que vive no schema do CRM —
mesmo tratamento que `Conversation.lead_id` (inteiro indexado, sem FK).

VOCABULARIO DE `status`: `REQUESTED`/`COMPLETED`/`FAILED`, palavra por palavra
igual aos tres `TipoEvento` correspondentes (`HANDOFF_REQUESTED`,
`HANDOFF_COMPLETED`, `HANDOFF_FAILED`). O plano usa "PENDING" numa frase em
prosa; adotar aquele termo criaria DOIS vocabularios para o mesmo fato, e quem
lesse a trilha teria de traduzir de um para o outro. O CHECK abaixo torna a
allowlist estrutural — nao depende de todo writer futuro lembrar dos tres.

Escrita: SOMENTE por `app/v2/handoff.py`. Nenhum fluxo da V1 conhece esta
tabela.

CRESCIMENTO ILIMITADO POR DESENHO — mesmo achado do lado CRM
(`app/models/lead_handoff_operation.py`). Esta tabela e APPEND-ONLY: nenhum
fluxo faz `DELETE`, porque a idempotencia por `event_id` (o UNIQUE abaixo)
exige que a linha da PRIMEIRA tentativa continue existindo para qualquer
retomada ou replay futuro reconhecer o que ja foi feito — apagar uma linha
"antiga" reabriria a janela de aplicacao dupla que o UNIQUE existe para
fechar. O controle de abuso real NAO e limite de tabela: e a superficie de
quem pode chamar `handoff_to_human()`. Nao ha rota HTTP publica que aceite
`event_id`/`target_user_id` arbitrarios e grave aqui — quem chama e codigo
interno da V2 (BIA decidindo handoff a partir de uma conversa real), e cada
conversa real produz no maximo um punhado de tentativas (a primeira, mais
eventuais retries por falha transitoria do CRM). O volume, portanto, e
limitado pelo volume real de conversas do WhatsApp Business, nao por um
endpoint que um chamador anonimo possa martelar.
"""
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import CheckConstraint, Column, DateTime, Index, Integer, String, text
from sqlalchemy.sql import func

from app.database import Base, IS_SQLITE


class StatusHandoff(StrEnum):
    """Os tres estados possiveis de uma operacao de handoff."""

    REQUESTED = "REQUESTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# Expressao do CHECK montada a partir do enum acima — o model nunca deixa a
# lista de tokens divergir do vocabulario Python. `migrations/m016` repete a
# MESMA expressao como literal (nao importa daqui, como manda a convencao de
# migrations/CLAUDE.md); `tests/test_v2_m016_gate.py` confere que as duas
# batem (F4 da revisao da Fase 6 corrigiu esta referencia — apontava para
# tests/test_v2_handoff.py, que nao faz essa checagem).
_TOKENS_STATUS = ",".join(f"'{membro.value}'" for membro in StatusHandoff)
_CHECK_STATUS_SQL = f"status IN ({_TOKENS_STATUS})"

# m020 — colunas do indice parcial de baixo, condicionadas por DIALETO (nao
# so o WHERE, que ja era condicional desde m019). Achado de concorrencia:
# `app/v2/worker_fila.py:consulta_de_reclamacao` ordena por
# `next_attempt_at.asc().nullsfirst(), id.asc()`; um btree comum e ASC NULLS
# LAST por padrao no PostgreSQL (SQLite e o OPOSTO — ASC ja poe NULL
# primeiro por padrao ali). `NULLS FIRST` pedido pela consulta x `NULLS
# LAST` do indice sao direcoes de varredura INCOMPATIVEIS: o planner
# descarta o indice inteiro e faz Seq Scan + Sort completo da tabela a CADA
# CICLO de CADA worker (30s) — provado empiricamente (EXPLAIN ANALYZE,
# PostgreSQL 16 real, 30k linhas): 14.796ms/587 buffers sem indice efetivo
# contra 0.098ms/24 buffers com a ordem corrigida, e o custo cresce com o
# tamanho da tabela, que por desenho nunca e podada (ver "CRESCIMENTO
# ILIMITADO" abaixo). Migration `m020_handoff_operations_indice_pendentes_
# nulls_first.py` substitui o indice antigo (DROP + CREATE) por este.
#
# `NULLS FIRST` SO no ramo PostgreSQL, nunca no SQLite: o SQLite deste
# projeto (>= 3.30) aceita `NULLS FIRST` em `ORDER BY`, mas NAO dentro de
# `CREATE INDEX` — levanta `OperationalError: unsupported use of NULLS
# FIRST` (provado contra o driver `sqlite3` real antes de escrever isto). E
# nao faz falta ali: o ASC padrao do SQLite ja poe NULL primeiro (o mesmo
# fato que `worker_fila.py` documenta em `_janela_chegou`), entao um indice
# ascendente comum ja bate com a consulta sem anotacao nenhuma — confirmado
# com `EXPLAIN QUERY PLAN` (`SCAN ... USING INDEX`, sem SORT separado). Cada
# processo roda UM dialeto so (`IS_SQLITE` e fixado na importacao de
# `app.database`), entao esta escolha e feita uma vez so, nunca alternando.
#
# `id` como SEGUNDA coluna nos dois dialetos (novo em relacao a m019): a
# consulta desempata por `id.asc()` depois de `next_attempt_at` — sem a
# segunda coluna no indice, o PostgreSQL ainda precisaria de um "Incremental
# Sort" residual para as linhas empatadas (o caso mais comum e `NULL`, o
# mais urgente — "Legenda A", secao 6.1 do desenho).
if IS_SQLITE:
    _INDICE_PENDENTES_COLS = ("next_attempt_at", "id")
else:
    _INDICE_PENDENTES_COLS = (text("next_attempt_at ASC NULLS FIRST"), "id")


class HandoffOperation(Base):
    """Uma operacao de handoff BIA -> humano — ver docstring do modulo."""

    __tablename__ = "handoff_operations"

    id = Column(Integer, primary_key=True, index=True)
    # UNIQUE de verdade no banco — ver __table_args__.
    event_id = Column(String(36), nullable=False)
    conversation_id = Column(Integer, nullable=False)
    target_user_id = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False)
    # Motivo da falha, no mesmo vocabulario FECHADO gravado em
    # `conversation_events.error_code` (formato `^[A-Z][A-Z0-9_]*$`). NULL
    # enquanto a operacao nao falhou; limpo quando uma nova tentativa comeca.
    error_code = Column(String(64), nullable=True)

    # ---- R15/R17, m019 (secao 8/4.2 do desenho) — reconciliacao e retomada.
    # Schema apenas: quem LE e ESCREVE estes campos e `app/v2/handoff.py` e o
    # worker, fora do escopo deste model/migration.
    #
    # `responsavel_id` que a operacao esperava encontrar no CRM no momento em
    # que foi criada — usado pelo CAS da reconciliacao (R15), nunca o valor
    # atual. Sem FK, mesmo motivo de `target_user_id`: `leads` vive no schema
    # do CRM.
    expected_responsavel_id = Column(Integer, nullable=True)
    # Quantas vezes R16 ja tentou retomar esta operacao. NOT NULL: ao
    # contrario de `state_version` (Conversation/m014), aqui nao existe
    # "ausencia de contagem" — toda operacao nasce com zero tentativas, por
    # isso NOT NULL de verdade (nao so nullable-com-default). `default=0` +
    # `server_default=text("0")` no mesmo par que `Message.send_attempts` usa
    # para NOT NULL numerico com DEFAULT — `text("0")` e nao a string nua "0"
    # pelo mesmo motivo de `state_version`: string nua vira `DEFAULT '0'` no
    # DDL, nao `DEFAULT 0`, e a coluna e INTEGER.
    attempt_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    # Proximo instante elegivel para retomada — NULL enquanto a operacao nunca
    # falhou. Coluna que o indice parcial abaixo cobre.
    next_attempt_at = Column(DateTime(timezone=True), nullable=True)
    # Instante em que a reconciliacao (R15) resolveu esta linha — NULL
    # enquanto pendente.
    reconciled_at = Column(DateTime(timezone=True), nullable=True)
    # Classe do erro (NAO_APLICADO_RETRIAVEL/AMBIGUO_RETRIAVEL/...) que a regra
    # de particao de fila do §4.2 usa para decidir se R16 e elegivel —
    # `error_code` sozinho NAO determina a classe (o mesmo token pode ser
    # retriavel OU ambiguo dependendo do status HTTP que o originou).
    error_class = Column(String(32), nullable=True)

    # Mesmo padrao de `ConversationEvent.created_at`: default do lado do Python
    # + server_default. `now()` do PostgreSQL e por TRANSACAO, entao linhas
    # gravadas na mesma transacao ficariam todas com o mesmo instante.
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        # UNIQUE declarada como Index(unique=True), nao Column(unique=True) nem
        # UniqueConstraint — mesmo motivo ja documentado em
        # `Conversation.whatsapp` (AUDIT-2026-08-W2E) e em `ConversationEvent`:
        # assim `create_all()` e a m016 emitem EXATAMENTE o mesmo objeto
        # (CREATE UNIQUE INDEX) nos dois dialetos.
        Index("uq_handoff_operations_event_id", "event_id", unique=True),
        CheckConstraint(_CHECK_STATUS_SQL, name="ck_handoff_operations_status"),
        # m019 (secao 8/4.2 do desenho), colunas corrigidas pela m020 — indice
        # PARCIAL compartilhado por R15 e R16: os dois workers fazem `FOR
        # UPDATE SKIP LOCKED` sobre `next_attempt_at`/`id` restritos as linhas
        # ainda pendentes de retomada. `sqlite_where` E `postgresql_where`
        # (nao so o de producao) porque o SQLite de dev/CI tambem precisa
        # emitir o MESMO objeto que a migration cria em bancos existentes —
        # mesmo cuidado de paridade model x migration ja aplicado ao
        # `uq_handoff_operations_event_id` acima e a `Conversation.whatsapp`.
        # Mesmo NOME e mesmo PREDICADO nos dois dialetos; a ORDEM/anotacao das
        # colunas (`_INDICE_PENDENTES_COLS` acima) diverge por dialeto DE
        # PROPOSITO desde a m020 — nao editar um lado sem reler o motivo la.
        Index(
            "ix_handoff_operations_pendentes",
            *_INDICE_PENDENTES_COLS,
            sqlite_where=text("status IN ('REQUESTED','FAILED')"),
            postgresql_where=text("status IN ('REQUESTED','FAILED')"),
        ),
    )

    def __repr__(self):
        return f"<HandoffOperation(id={self.id}, status='{self.status}')>"
