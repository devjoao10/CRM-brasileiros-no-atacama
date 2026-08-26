"""
AUDIT-2026-08-WA — quem recebe a conversa quando a Bia termina a triagem.

PROBLEMA QUE ISTO RESOLVE
-------------------------
O unico lugar do sistema que escolhia um atendente era um literal dentro de um
no do n8n: `Tool Alterar Responsavel` chama
`PUT /api/leads/{lead_id}/responsavel?responsavel_id=5`. Numero fixo, dentro de
um workflow que este repositorio nao pode alterar, decidido por um LLM. Nao ha
pool, nao ha carga, nao ha "quem esta ativo".

Hoje existe uma unica atendente operacional. Amanha existem duas. Se a escolha
continuar sendo um id escrito a mao, a segunda atendente exige mexer no n8n de
novo — e o mesmo bug volta.

Este modulo e a resolucao, e ela e configuravel:

    ATENDENTES_ELEGIVEIS="7"        -> so essa pessoa recebe
    ATENDENTES_ELEGIVEIS="7,11"     -> distribui entre as duas
    ATENDENTES_ELEGIVEIS=""         -> todos os usuarios ativos (default)

A estrategia e **menor carga primeiro**: conta as conversas abertas ja
atribuidas a cada elegivel e devolve quem tem menos, desempatando pelo menor
id. Com um unico elegivel isso devolve sempre ele — o comportamento de hoje,
sem nome nem id espalhado pelo codigo. Com dois, distribui sozinho.

NAO E round-robin com estado: nao guardamos ponteiro. Menor carga e melhor que
round-robin aqui porque sobrevive a reinicio, a atendente que entra no meio do
dia e a conversa que alguem assumiu manualmente.
"""
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.auth import User
from app.models.conversation import Conversation

logger = logging.getLogger(__name__)

# Mesmos status que o inbox considera "aberta" (conversations.py:LEGACY_OPEN_STATUSES).
# Duplicado aqui de proposito: importar do router criaria um ciclo
# router -> service -> router.
_STATUS_ABERTOS = ("aberta", "aguardando")


def _ids_configurados() -> list[int]:
    """
    Le ATENDENTES_ELEGIVEIS do ambiente a CADA chamada, nao no import.

    Isso e deliberado: a lista muda quando uma atendente entra ou sai, e um
    valor congelado no import obrigaria a reiniciar o servico para que a nova
    pessoa passasse a receber conversa. O custo e um getenv por handoff.
    """
    bruto = os.getenv("ATENDENTES_ELEGIVEIS", "").strip()
    if not bruto:
        return []
    ids = []
    for parte in bruto.split(","):
        parte = parte.strip()
        if not parte:
            continue
        try:
            ids.append(int(parte))
        except ValueError:
            logger.warning(
                "ATENDENTES_ELEGIVEIS contem valor nao numerico ignorado: %r", parte
            )
    return ids


def atendentes_elegiveis(db: Session) -> list[int]:
    """
    Ids que podem receber uma conversa vinda da fila, em ordem de id.

    Com ATENDENTES_ELEGIVEIS definido, so entram os ids listados que existirem
    e estiverem ATIVOS — um id desligado na configuracao nao vira dono fantasma.
    Sem configuracao, todos os usuarios ativos.
    """
    query = db.query(User.id).filter(User.is_active == True)  # noqa: E712
    configurados = _ids_configurados()
    if configurados:
        query = query.filter(User.id.in_(configurados))
    return [row[0] for row in query.order_by(User.id.asc()).all()]


def resolver_atendente_elegivel(db: Session) -> int | None:
    """
    Devolve o id de quem deve receber a proxima conversa, ou None.

    None NAO e erro: significa "nao ha ninguem elegivel agora". O chamador deve
    enfileirar a conversa **sem dono** — uma fila sem atendente e recuperavel
    (qualquer um assume); uma conversa atribuida a alguem inexistente ou
    inativo desaparece da caixa de todo mundo, que e exatamente o defeito que
    esta rodada esta corrigindo.
    """
    elegiveis = atendentes_elegiveis(db)
    if not elegiveis:
        logger.warning(
            "Handoff sem atendente elegivel — conversa entra na fila sem dono. "
            "Verifique ATENDENTES_ELEGIVEIS e os usuarios ativos."
        )
        return None

    if len(elegiveis) == 1:
        return elegiveis[0]

    # Carga = conversas ABERTAS ja atribuidas. Uma so query; a tabela e pequena
    # (dezenas de linhas) e isto roda uma vez por handoff, nao por listagem.
    carga = dict(
        db.query(Conversation.atendente_id, func.count(Conversation.id))
        .filter(
            Conversation.atendente_id.in_(elegiveis),
            Conversation.status.in_(_STATUS_ABERTOS),
        )
        .group_by(Conversation.atendente_id)
        .all()
    )
    # `min` sobre (carga, id): menor carga vence, id desempata de forma estavel.
    return min(elegiveis, key=lambda uid: (carga.get(uid, 0), uid))


