"""
BIA-V2 R8/R10 — barreira de escrita operacional para conversas sob a V2.

Fonte normativa: `docs/superpowers/plans/2026-08-29-bia-v2.md` (R8, R10, P6) e
`bia-v2-r8-r10-inventory.md` (secoes 0, 3.2 e 3.3).

O PROBLEMA, E POR QUE ELE NAO PODE SER RESOLVIDO NO CALL SITE
---------------------------------------------------------------
Dezessete dos dezenove writers de aplicacao de `conversations.*` vivem dentro
dos OITO arquivos com hash fixado por `tests/test_v2_v1_intacta.py` (Familia A)
e vigiados por AST+grep (Familia B.1), que proibe qualquer referencia a `state`,
`state_version` ou `closed_at` neles. Fazer cada writer chamar um helper seria,
literalmente, faze-los ramificar sobre estado V2 — exatamente o que a Familia
B.1 existe para impedir, e o que ja foi implementado e REVERTIDO uma vez por
decisao explicita do dono (`bia-v2-r15-r17-design-v4.md`, secao 4.1).

Este modulo intercepta no FLUSH, e nao no call site: um listener
`before_flush` na `SessionLocal` ve a mutacao ja pronta, decide, e devolve o
controle. Nenhum dos oito arquivos precisa mudar, importar nada daqui, ou saber
que a V2 existe. A ramificacao sobre `state` acontece em UM lugar — este.

EXCECAO CONHECIDA, FORA DO ESCOPO ORM: "UM lugar" vale para escrita que passa
por `session.dirty` (o caminho dos oito arquivos). `_repair_responsavel_cache`
(`conversas/app/routers/conversations.py`) escreve via `sa_update()` executado
na hora por `db.execute()` e depois `set_committed_value()` — deliberado, para
nao sujar a sessao e evitar um 2o UPDATE — e por isso essa escrita especifica
tambem fica invisivel para esta barreira. Nao e descoberto: o CHECK
`ck_conversations_estado_humano` e o wrapper de recuperacao de `IntegrityError`
em volta dela sao a rede de seguranca desse caminho, nao esta barreira.

O ESCOPO E `WAITING_HUMAN` / `HUMAN_ACTIVE`, NUNCA `AI_TRIAGE`
---------------------------------------------------------------
Recorte deliberado, e o MESMO que o banco ja escolheu: o CHECK
`ck_conversations_estado_humano` (m015) tambem so vale para esses dois estados
e e deliberadamente inerte em `LEGACY`, `NEW`, `AI_TRIAGE`, `CLOSED` e `NULL`.
`AI_TRIAGE` fica de fora POR DECISAO DO DONO desta rodada: a Familia B.3 exige
que o ciclo V1 completo sobre uma conversa `AI_TRIAGE` produza resultado
IDENTICO ao de uma `LEGACY`, e a B.4 exige que ele NAO incremente
`state_version`. Cobrir `AI_TRIAGE` quebraria as duas. O caso
`AI_TRIAGE` + `claim` fica registrado como RESIDUAL ACEITO desta rodada — nao
como resolvido.

COMO A BARREIRA SABE QUE QUEM ESCREVEU FOI A V1
------------------------------------------------
Pelo proprio `state`. A V2 escreve `state`/`state_version`/`closed_at` por UM
caminho so (`estados.aplicar_transicao`); a V1 NUNCA os escreve — e isso nao e
convencao, e o que a Familia B.1 prova por AST e grep sobre os oito arquivos, e
o que o R8 afirma. Entao:

  - flush que MEXE em `state`/`state_version`/`closed_at`  -> foi a V2, ja
    passou pela maquina; a barreira nao faz nada (senao incrementaria
    `state_version` DUAS vezes no mesmo flush).
  - flush que NAO mexe neles mas mexe num campo operacional -> foi a V1; a
    barreira avalia.

Ler `conversation.state` cru nao serviria: depois de `aplicar_transicao` o
valor em memoria JA e o novo. O que decide e o HISTORICO do atributo
(`attributes.get_history`), que compara contra o valor carregado do banco.

SEM `begin_nested()`, SEM SEGUNDA TRANSACAO — E ISSO E OBRIGATORIO
-------------------------------------------------------------------
`.claude/memory/pysqlite-savepoint-rollback-gap.md` documenta que
`db.begin_nested()` no SQLite deste repo NAO desfaz num `db.rollback()`
posterior. A barreira nao precisa dele: ela roda DENTRO do `before_flush` do
flush que ja esta acontecendo, muta o mesmo objeto, e o incremento de
`state_version` vai ao banco no MESMO flush e na MESMA transacao da mutacao
operacional que o motivou. Nao ha janela entre as duas escritas. Mesmo molde de
`handoff._aplicar_estado_local`, que e o precedente deste desenho.

RECUSA = `TransicaoInvalida`, E ISSO NAO PIORA NADA
----------------------------------------------------
Quando a mutacao V1 deixa a linha incoerente, `aplicar_transicao` levanta
`TransicaoInvalida` e o flush aborta. Hoje, nesses mesmos dois estados, a mesma
escrita ja aborta — pelo `IntegrityError` do CHECK m015, um passo depois e sem
token de diagnostico. A barreira nao introduz um modo de falha novo: antecipa o
que ja falhava, com motivo legivel. Deliberadamente NAO se mapeia a excecao para
um status HTTP aqui: isso mudaria o contrato de rotas V1 que hoje devolvem 500
nesse caso, e a tarefa e barrar a escrita incoerente, nao redesenhar a resposta.
"""
from __future__ import annotations

