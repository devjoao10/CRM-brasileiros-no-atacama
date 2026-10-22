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
    desligar_bia: bool = True,
) -> None:
    """
    Poe a conversa na fila humana e define quem e o dono. A Bia so e desligada
    quando `desligar_bia` for True (CONV-BIA-FILA-01, abaixo).

    INVARIANTE (AUDIT-2026-08-WA, revisada pelo HOTFIX-01) — atribuir NAO tira
    da fila, e responder tambem NAO apaga o instante em que ela entrou:

        primeira_resposta_humana_at IS NULL -> a conversa ESPERA (queued_at set)
        primeira_resposta_humana_at NOT NULL -> alguem ja atendeu (queued_at
            PRESERVADO — nao mais zerado aqui, ver HOTFIX-01 abaixo)

    Antes, `queued_at` era zerado assim que um `atendente_id` era definido. Isso
    fazia o handoff que atribui um dono remover a conversa da FILA DE ESPERA
    antes de qualquer humano falar com o cliente — e a fila ficava vazia
    enquanto a Bia dizia ao cliente que ele estava nela.

    HOTFIX-01 (pre-autorizada pelo dono do projeto — preservar queued_at na 1a
    resposta humana): esta funcao TAMBEM zerava `queued_at` sempre que
    `primeira_resposta_humana_at` ja nao era NULL — ou seja, qualquer
    claim/assign/release/handoff/PUT rodado DEPOIS da primeira resposta humana
    apagava de novo o instante de entrada na fila, tornando "tempo em fila" e
    "tempo ate a 1a resposta" para sempre incalculaveis. Isso tambem
    contradizia `app/v2/estados.py`, que ja EXIGE `queued_at IS NOT NULL` para
    os estados WAITING_HUMAN e HUMAN_ACTIVE (mesma regra do CHECK
    `ck_conversations_estado_humano`, m015). Agora, uma vez respondida,
    `queued_at` e so PRESERVADO por este ramo (nunca mais escrito aqui); quem
    o zera de novo e SOMENTE o `resetar_atendimento=True` do release, que
    devolve a conversa a fila de proposito.

    CONV-BIA-FILA-01 — entrar ou continuar na fila NAO desliga mais a Bia: quem
    a desliga e a vendedora, manualmente. O flag so cai por `desligar_bia`:

        desligar_bia=True (padrao) -> um humano assumindo, ou a Bia falhando:
            claim, botao "Desativar Bot" (PUT `is_bot_active=false`) e a falha
            degradada da Bia no webhook (evita o loop do "repita sua mensagem").
            O initiate tambem nasce sem a Bia, mas grava direto na criacao da
            conversa. O padrao e mantido de proposito: o webhook e o handoff
            V2 dependem dele.
        desligar_bia=False -> preserva `is_bot_active` como esta: handoff,
            assign (inclusive o do workflow n8n "Handoff Humano -> Julia"),
            release e o PUT que so troca o atendente. NUNCA religa uma Bia
            desligada.

    Esta funcao nunca religa a Bia: a volta para ela e a REABERTURA de uma
    conversa encerrada (webhook) ou o PUT `is_bot_active=true`, que devolve a
    conversa inteira.

    `keep_queue_position=True` preserva um `queued_at` existente — e o que torna
    o handoff idempotente: retry do n8n NAO manda a conversa para o fim da fila.

    `resetar_atendimento=True` apaga `primeira_resposta_humana_at`: usado pelo
    release, onde devolver a conversa a fila significa que ela volta a esperar
    por uma primeira resposta.

    NAO toca responsavel_id/responsavel_nome: responsabilidade COMERCIAL e do
    CRM e nao pode ser escrita por operacao de fila.
    """
    conversation.atendente_id = atendente_id
    if desligar_bia:
        conversation.is_bot_active = False

    if resetar_atendimento:
        conversation.primeira_resposta_humana_at = None

    if conversation.primeira_resposta_humana_at is not None:
        # HOTFIX-01: queued_at e PRESERVADO aqui — nao mais zerado (ver
        # docstring). Nada a fazer neste ramo.
        pass
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
      - queued_at PRESERVADO                 (HOTFIX-01 — nao mais zerado
        aqui; ver nota abaixo)
      - atendente_id = quem respondeu, se ainda estava vazio (quem responde
        assume; nao existe atendimento sem dono)
      - is_bot_active = False                (a Bia segue ligada na fila —
        CONV-BIA-FILA-01; a primeira resposta humana e a vendedora assumindo
        a conversa, e a Bia para de responder)

    HOTFIX-01 (pre-autorizada pelo dono do projeto — preservar queued_at na 1a
    resposta humana): ate aqui esta funcao zerava `queued_at` na MESMA escrita
    que grava `primeira_resposta_humana_at` — apagando o instante em que a
    conversa ENTROU na fila exatamente no momento em que ele passa a ter valor
    de metrica ("tempo em fila" e "tempo ate a 1a resposta" =
    primeira_resposta_humana_at - queued_at). Isso tambem contradizia
    `app/v2/estados.py`, que ja EXIGE `queued_at IS NOT NULL` para os estados
    WAITING_HUMAN e HUMAN_ACTIVE (mesma regra do CHECK
    `ck_conversations_estado_humano`, m015) — uma conversa V2 recebendo a 1a
    resposta humana antes de transicionar para HUMAN_ACTIVE violava esse CHECK
    no commit. Agora `queued_at` so e escrito por quem carimba a entrada na
    fila (`aplicar_estado_humano`) ou por quem devolve a conversa a ela
    (release); esta funcao nunca mais o apaga.

    AUDIT-2026-08-WF2 — RETOMADA: outbound humano em conversa ENCERRADA REABRE.

    Todo predicado de `_inbox_predicates` exige status IN ('aberta','aguardando').
    O envio gravava atendente_id/primeira_resposta_humana_at e deixava
    status='encerrada': a conversa que o atendente acabou de retomar (composer
    dentro da janela, ou template de retomada fora dela — o unico caminho
    permitido pela Meta) sumia de meus/fila/bia/todos e so restava em
    "encerradas". Mesma classe de estado do F-085.

    'aberta', nao 'aguardando': 'aguardando' e LEGADO e DERIVADO — a whitelist
    do `PUT /{id}` recusa escreve-lo ("use 'aberta' ou 'encerrada'; 'aguardando'
    e derivado") e ele so sobrevive em `LEGACY_OPEN_STATUSES` por tolerancia a
    linhas antigas. A reabertura pelo webhook ja grava 'aberta'; nao ha um
    segundo estado aberto a escolher.

    Fica ANTES do early-return de proposito: uma conversa atendida antes de ser
    encerrada tem primeira_resposta_humana_at != NULL e sairia daqui sem reabrir.

    Efeito no webhook, sem precisar toca-lo: o ramo de reabertura
    (`if conversation.status == "encerrada"`) deixa de disparar na resposta
    seguinte do cliente, entao atendente_id e primeira_resposta_humana_at
    sobrevivem e a Bia NAO reassume a conversa que um humano retomou.

    So chega aqui envio HUMANO (`autor_user_id` != None) e BEM-SUCEDIDO: a Bia,
    as auto-respostas e a frase de encerramento passam `autor_user_id=None` e
    uma tentativa recusada pela Meta nem chama esta funcao — nenhuma delas
    reabre conversa por conta propria.
    """
    if conversation.status == "encerrada":
        conversation.status = "aberta"

    if conversation.primeira_resposta_humana_at is not None:
        return False

    conversation.primeira_resposta_humana_at = datetime.now(timezone.utc)
    # HOTFIX-01: queued_at NAO e mais zerado aqui — preservado para que
    # "tempo em fila" e "tempo ate a 1a resposta" continuem calculaveis depois
    # do fluxo (ver docstring acima).
    conversation.is_bot_active = False
    if conversation.atendente_id is None:
        conversation.atendente_id = autor_user_id
    return True
