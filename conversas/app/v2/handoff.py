"""
BIA-V2 Fase 6 — handoff BIA -> humano (`handoff_to_human`).

Fonte normativa: docs/superpowers/plans/2026-08-29-bia-v2.md, secao "Fase 6 —
Handoff".

O QUE ESTA OPERACAO GARANTE
---------------------------
Um UNICO `target_user_id` alimenta `lead.responsavel_id` (no CRM),
`conversation.atendente_id` e `conversation.responsavel_id` — o contrato NAO
aceita valores diferentes nos tres. E a conversa so chega a `WAITING_HUMAN`
depois que o CRM CONFIRMOU que aquele humano e o responsavel pelo lead: um
`WAITING_HUMAN` local sem confirmacao do CRM seria exatamente a divergencia que
a V1 produzia (cliente na fila de um dono que o CRM nao conhece).

`target_user_id` E OBRIGATORIO E NUNCA `None` (decisao ja tomada, ver Fase 5)
Quem decide o que fazer quando NAO ha ninguem elegivel e o CHAMADOR (Fase 10):
`selecionar_atendente()` devolve `None` e o chamador simplesmente nao invoca
esta funcao. Aqui, um `target_user_id` que nao seja inteiro >= 1 e recusado
alto (`CampoInvalido`), antes de qualquer escrita.

TRES COMMITS, UMA UNIDADE ATOMICA — e por que nao e um so
---------------------------------------------------------
1. `REQUESTED` + `HANDOFF_REQUESTED`, comitados ANTES de falar com o CRM.
2. (chamada HTTP ao CRM — precedida de um `db.commit()` que nao escreve nada:
   ele so encerra a transacao de LEITURA, para nao segurar conexao do pool
   durante o round-trip. Ver o comentario no proprio ponto da chamada.)
3. estado local + `HUMAN_SELECTED` + `HANDOFF_COMPLETED` + `COMPLETED`, tudo
   num UNICO `db.commit()` no fim — esta e a "unidade atomica" do plano.
   No caminho de falha, o commit 3 e substituido por `FAILED` +
   `HANDOFF_FAILED`, e o estado local NUNCA e tocado.

O commit 1 nao e um descuido do "um unico commit": e o que torna a
reconciliacao POSSIVEL. Se a linha `REQUESTED` so fosse persistida no fim, uma
morte do processo entre a chamada ao CRM e o commit local nao deixaria RASTRO
NENHUM — e o estado descrito pelo plano ("reconciliacao encontra a operacao e
completa") seria inalcancavel por construcao. E o padrao classico de log de
intencao: registra a intencao de forma duravel, age, confirma.

R12 — QUEM E O DONO DA TRANSACAO COMPOSTA
------------------------------------------
`registrar_evento(commit=False)` NAO fecha transacao: `get_db()`
(`app/database.py`) so faz `db.close()`, sem commit e sem rollback explicito.
Uma operacao composta que esquece o `db.commit()` final SOME EM SILENCIO. Esta
funcao e a dona explicita dessa transacao — cada caminho de saida (sucesso,
falha do CRM, recusa do estado local, replay) passa por um commit ou por um
`return` que nao deixou escrita pendente.

`db.flush()` EXPLICITO ANTES DE CADA `registrar_evento`, POR DOIS MOTIVOS
`begin_nested()` (dentro de `_persistir_ou_compensar`) faz flush INCONDICIONAL
do que estiver pendente na sessao. Sem o flush proprio antes:
  (a) uma falha da NOSSA escrita (UNIQUE de `handoff_operations`, CHECK de
      `conversations`) apareceria DENTRO de `registrar_evento`, como se o
      evento fosse o culpado — as vezes ja como `PendingRollbackError`, sem
      apontar para a linha que de fato violou;
  (b) o SAVEPOINT do evento nasceria numa sessao que ainda nao tem transacao
      aberta — e e exatamente essa situacao que expoe a lacuna do pysqlite
      registrada em `.claude/memory/pysqlite-savepoint-rollback-gap.md`.
Flush primeiro: o erro aparece no lugar certo e o SAVEPOINT nasce aninhado.

AS SETE INVARIANTES DE `WAITING_HUMAN` — VERIFICADAS UMA VEZ, NAO DUAS
----------------------------------------------------------------------
O plano exige verificar as sete antes de declarar `COMPLETED`:

  1. is_bot_active == False              5. queued_at IS NOT NULL
  2. atendente_id IS NOT NULL            6. primeira_resposta_humana_at IS NULL
  3. responsavel_id IS NOT NULL          7. status == "aberta"
  4. atendente_id == responsavel_id

Quem verifica as SETE e `aplicar_transicao(conversation, WAITING_HUMAN, ...)`:
o ramo `Estado.WAITING_HUMAN` de `_validar_campos_operacionais`
(`app/v2/estados.py`) testa exatamente estas sete condicoes, e testa ANTES de
mutar qualquer coisa. Reescrever a lista aqui criaria uma SEGUNDA copia da
mesma regra, livre para divergir da primeira — e a mutacao que
`aplicar_transicao` faz (`state`, `state_version`, `closed_at`) nao consegue
invalidar nenhuma delas depois. Por isso nao ha segundo bloco de checagem: o
`aplicar_transicao` E o passo "verifica as invariantes" do plano, e ele tambem
recusa a ARESTA (ex.: um humano assumiu no meio do voo e a conversa ja esta em
`HUMAN_ACTIVE`, de onde `WAITING_HUMAN` nao e alcancavel).

Os campos operacionais sao escritos por `aplicar_estado_humano`
(`app/services/atendimento.py`), que e o PONTO UNICO de escrita de
`is_bot_active`/`atendente_id`/`queued_at`/`primeira_resposta_humana_at` desde
o F-085 — mesma decisao de reuso da Fase 5 (`selecao.py`): a V2 CHAMA a V1,
nunca a modifica nem reimplementa. `responsavel_id` fica de fora daquele helper
de proposito ("responsabilidade COMERCIAL e do CRM") e por isso e escrito aqui,
DEPOIS da confirmacao do CRM. `responsavel_nome` continua sendo cache de
exibicao do caminho de sincronizacao ja existente (`services/crm.py`); esta
operacao nao inventa uma segunda fonte para ele.

ONDE `reason` FOI PARAR
------------------------
`reason` NAO entra em `payload`: aquela allowlist e FECHADA numa unica chave
(`origem`) e alarga-la para prosa livre reabriria justamente o canal de PII que
`eventos_validacao.py` fechou. `reason` e um TOKEN de vocabulario da aplicacao
(minusculas, `snake_case`, <= 64) — a mesma gramatica que `action` de
`conversation_events` e que `motivo` de `aplicar_transicao` ja exigem. Entao
ele vai, sem campo novo e sem allowlist nova:
  - na coluna `action` dos quatro eventos desta operacao, e
  - como `motivo` da transicao de estado.
Validado por `validar_action` no inicio da funcao, para que um `reason` fora da
gramatica seja recusado ANTES de qualquer escrita, e nao la na frente pela
metade da operacao. NAO e persistido em `handoff_operations`: a trilha de
eventos ja o carrega, e duplica-lo criaria duas copias que podem divergir numa
retomada com `reason` diferente.

CONCORRENCIA NO MESMO `event_id` — DUAS CORRIDAS DIFERENTES, DOIS FECHOS
--------------------------------------------------------------------------
(1) PRIMEIRA TENTATIVA (a linha ainda nao existe): duas chamadas simultaneas
    com o mesmo `event_id` NOVO sao resolvidas pelo UNIQUE de
    `handoff_operations.event_id`: uma insere (dentro de um SAVEPOINT, mesmo
    padrao de `eventos.py::_persistir_ou_compensar`), a outra recebe
    `IntegrityError` no flush (dentro de `_abrir_tentativa`) e nao aplica nada.
    A excecao e mapeada para `EventoDuplicado` — mesmo tratamento deliberado
    que `EventoDuplicado` ja recebe em `main.py` (409), reaproveitado aqui em
    vez de inventar um vocabulario novo so para este segundo UNIQUE.

(2) RETOMADA/RETRY (a linha JA EXISTE, em `REQUESTED`) — achado F2 da revisao
    da Fase 6, corrigido aqui. O UNIQUE do item (1) so protege o INSERT; NAO
    protege duas chamadas que encontram a MESMA linha `REQUESTED` ja existente
    (retomada apos crash, ou retry de uma `FAILED` reaberta por
    `_abrir_tentativa`) e as DUAS prosseguem. Sem trava nenhuma, cada uma
    chamaria o CRM (idempotente, sem problema ali) e cada uma aplicaria
    `aplicar_transicao`+`HUMAN_SELECTED`+`HANDOFF_COMPLETED` de novo —
    `state_version` incrementando duas vezes (a maquina de estados PERMITE a
    self-transition `WAITING_HUMAN -> WAITING_HUMAN`, entao a segunda chamada
    reaplicaria sem violar invariante nenhuma), evento duplicado,
    contradizendo esta propria docstring.

    UM CLAIM ANTECIPADO ("REQUESTED -> RESUMING", comitado antes da chamada ao
    CRM) FOI CONSIDERADO E REJEITADO. Fecharia esta corrida, mas abriria outra
    pior: um crash exatamente entre o claim e o commit final (o MESMO ponto
    que o cenario "12" deste modulo simula e que a retomada existe para
    tornar recuperavel) deixaria a operacao PRESA em `RESUMING` PARA SEMPRE —
    nenhum mecanismo reclama de volta um claim orfao sem introduzir uma
    politica de expiracao por tempo (lease/heartbeat), complexidade que o
    problema nao pede. Um status transitorio novo tambem exigiria migrar o
    vocabulario fechado de `StatusHandoff` (model + m016 + gate) para um
    conceito que so existe para durar milissegundos — custo permanente de
    schema para um problema resolvido de forma mais simples abaixo.

    FECHADO SEM CLAIM ANTECIPADO E SEM STATUS NOVO: `_finalizar_ou_ceder_para_
    vencedor` torna o COMMIT FINAL de cada tentativa CONDICIONAL —
    `UPDATE handoff_operations SET status=:desfecho WHERE id=:id AND status !=
    'COMPLETED'`, executado DENTRO da MESMA transacao que contem o estado
    local (ja `flush()`ado) e os eventos (`HUMAN_SELECTED`+
    `HANDOFF_COMPLETED`, ou `HANDOFF_FAILED`) desta tentativa. Se
    `rowcount == 1`: ninguem mais concluiu antes de nos, o commit vale e
    persiste TUDO (estado local + eventos + status). Se `rowcount == 0`:
    outra chamada JA COMPLETOU esta operacao — `db.rollback()` desfaz TODO o
    trabalho especulativo desta tentativa (o `flush()` de `_aplicar_estado_
    local` incluido, mesmo que baseado num retrato da conversa que outra
    escrita concorrente ja tornou obsoleto) e devolvemos o resultado REAL,
    ja commitado por quem venceu (`replayed=True`).

    A exclusao mutua nasce do PROPRIO commit atomico de cada tentativa — nao
    de uma trava separada que pode ficar presa se o processo morrer no meio.
    Um crash A QUALQUER MOMENTO antes deste commit final deixa
    `handoff_operations` exatamente onde a ultima tentativa BEM-SUCEDIDA a
    deixou (tipicamente `REQUESTED`, do commit 1) — sempre recuperavel por
    uma nova chamada, exatamente como antes desta correcao.

Nada e aplicado duas vezes em nenhum dos dois casos.

R-INTENT: nenhuma decisao aqui ramifica sobre texto livre de
`AIInterpretationResult.intent` — esta funcao nem sequer recebe esse objeto.
"""
import logging
from dataclasses import dataclass
from enum import StrEnum