import logging

from sqlalchemy import event
from sqlalchemy.orm import attributes

from app.database import SessionLocal
from app.models.conversation import Conversation
from app.v2.estados import Estado, aplicar_transicao

logger = logging.getLogger(__name__)

# `motivo` do evento/transicao. Formato exigido por `estados._validar_motivo`
# (`[a-z][a-z0-9_]{0,63}`) — token FECHADO, nunca dado de cliente.
MOTIVO_ESCRITA_V1 = "escrita_operacional_v1"

# Os DOIS estados pos-handoff — o mesmo recorte do CHECK m015. Comparados como
# STRING crua (o valor persistido na coluna), nao como membro de `Estado`:
# `state` e `String(32)` livre e pode conter qualquer coisa numa linha antiga.
ESTADOS_SOB_BARREIRA = frozenset({Estado.WAITING_HUMAN.value, Estado.HUMAN_ACTIVE.value})

# Os SEIS campos que `estados._validar_campos_operacionais` le para decidir se
# a linha e coerente com um estado. Nem um a mais: `unread_count` (que a
# marcacao de lido escreve em toda abertura de conversa) e `responsavel_nome`
# NAO sao campos da maquina, e inclui-los faria a barreira disparar — e
# incrementar `state_version` — em escrita que nao muda estado nenhum.
CAMPOS_OPERACIONAIS = (
    "is_bot_active",
    "atendente_id",
    "responsavel_id",
    "queued_at",
    "primeira_resposta_humana_at",
    "status",
)

# As tres colunas da maquina. Se QUALQUER uma delas mudou neste flush, quem
# escreveu foi a V2 e a barreira nao tem o que fazer — ver "COMO A BARREIRA
# SABE" no docstring do modulo.
CAMPOS_DA_MAQUINA = ("state", "state_version", "closed_at")


def _foi_escrito(conversation: Conversation, campo: str) -> bool:
    """`True` se este flush vai emitir `campo` no UPDATE - escrito ou nao com valor novo."""
    return attributes.get_history(conversation, campo).has_changes()


def _mudou_de_valor(conversation: Conversation, campo: str) -> bool:
    """
    `True` se `campo` foi escrito com valor DIFERENTE do que estava no banco.

    Reatribuir o MESMO valor marca o atributo como sujo no SQLAlchemy, e uma
    escrita sem efeito nao pode disparar transicao nem consumir `state_version`.
    Dai a comparacao explicita em vez de confiar so em `has_changes()`.

    QUANDO O VALOR ANTERIOR E DESCONHECIDO, ASSUME-SE QUE MUDOU. `history.deleted`
    so traz o valor antigo se ele estava CARREGADO no instante do `setattr`; num
    atributo expirado (logo apos um commit, sem leitura no meio) ele vem vazio, e
    ai `None` significa "nao sei", nunca "era None" - confundir os dois faria a
    barreira ignorar exatamente a escrita `atendente_id = None` (o release) que
    ela existe para barrar. Conservador de proposito: no pior caso valida uma
    linha que ja estava coerente, e uma auto-transicao valida e barata.
    """
    historico = attributes.get_history(conversation, campo)
    if not historico.has_changes():
        return False
    if not historico.deleted:
        return True
    atual = historico.added[0] if historico.added else None
    return historico.deleted[0] != atual


