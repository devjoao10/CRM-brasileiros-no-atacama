"""
BIA-V2 R16 — worker de RETOMADA de handoffs.

Fonte normativa: `bia-v2-r15-r17-design-v4.md`, secao 5 (e 4.2 para a particao
de fila). Irmao de `worker_reconciliacao.py` (R15): mesmo protocolo de
reclamacao, fatias DISJUNTAS da mesma fila — as duas definidas juntas em
`worker_fila.py`, nunca aqui.

O QUE ESTE WORKER FAZ, E O QUE ELE DELIBERADAMENTE NAO FAZ
-----------------------------------------------------------
Faz: reapresenta ao CRM operacoes que ficaram para tras — tipicamente uma linha
presa em `REQUESTED` (o processo morreu entre a chamada ao CRM e o commit
local; ver Legenda A da secao 6.1: NENHUMA resposta HTTP produz esse rastro
sozinha, so um crash), e falhas de classe retriavel/ambigua.

Nao faz: LLM, triagem, `event_id` novo, `MESSAGE_RECEIVED` reenviado. A
retomada chama `handoff_to_human` com a MESMA IDENTIDADE da tentativa original
— mesmo `conversation_id`, mesmo `target_user_id`, mesmo `event_id`. E por isso
que ela e segura: o `event_id` E a chave de idempotencia dos DOIS lados, entao
reapresenta-lo nunca aplica nada duas vezes; no maximo devolve o que ja foi
aplicado (`replayed`), ou — desde a correcao do Caso 2b (secao 2.1.1) — um 409
`LEAD_ALREADY_ASSIGNED` quando o dono do lead ja mudou desde entao, que e
exatamente o desfecho correto para uma retomada que chegou tarde demais.

NAO E UMA ROTA HTTP. E um processo interno separado, com laco proprio de 30s:

    python -m app.v2.worker_retomada        # a partir de `conversas/`

Rodar duas ou mais instancias e seguro e e o modo pretendido de escalar: a
reclamacao usa `FOR UPDATE SKIP LOCKED` (`worker_fila.reclamar`), entao duas
instancias nunca pegam a mesma linha.

O CONTADOR DE TENTATIVAS, E POR QUE ELE MUDA DE LUGAR AQUI
-----------------------------------------------------------
`attempt_count` NAO e incrementado na reclamacao, e sim no desfecho, depois que
a chamada de rede retornou (achado HIGH da revisao, secao 5). O contador e o
gate de dead-letter — nao um numero de eficiencia — entao incrementa-lo cedo
faria 5 crashes consecutivos entre reclamar e ligar para o CRM esgotarem o teto
de uma operacao NUNCA tentada. Com a ordem corrigida, um crash nesse intervalo
so devolve a linha a fila no ciclo seguinte.

Tres coisas, entao, NAO consomem orcamento de tentativas: a reclamacao que
nunca chamou a rede, um 429/`CRM_LIMITE_TAXA` (a requisicao tocou a rede, mas
foi recusada pelo `SlowAPIMiddleware` ACIMA do roteamento — a logica de negocio
do CRM jamais viu a operacao), e uma excecao INESPERADA (infra, bug) — que e a
forma em processo do mesmo crash do paragrafo acima. Ver
`worker_fila.contabilizar_tentativa`.

CONSOME, sim, o VEREDITO de `handoff_to_human` (`ValueError` e suas subclasses
`HandoffInconsistente`/`CampoInvalido`): ali a operacao foi entregue e voltou
julgada como impossivel de aplicar, e e exatamente para encerrar esse caso que o
teto existe. Ate a correcao deste achado ele era o unico desfecho que NAO
contava, porque a contabilidade so era alcancada no retorno normal da chamada —
e a linha ruim girava para sempre. Ver `_retomar_uma`.

DEAD-LETTER
-----------
Ao esgotar `TETO_TENTATIVAS` tentativas REAIS, a linha para de ser reagendada.
O mecanismo esta no PREDICADO de elegibilidade (`attempt_count <
TETO_TENTATIVAS`), nao num status novo: ver "O TETO DE TENTATIVAS VIVE NO
PREDICADO" em `worker_fila.py`. Aqui so se zera `next_attempt_at` (sinal de
"sem proxima tentativa agendada") e se emite o alerta.

ALERTAS — POR NIVEL DE LOG, SEM MECANISMO NOVO
-----------------------------------------------
O Conversas sinaliza para operacao por `logging` (`app/logging_config.py`
instala o handler no root; `logger.warning`/`logger.error` ja e o canal que
`webhook.py`, `crm.py` e o proprio `_registrar_falha` usam). Este worker segue
o mesmo canal, so distinguindo severidade por CLASSE, como a secao 5 pede:

  - `AMBIGUO_RETRIAVEL`  -> CRITICAL ("pagina"). E a classe em que NAO se sabe
    se o CRM aplicou; um humano precisa olhar agora, porque a divergencia pode
    estar de pe neste instante.
  - dead-letter, e classe terminal -> ERROR ("ticket"). Nao ha mais nada
    automatico a fazer; entra na fila de trabalho humano, sem urgencia de
    plantao.

Nenhuma linha de log carrega dado de cliente — so ids internos e tokens do
vocabulario fechado, mesma disciplina de `_registrar_falha`.
"""
import logging
import threading
import time
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.handoff_op import HandoffOperation, StatusHandoff
from app.v2 import worker_fila
from app.v2.handoff import ClasseErroCrm, TokenErroCrm, handoff_to_human
# BIA-V2 R8/R10 — importar REGISTRA o listener `before_flush` da barreira de
# escrita operacional (o modulo se auto-registra na `SessionLocal`). E por isso
# que o import existe sem uso aparente: nenhum dos oito arquivos protegidos
# chama nada daqui, e nem deve — ver o docstring de `app/v2/compat.py`.
from app.v2 import compat  # noqa: F401

