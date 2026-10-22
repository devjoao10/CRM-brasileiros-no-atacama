"""
BIA-V2 R15 — worker de RECONCILIACAO entre Conversas e CRM.

Fonte normativa: `bia-v2-r15-r17-design-v4.md`, secoes 4, 4.2 e 6.2. Irmao de
`worker_retomada.py` (R16): mesmo protocolo de reclamacao, fatias DISJUNTAS da
mesma fila — as duas definidas juntas em `worker_fila.py`, nunca aqui.

DUAS DIRECOES, UM UNICO MECANISMO DE ESCRITA
---------------------------------------------
Politica hibrida deterministica (secao 4): uma politica pura "Conversas sempre
cede" e impossivel quando um humano ja respondeu; uma politica pura "CRM sempre
cede" e errada (tiraria o lead do humano ATIVO). Entao a direcao depende de
QUAL das duas pontas ficou para tras, e isso e legivel no `error_code` da falha:

- **Direcao A — Conversas converge para o CRM** (`CRM_LEAD_JA_ATRIBUIDO`): o CRM
  recusou nosso handoff porque o lead ja tem outro dono. Quem esta atrasado
  somos nos. Estado alvo: `WAITING_HUMAN` com os campos alinhados ao que o CRM
  JA tem. `operation_id = uuid5(NS, "convergencia:<event_id>")`.
- **Direcao B — CRM converge para o Conversas** (`ESTADO_LOCAL_RECUSADO` com
  `atendente_id != target_user_id`): o CRM aplicou, mas a escrita local foi
  recusada — tipicamente porque um humano assumiu a conversa no meio do voo.
  Quem esta atrasado e o CRM. Estado alvo: `lead.responsavel_id =
  conversation.atendente_id`. `operation_id = uuid5(NS,
  "correcao:<event_id>:<target_user_id>")`.

`sync_responsavel_to_crm` E PROIBIDO AQUI — ACHADO DE SEGURANCA DA REVISAO
--------------------------------------------------------------------------
As DUAS direcoes escrevem `lead.responsavel_id` por UM caminho so:
`handoff_to_human()` / o endpoint HTTP protegido por compare-and-swap.
`conversas/app/services/crm.py::sync_responsavel_to_crm` existe, e usado hoje
pela UI legada, e e exatamente o nome "obvio" a que se chegaria para
"sincronizar responsavel com o CRM" — mas escreve DIRETO no Postgres
compartilhado, sem `FOR UPDATE`, sem CAS e sem gravar no livro-razao. Usa-lo
aqui contornaria em silencio TODAS as garantias que o desenho prova. Este
modulo nunca o importa e nunca o menciona; `tests/test_v2_workers.py` guarda
isso por AST, nao por convencao.

OS DOIS COMPONENTES DA CHAVE DERIVADA DA DIRECAO B SAO OBRIGATORIOS
--------------------------------------------------------------------
`correcao:<event_id>:<target_user_id>` — e nao so `<event_id>`, como a primeira
versao do desenho propunha. O livro-razao do CRM amarra
`event_id -> (lead_id, target_user_id)` PERMANENTEMENTE
(`_conferir_parametros`). Se o alvo local mudar entre duas tentativas de
correcao (outro humano assume a conversa no meio do caminho), uma chave
derivada so do `event_id` tentaria reaplicar um `target_user_id` diferente do
que ja esta gravado sob aquela chave: `409 IDEMPOTENCY_PAYLOAD_CONFLICT`,
TERMINAL por definicao — a reconciliacao nunca convergiria, `reconciled_at`
ficaria NULL para sempre, e o desenho exige que "nenhum caminho exija edicao
manual". Com o alvo na derivacao, um alvo diferente produz uma OPERACAO
diferente em vez de um conflito permanente.

CRASH NO MEIO E SEGURO PORQUE A CHAVE E DETERMINISTICA
-------------------------------------------------------
`uuid5` e funcao pura: a mesma entrada sempre produz a mesma saida. Se o
processo morrer depois de chamar o CRM e antes de marcar `reconciled_at`, o
ciclo seguinte recomputa EXATAMENTE o mesmo `operation_id` e reapresenta a
MESMA operacao — que o livro-razao do CRM ja reconhece. Nao ha efeito
duplicado a evitar aqui, e por isso este modulo nao reimplementa nenhum
mecanismo de idempotencia proprio: ele so precisa usar a chave certa.

A operacao DERIVADA e uma linha de primeira classe em `handoff_operations`,
com ciclo de vida proprio. Se ela falhar por motivo retriavel, quem a retoma e
o R16, pelo caminho normal — nao ha tratamento especial, e nao deve haver.

NAO E UMA ROTA HTTP. Processo interno separado, laco proprio de 30s:

    python -m app.v2.worker_reconciliacao       # a partir de `conversas/`
"""
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.conversation import Conversation
from app.models.handoff_op import HandoffOperation
from app.services.crm import get_leads_responsaveis
from app.v2 import worker_fila
from app.v2.contratos import HandoffResult
from app.v2.handoff import _ERRO_ESTADO_LOCAL, ClasseErroCrm, TokenErroCrm, handoff_to_human
# BIA-V2 R8/R10 — importar REGISTRA o listener `before_flush` da barreira de
# escrita operacional (o modulo se auto-registra na `SessionLocal`). E por isso
# que o import existe sem uso aparente: nenhum dos oito arquivos protegidos
# chama nada daqui, e nem deve — ver o docstring de `app/v2/compat.py`.
from app.v2 import compat  # noqa: F401