def _estado_persistido(conversation: Conversation) -> object:
    """
    O `state` como esta NO BANCO — nunca o de memoria.

    `get_history().unchanged` carrega o valor quando o atributo nao foi
    reescrito neste flush; e esse o caso em que a barreira roda (se `state`
    tivesse sido reescrito, quem escreveu foi a V2 e ja saimos antes).
    """
    historico = attributes.get_history(conversation, "state")
    if historico.unchanged:
        return historico.unchanged[0]
    return conversation.state


def destino_implicado(conversation: Conversation) -> Estado:
    """
    Qual estado os campos operacionais DENOTAM depois da mutacao da V1.

    Isto NAO reimplementa regra nenhuma: e a leitura direta da tabela de
    invariantes de `estados._validar_campos_operacionais`, que discrimina as
    quatro linhas alcancaveis por dois campos e um desempate:

      - `status == 'encerrada'`  -> so `CLOSED` aceita;
      - `is_bot_active is True`  -> so `NEW`/`AI_TRIAGE` aceitam, e `NEW` nao e
        alcancavel (nenhuma aresta leva a ele);
      - senao a linha e humana, e o UNICO campo que separa `WAITING_HUMAN` de
        `HUMAN_ACTIVE` e `primeira_resposta_humana_at`.

    Quem VALIDA continua sendo `aplicar_transicao`: se os campos nao casarem
    com o destino implicado (ou a aresta nao existir), e ela que recusa.
    """
    if conversation.status == "encerrada":
        return Estado.CLOSED
    if conversation.is_bot_active is True:
        return Estado.AI_TRIAGE
    if conversation.primeira_resposta_humana_at is not None:
        return Estado.HUMAN_ACTIVE
    return Estado.WAITING_HUMAN


def aplicar_barreira(conversation: Conversation) -> Estado | None:
    """
    Avalia UMA conversa suja. Devolve o destino aplicado, ou `None` se a
    barreira nao se aplica (passthrough).

    Levanta `TransicaoInvalida` (de `aplicar_transicao`) quando a mutacao da V1
    deixa a linha incoerente — ver "RECUSA" no docstring do modulo. Nao commita,
    nao faz rollback, nao abre sessao, nao faz IO.
    """
    if any(_foi_escrito(conversation, campo) for campo in CAMPOS_DA_MAQUINA):
        return None  # escrita da propria V2; ja passou pela maquina.

    if _estado_persistido(conversation) not in ESTADOS_SOB_BARREIRA:
        return None  # LEGACY / AI_TRIAGE / CLOSED / NEW / NULL / lixo: inerte.

    if not any(_mudou_de_valor(conversation, campo) for campo in CAMPOS_OPERACIONAIS):
        return None  # escrita sem efeito de estado (unread_count, tags, ...).

    destino = destino_implicado(conversation)
    aplicar_transicao(conversation, destino, motivo=MOTIVO_ESCRITA_V1)
    logger.info(
        "barreira V1/V2: escrita operacional legada normalizada. conversation_id=%s "
        "destino=%s state_version=%s",
        conversation.id, destino.value, conversation.state_version,
    )
    return destino


@event.listens_for(SessionLocal, "before_flush")
def _barreira_before_flush(session, flush_context, instances) -> None:
    """
    Ponto de registro UNICO. `session.dirty` e recalculado a cada acesso e a
    barreira muta os objetos que ele lista, entao a colecao e materializada
    ANTES do laco.

    So `dirty`: `session.new` sao INSERTs, e uma linha nova nasce `LEGACY`
    (server_default) ou ja com o `state` que quem a criou escolheu — em nenhum
    dos dois casos ha uma escrita legada a normalizar.
    """
    for instancia in list(session.dirty):
        if isinstance(instancia, Conversation):
            aplicar_barreira(instancia)