logger = logging.getLogger(__name__)

# Token de `action` dos eventos que esta retomada gera (gramatica
# `^[a-z][a-z0-9_]*$`, <= 64 — `eventos_validacao.validar_action`). Deliberado
# que seja DIFERENTE do `reason` da tentativa original: a trilha precisa
# distinguir "a BIA pediu handoff" de "o worker reapresentou o pedido".
MOTIVO_RETOMADA = "retomada_handoff"


class DesfechoRetomada(StrEnum):
    """
    Como UMA linha da fila de retomada terminou o ciclo.

    `StrEnum` e nao string solta, mesmo padrao (e mesmo motivo) do `Desfecho` do
    modulo irmao `worker_reconciliacao.py`: um typo numa comparacao por `==`
    contra literal falha em SILENCIO — nunca bate, nunca levanta, e o ciclo
    inteiro passa a nao contabilizar nada sem ninguem perceber. Comparado por
    `is` contra um membro do enum, o mesmo typo vira `AttributeError` na
    primeira execucao.
    """

    CONCLUIDA = "concluida"
    FALHADA = "falhada"
    IGNORADA = "ignorada"


@dataclass(frozen=True)
class ResultadoCiclo:
    """Contabilidade de UM ciclo — para log, teste e futura metrica."""

    reclamadas: tuple = ()
    concluidas: int = 0
    falhadas: int = 0
    dead_letters: int = 0


def _alertar(operacao: HandoffOperation) -> None:
    """Alerta por CLASSE do desfecho — ver "ALERTAS" no docstring do modulo."""
    if operacao.error_class == ClasseErroCrm.AMBIGUO_RETRIAVEL.value:
        logger.critical(
            "PAGINA: handoff em estado AMBIGUO apos retomada — nao se sabe se o CRM "
            "aplicou. operacao_id=%s conversation_id=%s target_user_id=%s "
            "error_code=%s tentativas=%s",
            operacao.id, operacao.conversation_id, operacao.target_user_id,
            operacao.error_code, operacao.attempt_count,
        )
    elif operacao.error_class == ClasseErroCrm.NAO_APLICADO_TERMINAL.value:
        logger.error(
            "TICKET: handoff recusado em definitivo pelo CRM — nenhuma retomada "
            "automatica corrige. operacao_id=%s conversation_id=%s target_user_id=%s "
            "error_code=%s",
            operacao.id, operacao.conversation_id, operacao.target_user_id,
            operacao.error_code,
        )
    elif operacao.error_code in worker_fila.CODIGOS_EXCLUSIVOS_R15:
        # Nao e alerta: a linha acabou de MUDAR DE DONO dentro da propria fila,
        # do R16 para o R15 (secao 4.2). Ha correcao automatica prevista.
        logger.info(
            "handoff passa para a fila de reconciliacao (R15). operacao_id=%s "
            "error_code=%s",
            operacao.id, operacao.error_code,
        )