logger = logging.getLogger(__name__)

# Namespace do `uuid5` das operacoes derivadas. CONSTANTE FIXA, escrita UMA
# VEZ, nunca recalculada em runtime (achado LOW da revisao, secao 4): dois
# implementadores escolhendo valores diferentes quebrariam em silencio a
# reprodutibilidade que torna o crash acima seguro. O valor foi obtido uma
# unica vez com `uuid.uuid5(uuid.NAMESPACE_DNS, "bia-v2.r15.reconciliacao")` e
# CONGELADO como literal — a expressao que o gerou fica registrada aqui so como
# procedencia; recalcula-la a cada import seria a recomputacao proibida.
NS_RECONCILIACAO = uuid.UUID("f9359bd6-0e1c-5939-ac10-5d97e8ec78f3")

# Tokens de `action` dos eventos gerados (gramatica `^[a-z][a-z0-9_]*$`, <= 64).
MOTIVO_CONVERGENCIA = "reconciliacao_convergencia"
MOTIVO_CORRECAO = "reconciliacao_correcao"

# Releitura, so dentro deste worker, de um `409 LEAD_ALREADY_ASSIGNED` comum
# (secao 4/6.2). NAO e um token que o CRM emite — o CRM manda o MESMO token do
# Caso 4/5, e e o Conversas que o interpreta diferente conforme o caminho de
# codigo que o recebeu, porque so o Conversas sabe se a chamada era um handoff
# primario ou uma correcao de reconciliacao.
_RECONCILIADO_JA_DIVERGIU = "RECONCILIADO_JA_DIVERGIU"


class Desfecho(StrEnum):
    """Como UMA linha da fila de reconciliacao terminou o ciclo."""

    CONVERGIU = "CONVERGIU"
    DIVERGENCIA_NOVA = "DIVERGENCIA_NOVA"
    NAO_CONVERGIU = "NAO_CONVERGIU"
    IGNORADA = "IGNORADA"