# ─── Estado operacional da conversa ───────────────────────────────────
# PONTO UNICO de escrita de is_bot_active / atendente_id / queued_at /
# primeira_resposta_humana_at. Vive aqui, e nao no router, porque quem precisa
# escrever esse estado sao DOIS caminhos: as rotas de fila (claim/assign/
# release/handoff/PUT) e o registro de mensagem outbound. Quando as duas
# metades moravam em modulos diferentes, uma delas passou a escrever direto
# (F-085: `PUT /{id}` gravava atendente_id e is_bot_active fora do helper e
# produzia conversa que sumia de todas as abas).


def aplicar_estado_humano(
    conversation: Conversation,
    atendente_id: Optional[int],
    *,
    keep_queue_position: bool = False,
    resetar_atendimento: bool = False,
) -> None:
    """
    Tira a conversa do universo da BIA e define quem e o dono.

    INVARIANTE NOVO (AUDIT-2026-08-WA) — atribuir NAO tira da fila:

        primeira_resposta_humana_at IS NULL -> a conversa ESPERA (queued_at set)
        primeira_resposta_humana_at NOT NULL -> alguem ja atendeu (queued_at NULL)

    Antes, `queued_at` era zerado assim que um `atendente_id` era definido. Isso
    fazia o handoff que atribui um dono remover a conversa da FILA DE ESPERA
    antes de qualquer humano falar com o cliente — e a fila ficava vazia
    enquanto a Bia dizia ao cliente que ele estava nela.

    `is_bot_active` vira False em TODOS os casos: chegar aqui significa handoff,
    claim, assign, release ou initiate. A unica volta para a BIA e a REABERTURA
    de uma conversa encerrada (webhook).

    `keep_queue_position=True` preserva um `queued_at` existente — e o que torna
    o handoff idempotente: retry do n8n NAO manda a conversa para o fim da fila.

    `resetar_atendimento=True` apaga `primeira_resposta_humana_at`: usado pelo
    release, onde devolver a conversa a fila significa que ela volta a esperar
    por uma primeira resposta.

    NAO toca responsavel_id/responsavel_nome: responsabilidade COMERCIAL e do
    CRM e nao pode ser escrita por operacao de fila.
    """
    conversation.atendente_id = atendente_id
    conversation.is_bot_active = False

    if resetar_atendimento:
        conversation.primeira_resposta_humana_at = None

    if conversation.primeira_resposta_humana_at is not None:
        conversation.queued_at = None
    elif not (keep_queue_position and conversation.queued_at):
        conversation.queued_at = datetime.now(timezone.utc)


def marcar_atendimento_humano(conversation: Conversation, autor_user_id: int) -> bool:
    """
    Registra a PRIMEIRA resposta humana. Devolve True se foi esta a primeira.

    Chamado por `record_outbound_message` quando — e somente quando — o envio
    partiu de uma rota autenticada por uma pessoa. Bia e auto-resposta passam
    `autor_user_id=None` e nunca chegam aqui: uma resposta automatica nao
    encerra a espera do cliente por um humano.

    Efeitos, todos na primeira vez apenas:
      - primeira_resposta_humana_at = agora  (sai da FILA, entra em MEUS)
      - queued_at = NULL                     (nao espera mais)
      - atendente_id = quem respondeu, se ainda estava vazio (quem responde
        assume; nao existe atendimento sem dono)
      - is_bot_active = False                (defensivo: se a Bia ainda estava
        ligada por qualquer caminho, um humano falando encerra isso)
    """
    if conversation.primeira_resposta_humana_at is not None:
        return False

    conversation.primeira_resposta_humana_at = datetime.now(timezone.utc)
    conversation.queued_at = None
    conversation.is_bot_active = False
    if conversation.atendente_id is None:
        conversation.atendente_id = autor_user_id
    return True