import httpx
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import BIA_V2_CRM_API_KEY, CRM_BASE_URL
from app.database import IS_SQLITE
from app.models.conversation import Conversation
from app.models.handoff_op import HandoffOperation, StatusHandoff
from app.services.atendimento import aplicar_estado_humano
from app.services.crm import get_leads_responsaveis
from app.v2.contratos import HandoffResult
from app.v2.estados import Estado, TransicaoInvalida, aplicar_transicao
from app.v2.eventos import EventoDuplicado, registrar_evento
from app.v2.eventos_validacao import (
    ResultadoEvento,
    TipoEvento,
    validar_action,
    validar_event_id,
    validar_inteiro,
)

logger = logging.getLogger(__name__)

# Contrato HTTP fixado com o lado CRM (implementado em
# `app/routers/v2_lead_domain.py`, no outro app):
#   POST /api/v2/lead-domain/handoff
#   X-API-Key: <chave>
#   {"event_id": <uuid>, "lead_id": <int>, "target_user_id": <int>,
#    "expected_responsavel_id": <int|null>}   <- R-B, secao 2.1.2 do desenho:
#       baseline do compare-and-swap. Campo AUSENTE e diferente de `null`:
#       ausente = "nao sei / nao tenho baseline" (o CRM aplica incondicional,
#       comportamento pre-R-B); `null` = "espero que o lead esteja SEM dono".
#       Ver `_ler_baseline_cas`.
#   200 -> {"lead_id", "target_user_id", "event_id", "replayed",
#           "current_responsavel_id"}  <- NUNCA ecoa `expected_responsavel_id`
#   409 -> {"detail": {"error": "IDEMPOTENCY_PAYLOAD_CONFLICT"|"LEAD_ALREADY_ASSIGNED",
#                      "current_responsavel_id": ..., "current_owner_event_id": ...,
#                      "current_owner_applied_at": ...}}  <- unico status desta
#       rota discriminado por CORPO; ver `_classificar_conflito`.
_CAMINHO_CRM = "/api/v2/lead-domain/handoff"
_CABECALHO_API_KEY = "X-API-Key"
# Timeout CURTO de proposito: esta chamada acontece com o cliente esperando no
# WhatsApp, e o caminho de falha e seguro (registra FAILED, nao aplica estado).
_TIMEOUT_CRM = httpx.Timeout(10.0, connect=5.0)

# ---------------------------------------------------------------------------
# Vocabulario FECHADO de motivos de falha - mesmo espirito dos cinco tokens de
# `gate.py`. Formato `^[A-Z][A-Z0-9_]*$` porque estes valores vao para
# `conversation_events.error_code` (`eventos_validacao.validar_error_code`) e
# para `handoff_operations.error_code`. NENHUM carrega dado do cliente nem
# corpo de resposta do CRM: o token diz o MODO da falha, nunca o conteudo.
# ---------------------------------------------------------------------------
class TokenErroCrm(StrEnum):
    """
    Os tokens que `_chamar_crm` pode devolver. `StrEnum` LOCAL a este modulo,
    mesmo precedente de `EtapaFinal`/`ResultadoPipeline` em `pipeline.py` — e
    NAO em `contratos.py`, que e deliberadamente inerte a httpx/SQLAlchemy/
    config (um teste AST guarda isso) e nao pode hospedar semantica de erro
    HTTP.

    Membro de `StrEnum` E uma `str`: toda comparacao/persistencia que ja
    existia com as constantes `_ERRO_*` abaixo continua valendo palavra por
    palavra. Escritas em banco passam `.value`, mesma disciplina de
    `StatusHandoff`.
    """

    SEM_CREDENCIAL = "CRM_SEM_CREDENCIAL"
    INDISPONIVEL = "CRM_INDISPONIVEL"
    NAO_AUTORIZADO = "CRM_NAO_AUTORIZADO"
    NAO_ENCONTRADO = "CRM_NAO_ENCONTRADO"
    CONFLITO = "CRM_CONFLITO"
    # R-B, secao 6.2 do desenho: DISTINTO de `CONFLITO`, e nao um sinonimo. A
    # selecao da Direcao A da reconciliacao (R15) usa este token como
    # predicado; colapsa-lo em `CRM_CONFLITO` (que e terminal-ticket) deixaria
    # aquela direcao morta em silencio.
    LEAD_JA_ATRIBUIDO = "CRM_LEAD_JA_ATRIBUIDO"
    LIMITE_TAXA = "CRM_LIMITE_TAXA"
    RECUSADO = "CRM_RECUSADO"
    HTTP_ERRO = "CRM_HTTP_ERRO"
    RESPOSTA_INVALIDA = "CRM_RESPOSTA_INVALIDA"