@dataclass(frozen=True)
class _Passo:
    """
    Desfecho de uma direcao, MAIS os dois fatos que a contabilidade de
    tentativas precisa saber sobre a chamada ao CRM.

    O `error_code` viaja junto porque a regra do 429 (secao 5) e sobre a
    CHAMADA, nao sobre o desfecho: um `CRM_LIMITE_TAXA` nao conta como
    tentativa julgada, e sem carregar o token ate a contabilidade o worker
    queimaria orcamento por uma operacao que a logica de negocio do CRM nunca
    chegou a ver.

    `tentou_rede` E UM CAMPO, E NAO UMA INFERENCIA — achado da revisao de
    arquitetura. A condicao anterior era `error_code != CRM_LIMITE_TAXA`, o que
    fazia os EARLY RETURNS destas duas direcoes (dono do lead ilegivel no CRM,
    lead sem dono, conversa sem atendente local) contarem como tentativa: eles
    devolvem `error_code=None`, e `None != "CRM_LIMITE_TAXA"` e True. Nenhum
    deles chega a chamar `handoff_to_human` — a rede nao e tocada, ninguem
    julga nada, e ainda assim a linha caminhava para o dead-letter. `True` so
    aparece nos pontos que de fato ENTREGAM a operacao a `handoff_to_human`
    (via `_entregar_ao_handoff`); o default `False` cobre todo early-return, que
    e onde ele pertence.

    Consequencia deliberada: uma linha travada num early-return volta a fila
    indefinidamente, em vez de ser abandonada por um contador que media a coisa
    errada. Ela nao custa rede nenhuma, e as tres condicoes sao reversiveis por
    fora (o CRM volta a responder, um humano assume a conversa) — quem precisa
    ver que ela nao anda e o TICKET que cada ramo ja emite, nao um dead-letter
    silencioso.
    """

    desfecho: "Desfecho"
    error_code: str | None = None
    tentou_rede: bool = False


class _VeredictoDoHandoff(Exception):
    """
    Envelope de um VEREDITO de `handoff_to_human` — existe para carregar UM
    fato ate `_reconciliar_uma`: a operacao ja tinha sido entregue, e o que
    voltou foi o julgamento de que ela nunca vai conseguir ser aplicada.

    Sem ele, `_reconciliar_uma` teria de adivinhar, no seu `except`, se a
    excecao veio de `handoff_to_human` ou de algo ANTES dele (`_dono_no_crm`
    consulta o banco compartilhado e pode falhar sozinho, antes de qualquer
    entrega, na Direcao A) — exatamente a inferencia indireta que o campo
    `_Passo.tentou_rede` existe para eliminar.
    """


def _entregar_ao_handoff(
    db: Session,
    *,
    conversation_id: int,
    target_user_id: int,
    reason: str,
    event_id: str,
) -> HandoffResult:
    """
    Chama `handoff_to_human` MARCANDO a entrega — unico ponto deste modulo que
    pode faze-lo, para que "entregou" seja um fato de um lugar so.

    Envelopa SO `ValueError`, que e literalmente o vocabulario de excecao
    declarado por `handoff_to_human` (`HandoffInconsistente` e `CampoInvalido`
    herdam dele): as tres significam "esta operacao nunca vai conseguir ser
    aplicada", o veredito que o teto de tentativas existe para encerrar.
    Qualquer outra excecao (crash, infra, bug) sobe crua e NAO consome
    orcamento — a garantia da secao 5, "crash devolve a linha a fila".
    Encadeada com `from`, entao o `logger.exception` do laco continua mostrando
    a original e o traceback inteiro.

    Nao trata nem classifica mais nada: falha de CRM e recusa de estado local
    NAO levantam — voltam como `HandoffResult(sucesso=False)` e seguem pelo
    caminho normal de cada direcao.
    """
    try:
        return handoff_to_human(
            db,
            conversation_id=conversation_id,
            target_user_id=target_user_id,
            reason=reason,
            event_id=event_id,
        )
    except ValueError as erro:
        raise _VeredictoDoHandoff(str(erro)) from erro


@dataclass(frozen=True)
class LeituraDono:
    """
    Resultado de ler `leads.responsavel_id` no CRM.

    `lido` e `responsavel_id` sao perguntas DIFERENTES e a distincao e
    obrigatoria (sub-caso G5, secao 2.1.1): `responsavel_id=None` e um valor
    LEGITIMO de comparacao — "o lead nao tem dono", estado real entre um
    release e a reatribuicao seguinte — e nao pode ser confundido com
    `lido=False`, que e "nao consegui perguntar". Um `None` tratado como
    ausencia de dado faria o worker suprimir ou inventar convergencia.
    """

    lido: bool
    responsavel_id: int | None = None


