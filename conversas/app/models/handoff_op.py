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

from sqlalchemy import CheckConstraint, Column, DateTime, Index, Integer, String
from sqlalchemy.sql import func

from app.database import Base


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
        # NENHUM indice secundario de proposito: `app/v2/handoff.py` consulta
        # esta tabela SO por `event_id`. Indice para uma consulta que ninguem
        # faz ainda e custo sem leitor.
    )

    def __repr__(self):
        return f"<HandoffOperation(id={self.id}, status='{self.status}')>"