class ClasseErroCrm(StrEnum):
    """
    Como uma falha deve ser TRATADA, que e uma pergunta diferente de qual foi o
    modo dela (`TokenErroCrm`). O mesmo token muda de classe conforme a origem
    — `CRM_HTTP_ERRO` e retriavel no 400, terminal no 405 e ambiguo no 500;
    `CRM_INDISPONIVEL` e retriavel num `ConnectError` e ambiguo num
    `ReadTimeout` — e e por isso que a classe precisa de um campo proprio, nao
    de uma funcao do token (secao 8/m019 do desenho).

    `SUPERADO_TERMINAL` nunca sai de `_chamar_crm`: e a releitura que SO o
    worker de reconciliacao (R15) faz de um `LEAD_ALREADY_ASSIGNED` comum,
    depois de comparar `current_responsavel_id` com o dono local (secao 6.2).
    Fica aqui porque o vocabulario e fechado e mora num lugar so.
    """

    NAO_APLICADO_TERMINAL = "NAO_APLICADO_TERMINAL"
    NAO_APLICADO_RETRIAVEL = "NAO_APLICADO_RETRIAVEL"
    AMBIGUO_RETRIAVEL = "AMBIGUO_RETRIAVEL"
    SUPERADO_TERMINAL = "SUPERADO_TERMINAL"


@dataclass(frozen=True)
class RespostaCrm:
    """
    Desfecho de UMA chamada ao CRM. Sucesso e `RespostaCrm()` — `error_code`
    None, como o `None` que esta funcao devolvia antes desta mudanca.

    Os tres campos de dono sao ESTRITAMENTE DIAGNOSTICOS (corpo do 409, secao
    7 do desenho): existem para o log e para o worker de reconciliacao decidir
    entre "convergiu" e "divergencia nova". NAO sao persistidos, e nenhuma
    decisao deste modulo ramifica sobre eles.

    `error_class`, ao contrario dos tres, E PERSISTIDA (secao 6.2/8-m019): a
    regra de particao de fila entre R15 e R16 (secao 4.2) seleciona R16 por
    CLASSE, e o `error_code` sozinho NAO determina a classe — o mesmo token e
    retriavel, terminal ou ambiguo conforme o status que o originou. Sem a
    coluna, aquele predicado nao seria expressavel em SQL.

    `current_owner_applied_at` chega ja como texto ISO-8601 (o CRM serializa
    com `.isoformat()`): nunca um `datetime` cru, que `json.dumps` nao
    serializaria de volta.
    """

    error_code: TokenErroCrm | None = None
    error_class: ClasseErroCrm | None = None
    current_responsavel_id: int | None = None
    current_owner_event_id: str | None = None
    current_owner_applied_at: str | None = None


# Apelidos: os nomes que o resto do modulo ja usava continuam existindo, agora
# apontando para os membros do enum (uma unica definicao por token).
_ERRO_SEM_CREDENCIAL = TokenErroCrm.SEM_CREDENCIAL
_ERRO_INDISPONIVEL = TokenErroCrm.INDISPONIVEL
_ERRO_NAO_AUTORIZADO = TokenErroCrm.NAO_AUTORIZADO
_ERRO_NAO_ENCONTRADO = TokenErroCrm.NAO_ENCONTRADO
_ERRO_CONFLITO = TokenErroCrm.CONFLITO
_ERRO_LEAD_JA_ATRIBUIDO = TokenErroCrm.LEAD_JA_ATRIBUIDO
_ERRO_LIMITE_TAXA = TokenErroCrm.LIMITE_TAXA
_ERRO_RECUSADO = TokenErroCrm.RECUSADO
_ERRO_HTTP = TokenErroCrm.HTTP_ERRO
_ERRO_RESPOSTA_INVALIDA = TokenErroCrm.RESPOSTA_INVALIDA
# NAO e um token do CRM (a recusa e local, do proprio Conversas) — por isso
# fica fora de `TokenErroCrm`, mas dentro do mesmo vocabulario fechado.
_ERRO_ESTADO_LOCAL = "ESTADO_LOCAL_RECUSADO"

# Status HTTP -> token. Qualquer outro status vira `_ERRO_HTTP`: o vocabulario
# e fechado de proposito, entao nao se interpola o status no token. 409 NAO
# entra aqui: e o unico status desta rota discriminado pelo CORPO (secao 6.2
# do desenho) — ver `_classificar_conflito`.
_TOKEN_POR_STATUS = {
    401: _ERRO_NAO_AUTORIZADO,
    403: _ERRO_NAO_AUTORIZADO,
    404: _ERRO_NAO_ENCONTRADO,
    422: _ERRO_RECUSADO,
    429: _ERRO_LIMITE_TAXA,
}

# Status HTTP -> classe, transcrita da matriz da secao 6.1 do desenho. Status
# fora da tabela cai em `AMBIGUO_RETRIAVEL`, a leitura conservadora: sem saber
# o que a resposta significa, nao da para afirmar que a escrita nao aconteceu.
_CLASSE_POR_STATUS = {
    400: ClasseErroCrm.NAO_APLICADO_RETRIAVEL,  # corpo ilegivel, transitorio
    401: ClasseErroCrm.NAO_APLICADO_TERMINAL,
    403: ClasseErroCrm.NAO_APLICADO_TERMINAL,
    404: ClasseErroCrm.NAO_APLICADO_TERMINAL,
    405: ClasseErroCrm.NAO_APLICADO_TERMINAL,
    422: ClasseErroCrm.NAO_APLICADO_TERMINAL,
    429: ClasseErroCrm.NAO_APLICADO_RETRIAVEL,  # recusado ANTES do handler
    500: ClasseErroCrm.AMBIGUO_RETRIAVEL,  # duas origens indistinguiveis
    502: ClasseErroCrm.NAO_APLICADO_RETRIAVEL,
    503: ClasseErroCrm.NAO_APLICADO_RETRIAVEL,
    504: ClasseErroCrm.AMBIGUO_RETRIAVEL,  # timeout no proxy: pode ter aplicado
}

# Sub-tokens do 409, discriminados por `detail.error` (secao 6.2). Qualquer
# outro valor — inclusive corpo malformado — cai em `_ERRO_CONFLITO`.
_TOKEN_POR_ERRO_409 = {
    "IDEMPOTENCY_PAYLOAD_CONFLICT": _ERRO_CONFLITO,
    "LEAD_ALREADY_ASSIGNED": _ERRO_LEAD_JA_ATRIBUIDO,
}


class HandoffInconsistente(ValueError):
    """
    O `event_id` ja existe, mas com `conversation_id`/`target_user_id`
    DIFERENTES dos que foram gravados na primeira vez — retry adulterado ou
    reuso indevido da chave de idempotencia. E o espelho local do 409 que o CRM
    devolve no mesmo caso.

    Nunca "resolve" o conflito escolhendo um dos dois valores: aplicar o novo
    faria a chave de idempotencia proteger uma operacao diferente da que ela
    identifica, e aplicar o antigo mandaria a conversa para um dono que quem
    chamou nao pediu.

    A mensagem NAO ecoa valor nenhum (mesma disciplina de `EventoDuplicado`);
    os dados ficam disponiveis de forma estruturada em `.event_id`.
    """

    def __init__(self, event_id: str):
        self.event_id = event_id
        super().__init__("event_id ja registrado com outros parametros")