@dataclass(frozen=True)
class ResultadoCiclo:
    """Contabilidade de UM ciclo — para log, teste e futura metrica."""

    reclamadas: tuple = ()
    convergidas: int = 0
    divergencias_novas: int = 0
    pendentes: int = 0


def _dono_no_crm(db: Session, lead_id: int | None) -> LeituraDono:
    """
    Le `leads.responsavel_id` do CRM — LEITURA, nunca escrita.

    Reusa `crm.get_leads_responsaveis`, a mesma funcao (e a mesma query) que o
    inbox e o baseline do CAS (`handoff._ler_baseline_cas`) ja usam como fonte
    de verdade do responsavel. A premissa de banco compartilhado esta nomeada
    como residual no desenho (secao 10, item 6); nao se cria aqui um segundo
    caminho de leitura para a mesma coluna.

    A PROIBICAO da secao 4 e sobre ESCRITA (`sync_responsavel_to_crm`), nao
    sobre leitura: ler o dono atual e exatamente o que permite decidir se a
    divergencia foi resolvida ou se e nova.
    """
    if not lead_id or lead_id <= 0:
        return LeituraDono(lido=False)
    responsaveis = get_leads_responsaveis([lead_id], db)
    if not responsaveis or lead_id not in responsaveis:
        return LeituraDono(lido=False)
    return LeituraDono(lido=True, responsavel_id=responsaveis[lead_id]["responsavel_id"])


def marcar_reconciliado(db: Session, operacao: HandoffOperation) -> None:
    """Fecha a linha: `reconciled_at` pelo relogio do BANCO, nunca o do processo."""
    db.query(HandoffOperation).filter(HandoffOperation.id == operacao.id).update(
        {"reconciled_at": worker_fila.agora_do_banco()}, synchronize_session=False
    )
    db.commit()


def _confirmar_convergencia(
    db: Session,
    operacao: HandoffOperation,
    conversation: Conversation,
    *,
    alvo_local: int,
    origem: str,
) -> Desfecho:
    """
    A COMPARACAO QUE DECIDE ENTRE "RESOLVIDO" E "DIVERGENCIA NOVA" — achado HIGH
    da revisao de fechamento (secoes 4 e 6.2), e o ponto mais sutil deste worker.

    Um `409 LEAD_ALREADY_ASSIGNED` para a correcao NAO significa "ja resolvido".
    Ele prova apenas que o dono ATUAL do lead DIFERE do que a correcao esperava
    — nao prova que ele BATE com o que a correcao estava tentando estabelecer.
    Entre a divergencia original ser detectada e a correcao rodar, um TERCEIRO
    escritor pode ter movido `lead.responsavel_id` para um valor diferente tanto
    do antigo quanto do pretendido: em especial `sync_responsavel_to_crm`, que
    continua acessivel pela UI legada (a proibicao da secao 4 vale para ESTE
    worker, nao remove o caminho para outros usos). Suprimir o ticket
    incondicionalmente esconderia essa divergencia nova PARA SEMPRE.

    Por isso a decisao e por COMPARACAO explicita, nunca pelo status sozinho:

      - dono do CRM == `alvo_local`  -> convergencia REAL. Classificado
        `SUPERADO_TERMINAL` (`RECONCILIADO_JA_DIVERGIU`): esta operacao
        especifica deixou de ser a fonte de verdade, mas o estado desejado
        existe. Marca `reconciled_at`, SEM ticket.
      - dono do CRM != `alvo_local`  -> divergencia NOVA e nao resolvida. Trata
        como `AMBIGUO_RETRIAVEL`, gera ticket e NAO marca `reconciled_at`.
      - nao foi possivel ler o dono -> nao se afirma nada; a linha volta a fila.

    A mesma comparacao serve ao desfecho `ESTADO_LOCAL_RECUSADO` da propria
    correcao (ver `_corrigir_crm_para_conversas`), porque a pergunta e
    identica: "o CRM ja tem o valor que a Direcao B queria estabelecer?".
    """
    leitura = _dono_no_crm(db, conversation.lead_id)
    if not leitura.lido:
        logger.warning(
            "reconciliacao nao pode confirmar convergencia (dono do lead ilegivel). "
            "operacao_id=%s lead_id=%s origem=%s",
            operacao.id, conversation.lead_id, origem,
        )
        return Desfecho.NAO_CONVERGIU

    if leitura.responsavel_id == alvo_local:
        logger.info(
            "reconciliacao: %s (%s) — o CRM ja tem o alvo local. operacao_id=%s "
            "lead_id=%s alvo=%s origem=%s",
            _RECONCILIADO_JA_DIVERGIU, ClasseErroCrm.SUPERADO_TERMINAL.value,
            operacao.id, conversation.lead_id, alvo_local, origem,
        )
        marcar_reconciliado(db, operacao)
        return Desfecho.CONVERGIU

    logger.error(
        "TICKET: divergencia NOVA na reconciliacao (%s) — o dono no CRM nao e nem o "
        "esperado nem o pretendido; houve um terceiro escritor. operacao_id=%s "
        "lead_id=%s dono_crm=%s alvo_local=%s origem=%s",
        ClasseErroCrm.AMBIGUO_RETRIAVEL.value, operacao.id, conversation.lead_id,
        leitura.responsavel_id, alvo_local, origem,
    )
    return Desfecho.DIVERGENCIA_NOVA


