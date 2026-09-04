"""
Livro-razao de idempotencia do handoff Conversas -> CRM — WP BIA-V2, Fase 6.

Uma linha por `event_id` JA APLICADO por `POST /api/v2/lead-domain/handoff`.
O UNIQUE em `event_id` e o mecanismo: quem tenta gravar o mesmo `event_id` duas
vezes colide no banco, e o endpoint transforma a colisao em replay (200 com
`replayed=true`) ou em conflito (409), nunca num segundo efeito.

POR QUE TABELA PROPRIA, E NAO UMA COLUNA `event_id` EM `lead_history`
--------------------------------------------------------------------
`lead_history` ja e gravada em toda troca de `responsavel_id` (evento
`responsavel_changed`, `app/routers/leads.py`), entao reusa-la parecia o
caminho mais curto. Lendo o schema real, nao e — e o motivo nao e estetico:

1. `lead_history` NAO tem `target_user_id` como coluna. O id do novo
   responsavel vive dentro do JSON `dados` (`{"new_responsavel_id": ...}`). O
   contrato deste endpoint exige comparar `lead_id` E `target_user_id` do
   replay contra o que foi gravado, para distinguir 200-replay de 409. Fazer
   isso contra uma chave de JSON amarraria a idempotencia a um formato de blob
   escrito por OUTRO modulo — mudar aquele dict quebraria a idempotencia em
   silencio. Aqui os dois sao colunas, e a comparacao e trivial nos dois
   dialetos, sem ramo `IS_SQLITE`.

2. As duas escritas tem REGRAS DIFERENTES. `lead_history` so ganha linha
   quando o responsavel realmente muda (`if old_responsavel != responsavel_id`
   em `leads.py`). O livro-razao precisa de linha SEMPRE — inclusive quando o
   lead ja estava com o responsavel pedido —, senao um replay nao encontraria
   registro e reaplicaria. Forcar as duas regras na mesma tabela produziria
   linhas de historico do tipo "Responsavel alterado de '5' para '5'" na
   timeline do lead, ou um segundo vocabulario de `evento` so para esse caso.

3. Coluna nova em tabela existente TEM que ser nullable (Global Constraint do
   plano), e `event_id` nullable enfraquece exatamente a garantia que ele
   existe para dar. Tabela nova nasce com `event_id NOT NULL` — mesma escolha
   ja feita em `conversation_events` (m013) do lado do Conversas.

O que continua em `lead_history`: o rastro na timeline do lead. O endpoint
grava as DUAS coisas na mesma transacao — o livro-razao (sempre) e o evento
`responsavel_changed` (so quando o valor muda, igual a V1). Separar as tabelas
separa as regras; nao separa a transacao.

NOME DA TABELA — `lead_handoff_operations`, NAO `handoff_operations`
--------------------------------------------------------------------
Em producao o CRM e o Conversas compartilham o MESMO PostgreSQL (ver a secao
ESCOPO da m011). `handoff_operations` e o nome reservado pela File Structure do
plano para a tabela do lado do CONVERSAS (`conversas/app/models/handoff_op.py`,
migration m016), que guarda o estado da saga (REQUESTED/PENDING/COMPLETED).
Esta aqui e outra coisa: o registro do que o CRM ja aplicou. Dois nomes
distintos, prefixados pelo agregado de cada lado.

SEM ForeignKey EM `lead_id`/`target_user_id`, DE PROPOSITO
-----------------------------------------------------------
Mesma razao documentada em `conversation_events` (m013): o livro-razao e
append-only e precisa sobreviver a delecao da linha que descreve. Uma FK com
`ondelete=CASCADE` (o que `lead_history.lead_id` usa) apagaria o registro de
idempotencia junto com o lead, e a trilha de auditoria de "quem foi movido para
quem, sob qual `event_id`" desapareceria com ele. A existencia de lead e de
usuario e verificada explicitamente pelo endpoint, que devolve 404 — a FK nao
acrescentaria validacao, so o risco de apagamento em cascata.

CRESCIMENTO SEM LIMITE, DE PROPOSITO — E QUAL E O CONTROLE REAL
-----------------------------------------------------------------
Esta tabela e append-only e nunca encolhe: nenhuma linha e apagada ou arquivada
por este modulo, e isso NAO e uma omissao — e o proprio mecanismo de
idempotencia. Retencao, pruning ou TTL reabririam a janela de replay que a
tabela existe para fechar: um `event_id` antigo, liberado do livro-razao,
voltaria a ser tratado como INEDITO, e um handoff ja aplicado poderia ser
reaplicado.

O controle de abuso real NAO e limite de tamanho desta tabela — e o que ja
protege a API inteira: rate limit global de 200/minute por IP
(`SlowAPIMiddleware`, `app/main.py`) na frente do endpoint, mais a rotacao da
`BIA_V2_CRM_API_KEY` — a credencial do UNICO usuario autorizado a completar esta
operacao (ver a secao AUTORIZACAO de `app/routers/v2_lead_domain.py`; qualquer
OUTRA credencial ate autentica, mas recebe 403 antes de gravar). Quem nao tem
essa chave nao grava linha nenhuma; quem tem esta limitado pelo teto global
da aplicacao.
"""
from sqlalchemy import Column, DateTime, Index, Integer, String
from sqlalchemy.sql import func

from app.database import Base


class LeadHandoffOperation(Base):
    __tablename__ = "lead_handoff_operations"

    id = Column(Integer, primary_key=True, index=True)

    # Chave de idempotencia vinda do Conversas. Guardada SEMPRE na forma
    # canonica do UUID (minuscula, com hifens) — a canonicalizacao acontece na
    # fronteira, em `app/schemas/lead_domain.py`. Sem ela, "A0EE..." e
    # "a0ee..." sao o mesmo UUID e strings diferentes: o UNIQUE nao pega, e o
    # mesmo handoff seria aplicado duas vezes.
    event_id = Column(String(36), nullable=False)

    lead_id = Column(Integer, nullable=False)
    target_user_id = Column(Integer, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # `Index(unique=True)` e nao `UniqueConstraint`: mesmo DDL vindo do
    # `create_all()` e da migration m017, nos dois dialetos — mesma razao
    # documentada em `FunnelEntry` (m011) e em `conversation_events` (m013).
    __table_args__ = (
        Index("uq_lead_handoff_operations_event_id", "event_id", unique=True),
    )

    def __repr__(self):
        return (
            f"<LeadHandoffOperation(event_id='{self.event_id}', "
            f"lead_id={self.lead_id}, target_user_id={self.target_user_id})>"
        )