def _resultado(
    operacao: HandoffOperation, *, sucesso: bool, replayed: bool, error_code: str | None = None
) -> HandoffResult:
    """Constroi o `HandoffResult` a partir da linha da operacao — fonte unica dos ids devolvidos."""
    return HandoffResult(
        sucesso=sucesso,
        replayed=replayed,
        conversation_id=operacao.conversation_id,
        target_user_id=operacao.target_user_id,
        event_id=operacao.event_id,
        error_code=error_code,
    )


def _abrir_tentativa(
    db: Session,
    operacao: HandoffOperation | None,
    *,
    event_id: str,
    conversation: Conversation,
    target_user_id: int,
    reason: str,
) -> HandoffOperation:
    """
    Commit 1 — log de INTENCAO duravel, antes de qualquer contato com o CRM.

    Serve tanto para a primeira tentativa (INSERT) quanto para a retomada de
    uma operacao `FAILED` (volta para `REQUESTED`, limpa o `error_code`). Nos
    dois casos emite `HANDOFF_REQUESTED`: uma segunda tentativa e um novo
    pedido, e a trilha ficaria incompreensivel se o `COMPLETED` de uma retomada
    aparecesse sem um `REQUESTED` correspondente.

    O INSERT (primeira tentativa) roda dentro de um SAVEPOINT
    (`db.begin_nested()`), mesmo padrao de `eventos.py::_persistir_ou_compensar`:
    duas chamadas simultaneas com o mesmo `event_id` NOVO colidem no UNIQUE de
    `handoff_operations.event_id`, e so o SAVEPOINT desfaz o INSERT que perdeu
    sem derrubar a transacao inteira do caller. Em `IntegrityError`, RE-CONSULTA
    por `event_id` - so vira `EventoDuplicado` se a linha REALMENTE existir
    (mesma disciplina: nao mascarar uma violacao diferente). Ver "CONCORRENCIA
    NO MESMO event_id" no docstring do modulo.
    """
    if operacao is None:
        operacao = HandoffOperation(
            event_id=event_id,
            conversation_id=conversation.id,
            target_user_id=target_user_id,
            status=StatusHandoff.REQUESTED.value,
        )
        try:
            with db.begin_nested():
                db.add(operacao)
                db.flush()
        except IntegrityError as exc:
            existe = db.query(HandoffOperation).filter_by(event_id=event_id).first()
            if existe is None:
                raise
            raise EventoDuplicado(event_id=event_id) from exc
    else:
        operacao.status = StatusHandoff.REQUESTED.value
        operacao.error_code = None
        # `error_class` acompanha `error_code` em TODA escrita: uma linha
        # `REQUESTED` que ainda carregue a classe da tentativa anterior mentiria
        # para o predicado de particao do worker (secao 4.2 do desenho).
        operacao.error_class = None
        db.flush()  # ANTES de registrar_evento — ver "db.flush() EXPLICITO" no topo.

    registrar_evento(
        db,
        tipo=TipoEvento.HANDOFF_REQUESTED,
        commit=False,
        conversation_id=conversation.id,
        lead_id=conversation.lead_id,
        target_user_id=target_user_id,
        action=reason,
    )
    db.commit()
    return operacao


def _inteiro_opcional(valor: object) -> int | None:
    """Inteiro de verdade vindo do corpo do CRM, ou `None`. `bool` e recusado
    explicitamente (mesma disciplina de `validar_inteiro`): `True` nao vira 1."""
    return valor if isinstance(valor, int) and not isinstance(valor, bool) else None


def _texto_opcional(valor: object) -> str | None:
    """Texto vindo do corpo do CRM, ou `None` para qualquer outro tipo."""
    return valor if isinstance(valor, str) else None


def _ler_baseline_cas(db: Session, lead_id: int | None) -> dict:
    """
    Baseline do compare-and-swap: o `responsavel_id` que o CRM tem AGORA para
    este lead, lido do banco compartilhado por `crm.get_leads_responsaveis` —
    a mesma funcao (e a mesma query em lote) que o inbox ja usa como fonte de
    verdade do responsavel.

    Devolve um FRAGMENTO do corpo da requisicao, nao um valor solto, porque a
    distincao que importa (secao 2.1.2 do desenho) e AUSENTE x `null`:
      - `{}`                                  -> nao ha baseline confiavel; o
        campo nao vai no corpo e o CRM aplica incondicionalmente, exatamente
        como fazia antes de R-B existir. Preferido a mandar `null`, que o CRM
        le como "espero que o lead esteja SEM dono" e faria TODA operacao
        sobre lead ja atribuido falhar o CAS por engano.
      - `{"expected_responsavel_id": None}`   -> baseline lido e o lead
        REALMENTE nao tem dono.
      - `{"expected_responsavel_id": <int>}`  -> baseline lido.

    Sem baseline confiavel em tres casos: conversa nao vinculada a lead
    (`lead_id` ausente/<=0), lead ausente do retorno, e CRM inacessivel
    (`get_leads_responsaveis` devolve `None`, ja logando o motivo — e o caso de
    dev/CI sem as tabelas do CRM). Aquele caminho faz `db.rollback()` para
    limpar a transacao abortada:
    seguro AQUI porque o commit 1 ja fechou a transacao anterior e nada desta
    operacao esta pendente na sessao.
    """
    if not lead_id or lead_id <= 0:
        return {}
    responsaveis = get_leads_responsaveis([lead_id], db)
    if not responsaveis or lead_id not in responsaveis:
        return {}
    return {"expected_responsavel_id": responsaveis[lead_id]["responsavel_id"]}


def _classificar_conflito(resposta: httpx.Response) -> RespostaCrm:
    """
    409 e o UNICO status desta rota que precisa LER o corpo (secao 6.2): dois
    sub-casos de semantica bem diferente chegam com o mesmo status, e a
    Direcao A da reconciliacao (R15) seleciona por um deles.

    DOIS guards, nao um — os dois sao necessarios e cada um cobre um formato
    diferente de corpo quebrado:
      (a) `except ValueError` — corpo nao-JSON (`JSONDecodeError` E um
          `ValueError`, mesmo guard que o caminho 200 ja usa);
      (b) `isinstance(detalhe, dict)` — JSON VALIDO com forma errada. O 409 de
          hoje manda `detail` como STRING solta; `"...".get("error")`
          levantaria `AttributeError` sem ninguem capturar. Alcancavel de
          verdade: rollback do CRM depois do Conversas ja ter subido.

    Sem token reconhecido, o fallback e `CRM_CONFLITO`/`NAO_APLICADO_TERMINAL`.
    O corpo quebrado deixa ambiguo o MOTIVO, nunca se ESTA chamada aplicou
    algo: um 409 garante que a escrita desta chamada nao aconteceu nos dois
    emissores (CAS recusado antes de qualquer commit; ou replay detectando que
    o dono mudou, caso em que a escrita aconteceu no PASSADO, sob outra
    chamada). Por isso terminal, e nao `AMBIGUO_RETRIAVEL`.
    """
    try:
        dados = resposta.json()
    except ValueError:
        dados = None
    detalhe = dados.get("detail") if isinstance(dados, dict) else None
    if not isinstance(detalhe, dict):
        return RespostaCrm(
            error_code=_ERRO_CONFLITO, error_class=ClasseErroCrm.NAO_APLICADO_TERMINAL
        )
    return RespostaCrm(
        error_code=_TOKEN_POR_ERRO_409.get(
            _texto_opcional(detalhe.get("error")), _ERRO_CONFLITO
        ),
        error_class=ClasseErroCrm.NAO_APLICADO_TERMINAL,
        current_responsavel_id=_inteiro_opcional(detalhe.get("current_responsavel_id")),
        current_owner_event_id=_texto_opcional(detalhe.get("current_owner_event_id")),
        current_owner_applied_at=_texto_opcional(detalhe.get("current_owner_applied_at")),
    )