def _convergir_conversas_para_crm(
    db: Session, operacao: HandoffOperation, conversation: Conversation
) -> _Passo:
    """
    DIRECAO A — o CRM ja decidiu; o Conversas adota a decisao dele.

    O alvo e o dono que o CRM tem AGORA, nao o `target_user_id` da operacao
    original (que e justamente o que o CRM recusou). Com esse alvo,
    `handoff_to_human` manda um CAS cujo baseline e o proprio dono atual —
    entao a escrita no CRM e um no-op que passa, e o efeito real e local:
    a conversa vai para `WAITING_HUMAN` com `atendente_id`/`responsavel_id`
    iguais ao dono do CRM.

    Se o CRM esta SEM dono agora, esta direcao nao tem para onde convergir
    (`handoff_to_human` exige `target_user_id >= 1`; nao existe "handoff para
    ninguem"). A linha volta a fila em vez de forcar um alvo inventado. Os DOIS
    early-returns abaixo saem com `tentou_rede=False` (o default): nenhum deles
    entrega a operacao, entao nenhum consome orcamento de tentativas — e a
    linha volta a fila INDEFINIDAMENTE, nao "ate o teto". Ver o docstring de
    `_Passo`; o que leva o caso a um humano e o TICKET do segundo ramo.
    """
    leitura = _dono_no_crm(db, conversation.lead_id)
    if not leitura.lido:
        logger.warning(
            "reconciliacao (Direcao A) adiada: dono do lead ilegivel no CRM. "
            "operacao_id=%s lead_id=%s",
            operacao.id, conversation.lead_id,
        )
        return _Passo(Desfecho.NAO_CONVERGIU)
    if leitura.responsavel_id is None:
        logger.error(
            "TICKET: reconciliacao (Direcao A) sem alvo — o CRM recusou por lead ja "
            "atribuido, mas agora o lead esta SEM dono. operacao_id=%s lead_id=%s",
            operacao.id, conversation.lead_id,
        )
        return _Passo(Desfecho.NAO_CONVERGIU)

    resultado = _entregar_ao_handoff(
        db,
        conversation_id=operacao.conversation_id,
        target_user_id=leitura.responsavel_id,
        reason=MOTIVO_CONVERGENCIA,
        event_id=str(uuid.uuid5(NS_RECONCILIACAO, f"convergencia:{operacao.event_id}")),
    )
    if resultado.sucesso:
        logger.info(
            "reconciliacao (Direcao A) convergiu: conversa alinhada ao dono do CRM. "
            "operacao_id=%s lead_id=%s alvo=%s",
            operacao.id, conversation.lead_id, leitura.responsavel_id,
        )
        marcar_reconciliado(db, operacao)
        return _Passo(Desfecho.CONVERGIU, tentou_rede=True)

    logger.warning(
        "reconciliacao (Direcao A) nao convergiu nesta tentativa. operacao_id=%s "
        "error_code=%s",
        operacao.id, resultado.error_code,
    )
    return _Passo(Desfecho.NAO_CONVERGIU, resultado.error_code, tentou_rede=True)