def _retomar_uma(db: Session, operacao_id: int) -> DesfechoRetomada:
    """
    Retoma UMA operacao ja reclamada. Devolve um membro de `DesfechoRetomada`
    (`IGNORADA` = a linha sumiu, ou outra instancia ja a completou entre a
    reclamacao e agora — corrida benigna que o proprio `handoff_to_human`
    resolve devolvendo o resultado real).

    A chamada de rede acontece FORA de qualquer transacao aberta deste worker:
    `handoff_to_human` e o dono da sua propria transacao composta (ver o
    docstring dele, "R12 — QUEM E O DONO DA TRANSACAO COMPOSTA") e ja comita o
    log de intencao antes de falar com o CRM.

    A CONTABILIDADE E EXPLICITA, E VALE TAMBEM QUANDO A CHAMADA LEVANTA
    -------------------------------------------------------------------
    Achado HIGH da revisao de concorrencia: ate esta correcao,
    `contabilizar_tentativa` so era alcancado no caminho de RETORNO NORMAL de
    `handoff_to_human`. Um VEREDITO dele — `ValueError` de conversa apagada,
    `HandoffInconsistente` de `event_id` reusado com outros parametros,
    `CampoInvalido` de entrada fora do contrato — pulava o incremento inteiro e
    subia direto para o `except` do laco, entao a linha permanentemente ruim
    girava para sempre com `attempt_count` em 0, nunca alcancando o dead-letter
    que o docstring de `processar_ciclo` promete existir exatamente para ela.
    Provado empiricamente: 12 ciclos consecutivos, contador parado em 0.

    Por isso o `try/except` e LOCAL, em volta da chamada, e nao no laco: e aqui
    — e so aqui — que se sabe se a operacao chegou a ser ENTREGUE ao
    `handoff_to_human`. `tentou_rede` grava esse fato de forma explicita, em vez
    de deixa-lo ser inferido de "a funcao retornou sem lancar"; e o mesmo fato
    que o modulo irmao carrega em `_Passo.tentou_rede`.

    `except ValueError`, E NAO `except Exception` — A FRONTEIRA E O VEREDITO
    -----------------------------------------------------------------------
    As TRES excecoes que `handoff_to_human` documenta sao subclasses de
    `ValueError` (`HandoffInconsistente` e `CampoInvalido` herdam dele), e as
    tres dizem a MESMA coisa: esta operacao, como esta, nunca vai conseguir ser
    aplicada. Isso e um julgamento definitivo sobre a linha — e e justamente o
    que o teto de tentativas existe para encerrar. `ValueError` nao e uma
    aproximacao escolhida por conveniencia: e literalmente o vocabulario de
    excecao que o contrato de `handoff_to_human` declara.

    Qualquer OUTRA excecao (a `RuntimeError` de um crash simulado, um erro de
    infra, um bug) nao e veredito nenhum, e continua sem consumir orcamento —
    e a garantia da secao 5 que a correcao original do `attempt_count`
    estabeleceu: "crash entre reclamar e ligar para o CRM devolve a linha a
    fila", nunca esgota o teto de uma operacao que ninguem julgou. Um crash de
    verdade (OOM-kill, SIGKILL, rollout) nem chega aqui: o processo morre e
    nenhum `except` roda.

    Um early-return daqui de cima (linha sumida, ja COMPLETED) nunca entrega
    nada e por isso nunca conta.
    """
    operacao = worker_fila.carregar(db, operacao_id)
    if operacao is None or operacao.status == StatusHandoff.COMPLETED.value:
        return DesfechoRetomada.IGNORADA

    # A partir daqui a operacao FOI entregue — ver "A CONTABILIDADE E EXPLICITA".
    tentou_rede = True
    try:
        resultado = handoff_to_human(
            db,
            conversation_id=operacao.conversation_id,
            target_user_id=operacao.target_user_id,
            reason=MOTIVO_RETOMADA,
            # MESMA identidade — o ponto inteiro da retomada. Ver o docstring do modulo.
            event_id=operacao.event_id,
        )
    except ValueError:
        # `rollback` ANTES de contabilizar: a excecao pode ter deixado a
        # transacao suja (no PostgreSQL, `InFailedSqlTransaction`), e ai o
        # UPDATE do contador falharia tambem — o incremento que esta correcao
        # existe para garantir se perderia de novo, agora por outro caminho.
        db.rollback()
        worker_fila.contabilizar_tentativa(db, operacao_id, conta=tentou_rede)
        raise

    # DESFECHO: so agora o contador anda. As DUAS condicoes sao necessarias e
    # dizem coisas diferentes — `tentou_rede` e "a operacao foi entregue", o
    # termo do 429 e "a logica de negocio do CRM chegou a julga-la" (o
    # `SlowAPIMiddleware` recusa ACIMA do roteamento). Juntas elas sao a
    # definicao de tentativa REAL; nenhuma delas sozinha e.
    operacao = worker_fila.contabilizar_tentativa(
        db,
        operacao_id,
        conta=tentou_rede and resultado.error_code != TokenErroCrm.LIMITE_TAXA.value,
    )
    if operacao is None:
        return DesfechoRetomada.IGNORADA

    if resultado.sucesso:
        logger.info(
            "handoff retomado com sucesso. operacao_id=%s conversation_id=%s replayed=%s",
            operacao.id, operacao.conversation_id, resultado.replayed,
        )
        return DesfechoRetomada.CONCLUIDA

    _alertar(operacao)
    return DesfechoRetomada.FALHADA