def _chamar_crm(
    *, event_id: str, lead_id: int, target_user_id: int, baseline_cas: dict
) -> RespostaCrm:
    """
    Chama o CRM com `event_id` como idempotency key. Devolve `RespostaCrm()`
    em sucesso (`error_code is None`), ou o token de falha do vocabulario
    fechado acima MAIS a classe que diz como trata-la.

    Client SINCRONO: esta funcao inteira roda sobre uma `Session` sincrona do
    SQLAlchemy, como todos os demais modulos de `app/v2/`. Nenhuma transacao
    fica aberta durante a chamada — o commit 1 ja fechou a anterior.

    DOIS DICTS, NAO UM: `corpo_requisicao` (o que vai na rede) e `corpo_eco`
    (o que a resposta 200 precisa devolver de volta) sao objetos distintos de
    proposito. `expected_responsavel_id` entra so no primeiro — a resposta 200
    NUNCA o ecoa (secao 7 do desenho), entao conferi-lo no eco faria toda
    chamada BEM-SUCEDIDA com baseline nao-nulo (o caso "cliente que volta",
    lead que ja tem dono) virar `CRM_RESPOSTA_INVALIDA` com o CRM tendo
    aplicado corretamente. Reunificar os dois dicts reabre esse bug.

    A RESPOSTA DO CRM E ENTRADA EXTERNA, e e conferida como tal: um 200 cujo
    corpo nao ecoe os TRES parametros de identidade vira
    `CRM_RESPOSTA_INVALIDA` e NAO autoriza aplicar o estado local. Sem essa
    conferencia, um proxy, um ambiente apontado para o CRM errado ou uma rota
    trocada poderiam devolver 200 e a conversa iria para `WAITING_HUMAN` com
    um dono que o CRM nunca confirmou. `replayed` e informativo (o CRM ja
    conhecia este `event_id`) e nao muda o que fazemos aqui: o estado local
    pode nao ter sido aplicado ainda, e e justamente esse o caso de retomada.

    ORDEM DOS `except`, ESPECIFICO ANTES DE GENERICO — restricao real, nao
    estilo: uma clausula de classe-base (`RequestError`) colocada antes rouba
    a excecao e a especifica nunca dispara.
    `ConnectError`/`ConnectTimeout`/`PoolTimeout` sao `NAO_APLICADO_RETRIAVEL`
    porque a conexao nunca chegou a se estabelecer — nenhum byte saiu. Todo o
    resto de `RequestError` (`ReadTimeout`, `WriteTimeout`, `RemoteProtocol
    Error`, `DecodingError`, `ProxyError`...) e `AMBIGUO_RETRIAVEL`: nao ha
    garantia de que o servidor nao recebeu e aplicou. `InvalidURL` e
    `CookieConflict` NAO aparecem aqui de proposito — nao sao subclasses de
    `RequestError` (herdam de `Exception`), entao nenhum `except` desta
    familia as alcancaria; a primeira e erro de CONFIGURACAO (`CRM_BASE_URL`
    malformada) e a segunda e inalcancavel por este codigo.
    """
    if not BIA_V2_CRM_API_KEY or not BIA_V2_CRM_API_KEY.isascii():
        # Credencial ausente NAO e no-op silencioso aqui (ver app/config.py):
        # deixar o handoff "sumir" mantem o cliente esperando com o bot ligado.
        # `.isascii()` (achado #3 da revisao da Fase 6): um valor nao-ASCII
        # (ex.: aspas curvas de copy-paste no `.env`) faria o httpx levantar
        # `UnicodeEncodeError` — um `ValueError`, NAO um `httpx.RequestError`
        # — ao montar o header abaixo, escapando do `except` logo adiante e
        # deixando a operacao presa em `REQUESTED` sem `HANDOFF_FAILED`.
        # Tratado com o MESMO token de credencial ausente, de proposito: nos
        # dois casos a acao corretiva e identica (reconfigurar
        # `BIA_V2_CRM_API_KEY`), e um segundo token so para distinguir a
        # CAUSA nao ajudaria quem opera a decidir o que fazer.
        #
        # Terminal: nenhuma tentativa mecanica corrige credencial ausente. Fora
        # da matriz da secao 6.1 de proposito (nao chega a tocar a rede) — e o
        # proprio exemplo da regra "reclamacao que nunca chamou a rede".
        return RespostaCrm(
            error_code=_ERRO_SEM_CREDENCIAL, error_class=ClasseErroCrm.NAO_APLICADO_TERMINAL
        )

    corpo_eco = {"event_id": event_id, "lead_id": lead_id, "target_user_id": target_user_id}
    corpo_requisicao = {**corpo_eco, **baseline_cas}
    try:
        with httpx.Client(timeout=_TIMEOUT_CRM) as client:
            resposta = client.post(
                f"{CRM_BASE_URL}{_CAMINHO_CRM}",
                json=corpo_requisicao,
                headers={_CABECALHO_API_KEY: BIA_V2_CRM_API_KEY},
            )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        return RespostaCrm(
            error_code=_ERRO_INDISPONIVEL, error_class=ClasseErroCrm.NAO_APLICADO_RETRIAVEL
        )
    except httpx.RequestError:
        return RespostaCrm(
            error_code=_ERRO_INDISPONIVEL, error_class=ClasseErroCrm.AMBIGUO_RETRIAVEL
        )

    if resposta.status_code == 409:
        return _classificar_conflito(resposta)

    if resposta.status_code != 200:
        return RespostaCrm(
            error_code=_TOKEN_POR_STATUS.get(resposta.status_code, _ERRO_HTTP),
            error_class=_CLASSE_POR_STATUS.get(
                resposta.status_code, ClasseErroCrm.AMBIGUO_RETRIAVEL
            ),
        )

    try:
        dados = resposta.json()
    except ValueError:
        return RespostaCrm(
            error_code=_ERRO_RESPOSTA_INVALIDA, error_class=ClasseErroCrm.AMBIGUO_RETRIAVEL
        )

    if not isinstance(dados, dict) or any(
        dados.get(chave) != valor for chave, valor in corpo_eco.items()
    ):
        # A rota so responde 200 DEPOIS do commit — um eco quebrado quase
        # sempre e transporte/serializacao, nao "nao aplicou". Por isso
        # ambiguo, nunca `NAO_APLICADO_*`.
        return RespostaCrm(
            error_code=_ERRO_RESPOSTA_INVALIDA, error_class=ClasseErroCrm.AMBIGUO_RETRIAVEL
        )
    return RespostaCrm()