def _corrigir_crm_para_conversas(
    db: Session, operacao: HandoffOperation, conversation: Conversation
) -> _Passo:
    """
    DIRECAO B — o humano local ja esta atendendo; o CRM adota essa realidade.

    Premissa da direcao (secao 4): `atendente_id != target_user_id`. Quando ela
    NAO vale, nao ha o que corrigir por HANDOFF:
      - `atendente_id == target_user_id` -> as duas pontas concordam com o alvo
        original NA LEITURA LOCAL — mas fechar so por isso e o MESMO erro que o
        docstring de `_confirmar_convergencia` documenta para o outro ramo
        (achado do fechamento desta revisao): entre a reclamacao desta linha e
        este ciclo, um terceiro escritor (ex.: `PUT /api/leads/{id}/responsavel`,
        o endpoint legado) pode ter movido `lead.responsavel_id` no CRM para um
        valor diferente. Por isso este caso TAMBEM passa por
        `_confirmar_convergencia` — releitura FRESCA do CRM, nunca a suposicao
        de que "a premissa nao bateu" ja implica "o CRM tem o valor certo". Sem
        isso, `reconciled_at` fecharia a linha para sempre com a divergencia
        real ainda de pe.
      - `atendente_id is None` -> a recusa local nao foi por troca de humano, e
        nao existe alvo local para o qual convergir o CRM ("handoff para
        ninguem" nao e expressavel por este endpoint). Ticket; a linha volta a
        fila.

    Os dois saem com `tentou_rede=False` (o default), como todo early-return:
    nenhum entrega a operacao a `handoff_to_human`, entao nenhum consome
    orcamento de tentativas — inclusive o primeiro, que so LE o CRM por
    `_confirmar_convergencia` e nunca entrega nada a `handoff_to_human`. Ver o
    docstring de `_Passo`.

    SUCESSO NAO E `resultado.sucesso`, E O ESTADO DO CRM. O alvo declarado da
    Direcao B e `lead.responsavel_id = conversation.atendente_id` — nada alem
    disso. E `handoff_to_human` faz DUAS coisas: escreve no CRM E aplica o
    estado local. A segunda pode legitimamente ser recusada aqui (se o humano ja
    respondeu, a conversa esta em `HUMAN_ACTIVE`, de onde `WAITING_HUMAN` nem e
    alcancavel no grafo de `TRANSICOES_PERMITIDAS`) — e nesse caso a chamada
    devolve `ESTADO_LOCAL_RECUSADO` mesmo tendo o CRM ja aplicado a correcao,
    que era o objetivo INTEIRO desta direcao. Declarar fracasso ali deixaria a
    linha girando ate o dead-letter com a divergencia JA resolvida. Por isso o
    desfecho e decidido lendo o dono no CRM (`_confirmar_convergencia`), nao
    pelo booleano da chamada.
    """
    alvo_local = conversation.atendente_id
    if alvo_local == operacao.target_user_id:
        # NAO fecha so pela leitura local (achado do fechamento desta revisao):
        # entre a reclamacao desta linha e este ponto, um terceiro escritor pode
        # ter movido `lead.responsavel_id` no CRM. A mesma comparacao de
        # `_confirmar_convergencia` decide se a convergencia e REAL agora.
        # `tentou_rede` fica no default (False): nenhuma entrega a
        # `handoff_to_human` foi feita aqui, so a leitura que a funcao ja faz.
        return _Passo(
            _confirmar_convergencia(
                db, operacao, conversation,
                alvo_local=alvo_local, origem="SEM_DIVERGENCIA_LOCAL",
            )
        )
    if alvo_local is None:
        logger.error(
            "TICKET: reconciliacao (Direcao B) sem alvo local — a conversa nao tem "
            "atendente para o qual convergir o CRM. operacao_id=%s conversation_id=%s",
            operacao.id, operacao.conversation_id,
        )
        return _Passo(Desfecho.NAO_CONVERGIU)

    resultado = _entregar_ao_handoff(
        db,
        conversation_id=operacao.conversation_id,
        target_user_id=alvo_local,
        reason=MOTIVO_CORRECAO,
        # OS DOIS componentes — ver "OS DOIS COMPONENTES" no docstring do modulo.
        event_id=str(
            uuid.uuid5(NS_RECONCILIACAO, f"correcao:{operacao.event_id}:{alvo_local}")
        ),
    )
    if resultado.sucesso:
        logger.info(
            "reconciliacao (Direcao B) corrigiu o CRM para o atendente local. "
            "operacao_id=%s lead_id=%s alvo=%s",
            operacao.id, conversation.lead_id, alvo_local,
        )
        marcar_reconciliado(db, operacao)
        return _Passo(Desfecho.CONVERGIU, tentou_rede=True)

    if resultado.error_code in (TokenErroCrm.LEAD_JA_ATRIBUIDO.value, _ERRO_ESTADO_LOCAL):
        return _Passo(
            _confirmar_convergencia(
                db, operacao, conversation, alvo_local=alvo_local, origem=resultado.error_code
            ),
            resultado.error_code,
            tentou_rede=True,
        )

    logger.warning(
        "reconciliacao (Direcao B) nao convergiu nesta tentativa. operacao_id=%s "
        "error_code=%s",
        operacao.id, resultado.error_code,
    )
    return _Passo(Desfecho.NAO_CONVERGIU, resultado.error_code, tentou_rede=True)