def processar_ciclo(db: Session) -> ResultadoCiclo:
    """
    UM ciclo: reclama um lote (transacao curta) e processa cada linha.

    Cada operacao e isolada em `try/except Exception`: o laco de um worker nao
    pode morrer porque UMA linha esta ruim (conversa apagada, `event_id`
    reusado com outros parametros — `handoff_to_human` levanta `ValueError`/
    `HandoffInconsistente`/`CampoInvalido` nesses casos). A linha ja teve
    `next_attempt_at` adiado na reclamacao, entao ela volta a fila sozinha; o
    teto de tentativas e o que impede uma linha permanentemente ruim de girar
    para sempre — e quem faz o contador andar nesse caminho e o `try/except`
    LOCAL de `_retomar_uma`, nao este aqui.

    A excecao vira um desfecho `FALHADA` normal em vez de saltar o resto do
    corpo: ela ja consumiu orcamento la dentro, entao precisa passar pela
    mesma checagem de dead-letter que qualquer outro desfecho. Com um `continue`
    aqui, a linha ruim atingiria o teto e sairia da fila (o predicado de
    elegibilidade a exclui) em SILENCIO — sem o TICKET que e a unica coisa que
    leva o caso a um humano.
    """
    reclamadas = worker_fila.reclamar(
        db, criterios=worker_fila.criterios_r16(), limite=worker_fila.LOTE_R16
    )
    concluidas = falhadas = dead_letters = 0

    for operacao_id in reclamadas:
        try:
            desfecho = _retomar_uma(db, operacao_id)
        except Exception:
            db.rollback()
            logger.exception(
                "TICKET: retomada abortou com excecao inesperada. operacao_id=%s", operacao_id
            )
            desfecho = DesfechoRetomada.FALHADA

        if desfecho is DesfechoRetomada.CONCLUIDA:
            concluidas += 1
        elif desfecho is DesfechoRetomada.FALHADA:
            falhadas += 1

        operacao = worker_fila.carregar(db, operacao_id)
        if (
            operacao is not None
            and operacao.status != StatusHandoff.COMPLETED.value
            and operacao.attempt_count >= worker_fila.TETO_TENTATIVAS
        ):
            worker_fila.enviar_para_dead_letter(db, operacao)
            dead_letters += 1
            logger.error(
                "TICKET (DEAD-LETTER): handoff esgotou %s tentativas reais e nao sera mais "
                "reagendado. operacao_id=%s conversation_id=%s target_user_id=%s "
                "error_code=%s error_class=%s",
                worker_fila.TETO_TENTATIVAS, operacao.id, operacao.conversation_id,
                operacao.target_user_id, operacao.error_code, operacao.error_class,
            )

    return ResultadoCiclo(reclamadas, concluidas, falhadas, dead_letters)


def rodar(parar: threading.Event | None = None) -> None:
    """
    Laco do processo. `parar` existe para teste e para desligamento gracioso —
    sem ele o laco e infinito, que e o comportamento em producao.

    O ciclo inteiro esta dentro do `try`: uma falha de INFRAESTRUTURA (banco
    fora do ar na propria reclamacao) nao pode derrubar o processo, senao o
    supervisor entraria em crash loop justamente quando ha mais fila acumulando.
    """
    logger.info("worker de retomada (R16) iniciado — ciclo de %ss", worker_fila.CICLO_SEGUNDOS)
    while parar is None or not parar.is_set():
        db = SessionLocal()
        try:
            resultado = processar_ciclo(db)
            if resultado.reclamadas:
                logger.info(
                    "ciclo de retomada: %s reclamada(s), %s concluida(s), %s falha(s), "
                    "%s dead-letter(s)",
                    len(resultado.reclamadas), resultado.concluidas, resultado.falhadas,
                    resultado.dead_letters,
                )
        except Exception:
            logger.exception("ciclo de retomada abortou; proximo ciclo segue normalmente")
        finally:
            db.close()

        if parar is None:
            time.sleep(worker_fila.CICLO_SEGUNDOS)
        elif parar.wait(worker_fila.CICLO_SEGUNDOS):
            break


if __name__ == "__main__":  # pragma: no cover - ponto de entrada do processo
    from app.logging_config import configurar_logging

    configurar_logging()
    rodar()