def _aplicar_estado_local(
    db: Session, conversation: Conversation, *, target_user_id: int, reason: str
) -> str:
    """
    Escreve os campos operacionais e transiciona para `WAITING_HUMAN`. Devolve
    o `state` ANTERIOR (para `state_before` dos eventos), lido antes da
    transicao.

    RELE A LINHA ANTES DE DECIDIR, e o `db.refresh()` abaixo e a parte que nao
    pode sair daqui. A chamada ao CRM leva tempo de rede, e nesse intervalo a
    conversa pode ter mudado de dono por outro caminho (um humano deu claim, a
    V1 encerrou, o gate da Fase 7 ja assumiu). Sem a releitura, as sete
    invariantes seriam conferidas contra o retrato lido ANTES da ida ao CRM, e
    `aplicar_transicao` gravaria por cima do outro writer — inclusive
    `state_version`, que voltaria atras: exatamente a escrita perdida que a
    Fase 7 existe para impedir do lado da IA. Mesma propriedade que `gate.py`
    documenta em "RELE SOB LOCK".

    COM LOCK, NAO SO RELEITURA (achado F1 da revisao da Fase 6, corrigido
    aqui) — esta era a UNICA mutacao de `conversations` no repositorio inteiro
    que relia sem `with_for_update`: `_lock_conversation`
    (`conversas/app/routers/conversations.py`) ja usa esse protocolo em
    claim/assign/release/handoff (o handoff LEGADO, via router), e `_lock_lead`
    (`app/routers/leads.py`, CRM) no equivalente de lead. Fora do SQLite,
    `db.refresh(conversation, with_for_update=True)` faz o `SELECT` desta
    releitura tomar o lock de linha no PostgreSQL: qualquer
    `claim`/`assign`/`release` concorrente que passe por `_lock_conversation`
    (ou qualquer UPDATE simples — no PostgreSQL, UPDATE sempre precisa do
    mesmo lock de linha para executar, mesmo sem pedir `FOR UPDATE`
    explicitamente) fica serializado contra este bloco. A janela protegida e
    curta de proposito: nenhuma chamada de rede acontece depois deste ponto,
    so o commit local (ou o rollback, no caminho de recusa).

    INVESTIGACAO DO CENARIO DO REVISOR (F1) — resultado REAL, nao suposicao.
    O revisor descreveu um humano respondendo (`record_outbound_message` ->
    `marcar_atendimento_humano`, `app/services/outbound.py`) DURANTE o
    round-trip HTTP ao CRM, comitando `atendente_id`+
    `primeira_resposta_humana_at` numa transacao SEPARADA, e afirmou que isso
    "passa despercebido (CHECK do banco satisfeito, ninguem falha)". Um teste
    adversarial reproduzindo EXATAMENTE esse cenario — duas sessoes reais, uma
    comitando `record_outbound_message` no meio do `_chamar_crm` da outra; ver
    `tests/test_v2_handoff.py`, cenario F1 — mostra o CONTRARIO: a chamada
    termina em `TransicaoInvalida` -> `ESTADO_LOCAL_RECUSADO`, NUNCA em
    sucesso silencioso, mesmo ANTES desta correcao. O motivo e ESTRUTURAL, nao
    um acidente do caso especifico: `marcar_atendimento_humano` grava
    `primeira_resposta_humana_at`, e a invariante 6
    (`primeira_resposta_humana_at IS NULL`) de `WAITING_HUMAN`
    (`_validar_campos_operacionais`, `app/v2/estados.py`) exige exatamente o
    OPOSTO — a resposta humana concorrente a derruba, e `aplicar_transicao`
    recusa antes de mutar qualquer coisa.

    ATUALIZADO PELO HOTFIX-01 (preservar `queued_at` na 1a resposta humana).
    Ate aquela correcao, `marcar_atendimento_humano` TAMBEM zerava `queued_at`
    (e `aplicar_estado_humano` fazia o mesmo sempre que lia
    `primeira_resposta_humana_at is not None`), entao a resposta humana
    concorrente derrubava DUAS invariantes de uma vez: a 5 (`queued_at IS NOT
    NULL`) e a 6. Hoje `queued_at` e PRESERVADO, a invariante 5 continua
    satisfeita, e so a 6 e violada. A CONCLUSAO NAO MUDA — uma invariante basta
    para `aplicar_transicao` recusar, e a recusa continua sendo antes de
    qualquer mutacao. Muda so o numero de invariantes que caem, e este
    paragrafo existe para que a prosa nao continue descrevendo o codigo antigo.

    O lock acima, entao, NAO fecha a corrida especifica que o revisor
    descreveu (a validacao de invariantes ja fechava). Fecha uma DIFERENTE,
    mais silenciosa, que a mesma investigacao expos: um `claim`/`assign`
    concorrente que so muda `atendente_id` (sem tocar
    `primeira_resposta_humana_at` — ex.: um humano clica "assumir" mas ainda
    nao respondeu) NAO colide com invariante nenhuma de `WAITING_HUMAN`, e
    `aplicar_estado_humano` sobrescreve `atendente_id` incondicionalmente para
    `target_user_id` — o claim do outro humano seria substituido em silencio,
    sem qualquer sinal de conflito. E essa corrida, nao a do revisor, que o
    lock fecha, ao serializar esta releitura contra o MESMO protocolo que
    `claim`/`assign`/`release` ja usam. Vale a correcao de qualquer forma: ela
    fecha a classe inteira de corrida na raiz, nao so o sintoma que o revisor
    apontou.

    SQLite segue o MESMO ramo `IS_SQLITE` de `_lock_conversation`/`_lock_lead`
    por CONSISTENCIA de codigo com o resto do repositorio — nao porque
    `with_for_update` levante uma excecao no SQLite. Um probe descartavel
    contra o SQLAlchemy 2.0.40 desta venv confirma que a clausula e
    SILENCIOSAMENTE IGNORADA (nenhuma excecao, com ou sem transacao aberta),
    nao levantada: o comentario em `conversas/app/database.py:21` ("sem o
    ramo, with_for_update() levanta no SQLite") esta desatualizado para esta
    combinacao de versoes. Aquele arquivo fica FORA do escopo desta correcao
    (nao esta entre os arquivos tocados nesta rodada) e por isso nao foi
    alterado — fica registrado aqui para quem revisar aquele comentario
    depois. Em qualquer caso, o SQLite ja serializa o banco INTEIRO por lock
    de arquivo, com ou sem `FOR UPDATE` — a mesma razao pela qual
    `_lock_conversation` pula `.with_for_update()` no SQLite.

    Levanta `TransicaoInvalida` sem deixar nada flushado se a conversa nao
    puder ir para `WAITING_HUMAN` — as sete invariantes e a aresta sao
    verificadas por `aplicar_transicao` ANTES de qualquer mutacao de estado, e
    as escritas de campo feitas logo acima ainda estao so na sessao (nenhum
    `db.flush()` aconteceu ate aqui, e a sessao e `autoflush=False`). Quem
    chama desfaz com `db.rollback()`.

    `keep_queue_position=True`: um retry NAO manda a conversa para o fim da
    fila — a mesma razao pela qual o handoff da V1 usa esta flag.
    """
    # `refresh`, e nao `db.get(..., populate_existing=True)` como em `gate.py`:
    # a linha apagada no meio do voo levanta `ObjectDeletedError` (falha alta e
    # rastreavel) em vez de devolver `None` e exigir um ramo de tratamento para
    # um caso que nenhum fluxo deste repositorio produz — nada apaga
    # `conversations`. `with_for_update=True` fora do SQLite — ver "COM LOCK,
    # NAO SO RELEITURA" acima.
    if IS_SQLITE:
        db.refresh(conversation)
    else:
        db.refresh(conversation, with_for_update=True)
    estado_antes = conversation.state

    aplicar_estado_humano(conversation, target_user_id, keep_queue_position=True)
    # UM unico target_user_id alimenta atendente_id E responsavel_id — a
    # invariante 4 (`atendente_id == responsavel_id`) e estrutural aqui: nao ha
    # dois valores para divergirem.
    conversation.responsavel_id = target_user_id

    aplicar_transicao(conversation, Estado.WAITING_HUMAN, motivo=reason)
    db.flush()  # o CHECK `ck_conversations_estado_humano` (m015) falha AQUI, no lugar certo.
    return estado_antes