def _reconciliar_uma(db: Session, operacao_id: int) -> Desfecho:
    """
    Reconcilia UMA linha ja reclamada, despachando pela direcao do `error_code`.

    O `except _VeredictoDoHandoff` e o espelho do `try/except` local de
    `worker_retomada._retomar_uma`, e existe pelo mesmo achado HIGH: um
    veredito de `handoff_to_human` (conversa apagada, `event_id` reusado com
    outros parametros) pulava a contabilidade inteira, porque a chamada abaixo
    so era alcancada no retorno NORMAL das duas direcoes. A linha ruim girava
    para sempre com `attempt_count` em 0. Envelopado, ele continua subindo para
    o laco (que loga o traceback e conta a linha como pendente), mas agora passa
    pela contabilidade antes — e chega ao dead-letter no quinto ciclo. Excecao
    que NAO e veredito nao passa por aqui e nao consome orcamento; ver
    `_entregar_ao_handoff`.
    """
    operacao = worker_fila.carregar(db, operacao_id)
    if operacao is None or operacao.reconciled_at is not None:
        return Desfecho.IGNORADA

    conversation = db.get(Conversation, operacao.conversation_id)
    if conversation is None:
        logger.error(
            "TICKET: reconciliacao sem conversa — a linha aponta para uma conversa que "
            "nao existe mais. operacao_id=%s conversation_id=%s",
            operacao.id, operacao.conversation_id,
        )
        return Desfecho.NAO_CONVERGIU

    try:
        if operacao.error_code == TokenErroCrm.LEAD_JA_ATRIBUIDO.value:
            passo = _convergir_conversas_para_crm(db, operacao, conversation)
        else:
            passo = _corrigir_crm_para_conversas(db, operacao, conversation)
    except _VeredictoDoHandoff:
        # `rollback` ANTES de contabilizar: a excecao pode ter deixado a
        # transacao suja (no PostgreSQL, `InFailedSqlTransaction`), e o UPDATE
        # do contador falharia junto.
        db.rollback()
        worker_fila.contabilizar_tentativa(db, operacao_id, conta=True)
        raise

    # DESFECHO: o contador so anda depois de a operacao ter sido ENTREGUE
    # (`tentou_rede`, campo explicito — nunca inferido do `error_code`, ver o
    # docstring de `_Passo`), e nunca para um 429 — mesma regra de R16 (secao
    # 5). As duas condicoes juntas sao a definicao de tentativa REAL: entregue
    # E julgada pela logica de negocio do CRM.
    worker_fila.contabilizar_tentativa(
        db,
        operacao_id,
        conta=passo.tentou_rede and passo.error_code != TokenErroCrm.LIMITE_TAXA.value,
    )
    return passo.desfecho