def _reservar_desfecho_ou_ceder_para_vencedor(
    db: Session,
    operacao: HandoffOperation,
    *,
    status_final: str,
    error_code: str | None,
    error_class: str | None,
) -> HandoffResult | None:
    """
    RESERVA o DESFECHO FINAL desta tentativa (`COMPLETED` ou `FAILED`) de
    forma CONDICIONAL, ANTES de qualquer `registrar_evento` desta tentativa —
    ordem que e parte da correcao, nao um detalhe (ver "ORDEM: ANTES DE
    registrar_evento" abaixo).

    Fecha F2 (revisao da Fase 6: duas chamadas concorrentes com o MESMO
    `event_id` sobre uma operacao ainda em voo aplicavam o efeito local DUAS
    VEZES) SEM um claim antecipado e SEM status transitorio novo — ver
    "CONCORRENCIA NO MESMO event_id" no docstring do modulo para o porque de
    um claim antecipado (`REQUESTED` -> `RESUMING`, comitado ANTES da chamada
    ao CRM) ter sido CONSIDERADO E REJEITADO: ele fecharia esta corrida mas
    abriria outra pior (operacao presa para sempre se o processo morresse
    entre o claim e o commit final).

    MECANISMO: `UPDATE handoff_operations SET status=:status_final,
    error_code=:error_code, error_class=:error_class WHERE id=:id AND status !=
    'COMPLETED'`, como parte
    da MESMA transacao que contem o resto do trabalho desta tentativa (nunca
    commita sozinho). `error_class` viaja NA MESMA escrita que `error_code` de
    proposito (secao 6.2 do desenho): dois `UPDATE`s separados abririam uma
    janela em que a linha teria codigo de uma tentativa e classe de outra, e e
    exatamente o par (codigo, classe) que a particao de fila do §4.2 le para
    decidir qual worker e dono da linha. So exclui `COMPLETED` (nunca `REQUESTED`/`FAILED`)
    porque e o UNICO desfecho com efeito colateral irreversivel em
    `conversations` — sobrescreve-lo perderia a aplicacao real. Duas
    tentativas concorrentes ambas terminando em `FAILED` (ex.: CRM fora do ar
    para as duas) podem legitimamente sobrescrever uma a outra aqui: nenhuma
    tocou `conversations`, entao nao ha estado para divergir — so um
    `HANDOFF_FAILED` redundante na trilha, tolerado (o design anterior a esta
    correcao ja tolerava isso).

    ORDEM: ANTES DE `registrar_evento`, NUNCA DEPOIS — restricao real, nao
    estilo. `registrar_evento` insere dentro de um SAVEPOINT
    (`db.begin_nested()`) que e LIBERADO (`RELEASE`) assim que retorna com
    sucesso. `.claude/memory/pysqlite-savepoint-rollback-gap.md` documenta,
    verificado empiricamente contra o SQLite deste repositorio (sem o fix de
    `isolation_level`/`BEGIN` manual em `database.py`), que um `db.rollback()`
    chamado DEPOIS que um SAVEPOINT ja foi liberado NAO desfaz a linha que
    aquele SAVEPOINT inseriu — a linha sobrevive ao rollback. Chamar esta
    funcao SO ANTES de qualquer `registrar_evento` da tentativa garante que,
    se ela devolver um resultado (perdemos a corrida) e quem chama nunca tiver
    chegado a registrar evento nenhum ainda, o `db.rollback()` abaixo so
    precisa desfazer flushes SIMPLES (o desta funcao, e o de
    `_aplicar_estado_local` no caminho de sucesso) — nunca um SAVEPOINT
    liberado. Mover esta chamada para DEPOIS de `registrar_evento`
    reintroduziria exatamente essa lacuna, silenciosamente, so no SQLite (dev/
    CI) — testes que passam sem provar a garantia que a docstring promete.

    Devolve `None` se ESTA chamada reservou o desfecho (`rowcount==1`): quem
    chama prossegue - registra os eventos desta tentativa e da `db.commit()`.

    Devolve um `HandoffResult` PRONTO (`sucesso=True, replayed=True`) se
    `rowcount==0` — outra chamada concorrente JA tinha `COMPLETED` a operacao
    antes de nos. Chama `db.rollback()` primeiro, desfazendo o que esta
    tentativa ja tiver flushado ate aqui (o `flush()` de `_aplicar_estado_
    local`, no caminho de sucesso — plano, nao aninhado, portanto seguro de
    desfazer mesmo no SQLite) — nunca commitamos um desfecho que ja deixou de
    ser o real. `sucesso=True, replayed=True` mesmo quando ESTA tentativa
    especificamente calculou uma FALHA: o `event_id` identifica a OPERACAO,
    nao esta chamada HTTP especifica, e a operacao, do ponto de vista de quem
    pergunta por ela agora, TERMINOU com sucesso.
    """
    linhas = (
        db.query(HandoffOperation)
        .filter(HandoffOperation.id == operacao.id, HandoffOperation.status != StatusHandoff.COMPLETED.value)
        .update(
            {"status": status_final, "error_code": error_code, "error_class": error_class},
            synchronize_session=False,
        )
    )
    if linhas == 1:
        return None
    db.rollback()
    db.refresh(operacao)
    return _resultado(operacao, sucesso=True, replayed=True)


def _registrar_falha(
    db: Session,
    operacao: HandoffOperation,
    *,
    conversation: Conversation,
    reason: str,
    error_code: str,
    error_class: str | None,
) -> HandoffResult:
    """
    Registro de FALHA: `FAILED` + `HANDOFF_FAILED`. O estado local nunca foi
    aplicado (ou ja foi desfeito por quem chamou), entao a conversa continua
    exatamente como estava — nunca chega perto de `WAITING_HUMAN`.

    `error_class` E PERSISTIDA JUNTO DE `error_code`, na MESMA transacao (secao
    6.2/8-m019 do desenho). Nao e informacao redundante: a regra de particao de
    fila (secao 4.2) elege R16 por CLASSE
    (`NAO_APLICADO_RETRIAVEL`/`AMBIGUO_RETRIAVEL`), e o MESMO token pode
    pertencer a classes diferentes conforme o status que o originou
    (`CRM_HTTP_ERRO` e retriavel no 400, terminal no 405, ambiguo no 500) —
    derivar a classe do codigo depois do fato e impossivel.

    `None` e um valor legitimo aqui, nao "esqueceram de passar": e o que
    `ESTADO_LOCAL_RECUSADO` grava. Aquela falha nao veio do CRM (a chamada
    HTTP teve SUCESSO; quem recusou foi a maquina de estados local), entao
    nenhum membro de `ClasseErroCrm` a descreve — `NAO_APLICADO_*` seria falso
    (o CRM APLICOU), `AMBIGUO_RETRIAVEL` seria falso (sabe-se exatamente o que
    aconteceu) e `SUPERADO_TERMINAL` e reservado a releitura que so o worker de
    reconciliacao faz. A linha e roteada por `error_code`, que e como a secao
    4.2 elege a Direcao B de R15 — e a classe NULA e o que a mantem fora da
    fila de R16 por uma segunda via, alem da exclusao explicita por codigo.

    O desfecho e RESERVADO por `_reservar_desfecho_ou_ceder_para_vencedor`
    (F2, ver docstring la e no topo do modulo) ANTES de `registrar_evento` —
    ordem obrigatoria, nao estilo (ver "ORDEM" naquela docstring). Se outra
    chamada concorrente JA tinha completado esta MESMA operacao com sucesso
    antes de nos, a falha desta tentativa e informacao OBSOLETA — devolvemos
    o resultado REAL (replay de sucesso) em vez de reportar uma falha que a
    operacao, no fim, nao teve, e SEM ter emitido `HANDOFF_FAILED` nenhum.
    """
    cedido = _reservar_desfecho_ou_ceder_para_vencedor(
        db, operacao, status_final=StatusHandoff.FAILED.value,
        error_code=error_code, error_class=error_class,
    )
    if cedido is not None:
        return cedido

    registrar_evento(
        db,
        tipo=TipoEvento.HANDOFF_FAILED,
        commit=False,
        conversation_id=conversation.id,
        lead_id=conversation.lead_id,
        target_user_id=operacao.target_user_id,
        action=reason,
        result=ResultadoEvento.FALHA,
        error_code=error_code,
    )
    db.commit()

    # Ids internos e token de vocabulario fechado — nenhum dado de cliente
    # (nome, telefone, conteudo de mensagem) entra nesta linha.
    logger.warning(
        "handoff FALHOU: conversation_id=%s target_user_id=%s error_code=%s error_class=%s",
        operacao.conversation_id, operacao.target_user_id, error_code, error_class,
    )
    return _resultado(operacao, sucesso=False, replayed=False, error_code=error_code)


def handoff_to_human(
    db: Session,
    *,
    conversation_id: int,
    target_user_id: int,
    reason: str,
    event_id: str,
) -> HandoffResult:
    """
    Passa a conversa da BIA para um humano, de ponta a ponta: registra a
    intencao, confirma com o CRM, aplica o estado local e fecha a operacao.

    `event_id` e a chave de idempotencia da OPERACAO (o mesmo valor vai ao CRM)
    e e canonicalizado como UUID. Chamar de novo com o mesmo `event_id`:
      - operacao `COMPLETED` -> devolve o resultado ja registrado, `replayed=True`,
        sem reaplicar nada e sem emitir evento;
      - operacao `REQUESTED` -> RETOMA (o processo morreu antes do commit local):
        rechama o CRM, que e idempotente, e completa dali em diante;
      - operacao `FAILED`    -> NOVA TENTATIVA sob a MESMA chave. E o que mantem
        a idempotencia util: exigir um `event_id` novo para tentar de novo faria
        o CRM tratar a nova tentativa como outra operacao, e o caso em que a
        primeira chamada CHEGOU ao CRM mas a resposta se perdeu viraria uma
        aplicacao dupla;
      - parametros diferentes -> `HandoffInconsistente`, sem efeito nenhum.

    Levanta `CampoInvalido` (entrada fora do contrato), `ValueError`
    (`conversation_id` inexistente — erro de chamador, como em `gate.py`) ou
    `HandoffInconsistente`. Falha do CRM e recusa do estado local NAO levantam:
    viram `HandoffResult(sucesso=False)` com o `error_code` do modo de falha,
    porque sao desfechos previstos da operacao e precisam ficar na trilha.
    """
    # Fronteira: TUDO validado antes da primeira escrita. `validar_inteiro`
    # recusa `bool` explicitamente (True nao vira 1) e exige >= 1.
    validar_inteiro(conversation_id, "conversation_id", minimo=1)
    validar_inteiro(target_user_id, "target_user_id", minimo=1)
    validar_action(reason)
    event_id_final = validar_event_id(event_id)

    conversation = db.get(Conversation, conversation_id)
    if conversation is None:
        raise ValueError(f"handoff_to_human: conversation_id {conversation_id} nao encontrada")

    operacao = db.query(HandoffOperation).filter_by(event_id=event_id_final).one_or_none()
    if operacao is not None:
        if operacao.conversation_id != conversation_id or operacao.target_user_id != target_user_id:
            raise HandoffInconsistente(event_id=event_id_final)
        if operacao.status == StatusHandoff.COMPLETED.value:
            return _resultado(operacao, sucesso=True, replayed=True)

    if operacao is None or operacao.status == StatusHandoff.FAILED.value:
        operacao = _abrir_tentativa(
            db, operacao,
            event_id=event_id_final, conversation=conversation,
            target_user_id=target_user_id, reason=reason,
        )

    # `lead_id` num local ANTES do release: depois dele o objeto fica expirado, e
    # toca-lo reabriria a transacao que a linha seguinte acabou de fechar.
    lead_id = conversation.lead_id
    # R-B: baseline do compare-and-swap, lido AQUI e nao la dentro de
    # `_chamar_crm` pelo mesmo motivo da linha acima — depois do commit
    # abaixo, esta query abriria uma transacao NOVA que ficaria aberta durante
    # todo o round-trip HTTP, exatamente o que aquele commit existe para
    # evitar. E o ponto mais fresco possivel: nenhuma chamada de rede acontece
    # entre esta leitura e a ida ao CRM. Ver `_ler_baseline_cas`.
    baseline_cas = _ler_baseline_cas(db, lead_id)
    # NAO segurar conexao do pool durante um round-trip HTTP. Nada esta pendente
    # aqui (o commit 1 fechou a transacao anterior; a retomada de uma operacao
    # `REQUESTED` nao escreveu nada), entao isto so encerra a transacao de
    # LEITURA aberta pelo SELECT da conversa. A V1 ja pagou este preco uma vez:
    # `webhook.py` fecha a sessao antes do await de 240s ao n8n
    # (AUDIT-2026-08-WF2 D1) porque segurar a conexao esgotava o pool sob rajada
    # de inbound.
    db.commit()

    resposta_crm = _chamar_crm(
        event_id=event_id_final,
        lead_id=lead_id,
        target_user_id=target_user_id,
        baseline_cas=baseline_cas,
    )
    if resposta_crm.error_code is not None:
        # Token E classe sao persistidos (o par que a particao de fila do §4.2
        # le); os tres campos de dono ficam so na memoria desta chamada — ver o
        # docstring de `RespostaCrm`.
        return _registrar_falha(
            db, operacao, conversation=conversation, reason=reason,
            error_code=resposta_crm.error_code.value,
            error_class=(
                resposta_crm.error_class.value if resposta_crm.error_class is not None else None
            ),
        )

    try:
        estado_antes = _aplicar_estado_local(
            db, conversation, target_user_id=target_user_id, reason=reason
        )
    except (TransicaoInvalida, IntegrityError):
        # `IntegrityError` (achado #4 da revisao da Fase 6): o comentario no
        # `db.flush()` de `_aplicar_estado_local` afirma que o CHECK
        # `ck_conversations_estado_humano` "falha aqui, no lugar certo" — mas
        # ate esta correcao o except so cobria `TransicaoInvalida`, entao uma
        # violacao REAL do CHECK escaparia crua (e, no PostgreSQL, deixaria a
        # sessao em `InFailedSqlTransaction` sem o `db.rollback()` abaixo).
        # Hoje e inalcancavel na pratica (a validacao Python de
        # `_validar_campos_operacionais` ja cobre o que o CHECK cobre) — mas o
        # comentario promete uma garantia que so este except entrega de
        # verdade. Mesmo `error_code` de `TransicaoInvalida`: os dois
        # significam "a escrita local foi recusada", so a CAMADA que recusou
        # muda (Python vs. banco).
        #
        # Desfaz as escritas de campo desta tentativa. O commit 1 ja fechou a
        # transacao anterior, entao o rollback nao alcanca a linha `REQUESTED`.
        db.rollback()
        # `error_class=None` de proposito — ver o docstring de `_registrar_falha`:
        # esta falha nao veio do CRM, entao nenhum membro de `ClasseErroCrm` a
        # descreve. A linha e da Direcao B de R15, eleita por `error_code`.
        return _registrar_falha(
            db, operacao, conversation=conversation, reason=reason,
            error_code=_ERRO_ESTADO_LOCAL, error_class=None,
        )

    # F2 (revisao da Fase 6): reserva o desfecho ANTES de registrar qualquer
    # evento desta tentativa — ordem obrigatoria (ver "ORDEM: ANTES DE
    # registrar_evento" no docstring de `_reservar_desfecho_ou_ceder_para_
    # vencedor`). Se outra chamada concorrente ja completou esta operacao
    # entre o `_aplicar_estado_local` acima e agora, cede o resultado REAL
    # (replay) em vez de aplicar HUMAN_SELECTED/HANDOFF_COMPLETED de novo.
    cedido = _reservar_desfecho_ou_ceder_para_vencedor(
        db, operacao, status_final=StatusHandoff.COMPLETED.value,
        error_code=None, error_class=None,
    )
    if cedido is not None:
        return cedido

    # Commit 3 — a unidade atomica do plano: estado local + os dois eventos +
    # `COMPLETED` vivem ou morrem juntos.
    registrar_evento(
        db,
        tipo=TipoEvento.HUMAN_SELECTED,
        commit=False,
        conversation_id=conversation.id,
        lead_id=conversation.lead_id,
        target_user_id=target_user_id,
        action=reason,
        result=ResultadoEvento.SUCESSO,
    )
    registrar_evento(
        db,
        tipo=TipoEvento.HANDOFF_COMPLETED,
        commit=False,
        conversation_id=conversation.id,
        lead_id=conversation.lead_id,
        target_user_id=target_user_id,
        action=reason,
        # `state_before`/`state_after` sao seguros aqui (ao contrario de
        # `gate.py`, que os omite): `aplicar_transicao` ja provou que os dois
        # sao membros do vocabulario de `Estado`, entao nao ha valor corrompido
        # capaz de quebrar a propria gravacao do evento.
        state_before=estado_antes,
        state_after=Estado.WAITING_HUMAN.value,
        result=ResultadoEvento.SUCESSO,
    )
    db.commit()

    return _resultado(operacao, sucesso=True, replayed=False)