def processar_ciclo(db: Session) -> ResultadoCiclo:
    """
    UM ciclo: reclama a fatia EXCLUSIVA de R15 (transacao curta, `FOR UPDATE
    SKIP LOCKED`) e reconcilia cada linha.

    Cada operacao e isolada em `try/except Exception` pelo mesmo motivo de R16:
    o laco nao pode morrer por causa de UMA linha ruim. A linha ja teve
    `next_attempt_at` adiado na reclamacao e volta a fila sozinha.

    A excecao vira um desfecho `NAO_CONVERGIU` normal em vez de saltar o resto
    do corpo: `_reconciliar_uma` ja contabilizou a tentativa la dentro, entao a
    linha precisa passar pela mesma checagem de dead-letter que qualquer outro
    desfecho. Com um `continue` aqui, ela atingiria o teto e sairia da fila em
    SILENCIO — exatamente o que o comentario do dead-letter abaixo diz ser pior
    do que nao ter teto nenhum.
    """
    reclamadas = worker_fila.reclamar(
        db, criterios=worker_fila.criterios_r15(), limite=worker_fila.LOTE_R15
    )
    convergidas = divergencias = pendentes = 0

    for operacao_id in reclamadas:
        try:
            desfecho = _reconciliar_uma(db, operacao_id)
        except Exception:
            db.rollback()
            logger.exception(
                "TICKET: reconciliacao abortou com excecao inesperada. operacao_id=%s",
                operacao_id,
            )
            desfecho = Desfecho.NAO_CONVERGIU

        if desfecho is Desfecho.CONVERGIU:
            convergidas += 1
        elif desfecho is Desfecho.DIVERGENCIA_NOVA:
            divergencias += 1
        elif desfecho is Desfecho.NAO_CONVERGIU:
            pendentes += 1

        # Dead-letter, mesma regra de R16: a linha sai da fila sozinha ao
        # atingir o teto (o predicado de elegibilidade a exclui), mas sair em
        # SILENCIO seria pior do que nao ter teto — uma divergencia que nunca
        # convergiu e exatamente o que precisa chegar a um humano.
        operacao = worker_fila.carregar(db, operacao_id)
        if (
            operacao is not None
            and operacao.reconciled_at is None
            and operacao.attempt_count >= worker_fila.TETO_TENTATIVAS
        ):
            worker_fila.enviar_para_dead_letter(db, operacao)
            logger.error(
                "TICKET (DEAD-LETTER): reconciliacao esgotou %s tentativas sem convergir e "
                "nao sera mais reagendada — a divergencia CONTINUA de pe. operacao_id=%s "
                "conversation_id=%s error_code=%s",
                worker_fila.TETO_TENTATIVAS, operacao.id, operacao.conversation_id,
                operacao.error_code,
            )

    return ResultadoCiclo(reclamadas, convergidas, divergencias, pendentes)


def rodar(parar: threading.Event | None = None) -> None:
    """
    Laco do processo. `parar` existe para teste e desligamento gracioso — sem
    ele o laco e infinito, que e o comportamento em producao.
    """
    logger.info(
        "worker de reconciliacao (R15) iniciado — ciclo de %ss", worker_fila.CICLO_SEGUNDOS
    )
    while parar is None or not parar.is_set():
        db = SessionLocal()
        try:
            resultado = processar_ciclo(db)
            if resultado.reclamadas:
                logger.info(
                    "ciclo de reconciliacao: %s reclamada(s), %s convergida(s), "
                    "%s divergencia(s) nova(s), %s pendente(s)",
                    len(resultado.reclamadas), resultado.convergidas,
                    resultado.divergencias_novas, resultado.pendentes,
                )
        except Exception:
            logger.exception("ciclo de reconciliacao abortou; proximo ciclo segue normalmente")
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
