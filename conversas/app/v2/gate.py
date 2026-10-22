"""
BIA-V2 Fase 7 - Authorization gate (`autorizar_envio`).

Fonte normativa: docs/superpowers/plans/2026-08-29-bia-v2.md, secao "Fase 7 -
Authorization gate".

PROBLEMA QUE ISTO RESOLVE
--------------------------
A IA leva segundos (por vezes minutos) gerando uma resposta. Entre o
instante em que ela LEU o estado da conversa e o instante em que TERMINA,
qualquer coisa pode ter mudado por outro caminho: um humano assumiu, o bot
foi desligado, a conversa transicionou de estado, ou a janela de 24h da
Meta fechou. `autorizar_envio` e a ULTIMA barreira entre "a IA terminou de
gerar" e "a mensagem sai para o WhatsApp" - decide se a resposta ainda pode
ser enviada, relendo o estado ATUAL da conversa nesse instante.

R-INTENT (ver Fase 1 e Fase 7 no plano): este modulo nao recebe nem le
`AIInterpretationResult` - as seis checagens abaixo sao inteiramente sobre
COLUNAS operacionais da conversa (state, state_version, is_bot_active,
atendente_id, status, last_customer_msg_at), nunca sobre texto livre de intent.

"RELE SOB LOCK" SEM FOR UPDATE
-------------------------------
Este gate precisa funcionar em SQLite, que nao tem `SELECT ... FOR UPDATE`
de verdade (seria no-op de qualquer forma - o arquivo inteiro ja e
serializado por lock de arquivo). A garantia NAO vem de lock de linha; vem
de comparar o `state_version` FRESCO - lido agora, depois de descartar
qualquer cache da sessao - contra o `version_esperada` que o CALLER tinha
quando comecou a gerar a resposta. Se outra sessao deu commit na linha
nesse meio-tempo, a comparacao diverge e o gate recusa - mesmo efeito
pratico de um lock, sem depender de um.

Por isso a query usa `db.get(Conversation, conversation_id,
populate_existing=True)`: uma linha ja presente na identity map da sessao
NAO e reescrita por uma query nova a menos que esteja expirada ou a query
peca `populate_existing()` explicitamente - sem isso, `Session.get()` pode
nem emitir SQL se achar o objeto em cache, e o gate estaria "relendo" um
valor que nunca saiu da memoria. `populate_existing=True` forca a emissao
do SELECT e repopula os atributos desse objeto com a linha persistida
agora - restrito a ESTE objeto, sem expirar o resto da identity map da
sessao do caller (uma versao anterior usava `db.expire_all()`, que tinha o
mesmo efeito de releitura mas invalidava de lado qualquer outro objeto ja
carregado na mesma sessao - lead, mensagens de contexto - sem relacao com
este gate). Isso e o que faz a releitura valer entre SESSOES diferentes -
o caso real (a IA processa numa sessao/request; um handoff concorrente
commita noutra) - nao so dentro da mesma sessao, onde `expire_on_commit`
(default do SQLAlchemy) ja mascara a diferenca. Ver `tests/test_v2_gate.py`,
teste do briefing, que prova isto com duas sessoes de verdade.

AS SEIS CONDICOES DE RECUSA, CHECADAS EM ORDEM
------------------------------------------------
A primeira que falhar decide o `error_code` do evento; nunca uma setima:

  1. `state_version` atual != `version_esperada`.
  2. `state` atual != `state_esperado` - independente de 1: `state_version`
     pode incrementar sem trocar `state`, dependendo de qual escrita
     incrementou (por isso as duas sao checadas separadamente, nunca uma
     assumida como prova da outra).
  3. `is_bot_active is False` - bot nao pode mais falar.
  4. `atendente_id is not None` - um humano assumiu (ver nota abaixo).
  5. `status != 'aberta'` - a conversa foi encerrada (ver "O FURO DO STATUS").
  6. `not service_window_open(last_customer_msg_at)` - janela Meta fechada.
     REUSA `service_window_open` (FONTE UNICA da regra de 24h, ja definida
     em `conversas/app/models/conversation.py`); nunca reimplementada aqui.

O FURO DO STATUS (R8/R10, inventario secao 0.5) - POR QUE A 5a EXISTE
-----------------------------------------------------------------------
Um `PUT /api/conversations/{id}` com `{"status":"encerrada"}` ISOLADO (sem
claim antes) encerra a conversa e NAO desliga o bot: `update_conversation` so
escreve `conversation.status` nesse ramo. Numa conversa `AI_TRIAGE` com o bot
ligado, as condicoes 1-4 e 6 passavam todas, e o gate AUTORIZAVA a IA a
responder numa conversa que um humano acabava de encerrar. Nenhuma das outras
cinco releituras enxerga esse caso, porque nenhuma delas le `status`.

A checagem entra ANTES da janela de 24h de proposito: "o humano encerrou" e um
fato do NOSSO sistema e diz mais a quem investiga do que "a janela da Meta
expirou", que e a consequencia mais provavel de a conversa estar parada. Entra
DEPOIS de 3 e 4 para nao mudar o `error_code` de nenhum caso que ja era
recusado por bot desligado ou humano dono - o vocabulario cresce, a prioridade
existente nao se mexe.

'aberta' e o unico status aberto que se escreve: 'aguardando' e LEGADO e
DERIVADO (a whitelist do `PUT /{id}` o recusa explicitamente) e sobrevive so em
`LEGACY_OPEN_STATUSES`, por tolerancia a linhas antigas. Uma conversa em
'aguardando' e uma linha legada que a IA nao deve estar processando - recusar e
o comportamento fail-closed correto, coerente com a decisao 10 da maquina.

"UM HUMANO ASSUMIU" = `atendente_id IS NOT NULL`, e nao
`primeira_resposta_humana_at IS NOT NULL`. Escolha deliberada: `atendente_id`
e escrito no INSTANTE do handoff/claim/assign, por `aplicar_estado_humano`
(`app/services/atendimento.py`), que TAMBEM desliga `is_bot_active` nesse
mesmo instante - antes de qualquer humano ter de fato respondido.
`primeira_resposta_humana_at` so e gravado bem depois, na PRIMEIRA resposta
humana (`marcar_atendimento_humano`, no mesmo modulo). A IA precisa parar de
falar assim que alguem assume a fila, nao so quando essa pessoa manda a
primeira mensagem - `atendente_id` e o sinal mais cedo, e e o mesmo campo
que `app/v2/estados.py` (`_validar_campos_operacionais`) e a Familia B de
`tests/test_v2_v1_intacta.py` (B.3) ja tratam como o dono operacional da
conversa.

Toda recusa emite EXATAMENTE UM `AI_RESPONSE_DISCARDED` (a ordem acima
decide qual `error_code` e reportado, mesmo quando mais de uma condicao e
verdadeira ao mesmo tempo). O evento carrega `conversation_id` +
`error_code` (vocabulario FECHADO de seis tokens) + `result=IGNORADO` -
nunca `state_before`/`state_after`: este gate nao executa transicao
nenhuma (so recusa ou libera o envio), e esses dois campos tem formato
restrito (`eventos_validacao.validar_state`) que uma linha corrompida
poderia violar, quebrando a propria emissao do evento de diagnostico.
Nenhum dado de cliente (telefone, nome, conteudo de mensagem) entra em
`error_code` nem em qualquer outro campo do evento - mesmo cuidado que
`estados.py` ja aplica a `TransicaoInvalida`.

Sucesso (nenhuma das seis condicoes) NAO emite evento.

`conversation_id` QUE NAO RESOLVE A NENHUMA LINHA NAO E UMA DAS SEIS
CONDICOES DE RECUSA - e erro de chamador (a IA nao deveria estar
processando uma conversa que nao existe mais), entao levanta `ValueError`
em vez de devolver `False` em silencio ou de emitir um evento de
diagnostico sobre uma conversa inexistente.
"""
from sqlalchemy.orm import Session

from app.models.conversation import Conversation, service_window_open
from app.v2.eventos import registrar_evento
from app.v2.eventos_validacao import ResultadoEvento, TipoEvento

# Vocabulario FECHADO dos motivos de recusa - mesmo espirito dos tokens de
# `TransicaoInvalida` em estados.py, mas em MAIUSCULO: `error_code` de
# `eventos_validacao.py` exige o formato `^[A-Z][A-Z0-9_]*$`.
_ERRO_STATE_VERSION_DIVERGENTE = "STATE_VERSION_DIVERGENTE"
_ERRO_STATE_DIVERGENTE = "STATE_DIVERGENTE"
_ERRO_BOT_INATIVO = "BOT_INATIVO"
_ERRO_HUMANO_ASSUMIU = "HUMANO_ASSUMIU"
_ERRO_CONVERSA_ENCERRADA = "CONVERSA_ENCERRADA"
_ERRO_JANELA_FECHADA = "JANELA_FECHADA"

# O unico status em que a IA pode falar. Ver "O FURO DO STATUS" no docstring.
_STATUS_ABERTA = "aberta"


def _recusar(db: Session, conversation_id: int, error_code: str) -> bool:
    """
    Emite `AI_RESPONSE_DISCARDED` com o motivo e devolve `False`. Unico
    ponto de saida negativa de `autorizar_envio` - as seis checagens nunca
    duplicam a chamada de `registrar_evento`.
    """
    registrar_evento(
        db,
        tipo=TipoEvento.AI_RESPONSE_DISCARDED,
        commit=True,
        conversation_id=conversation_id,
        result=ResultadoEvento.IGNORADO,
        error_code=error_code,
    )
    return False


def motivo_recusa(
    conversation: Conversation,
    *,
    state_esperado: str,
    version_esperada: int,
) -> str | None:
    """
    As SEIS condicoes de recusa, na ordem normativa, sobre uma linha JA lida.
    Devolve o `error_code` da PRIMEIRA que for verdadeira, ou `None` se
    nenhuma for (= envio autorizado).

    PURA: nao le banco, nao escreve, nao emite evento, nao faz IO. Toda a
    decisao do gate mora aqui; `autorizar_envio` acrescenta exatamente duas
    coisas — a RELEITURA sob `populate_existing=True` e a EMISSAO do
    `AI_RESPONSE_DISCARDED` — e nada mais.

    Extraida (sem mudanca de ordem, de token ou de comparacao) para que o
    endpoint SOMBRA (`app/v2/sombra.py`) possa avaliar o gate sem persistir o
    evento de recusa que `_recusar` grava. Sem esta separacao, a sombra teria
    de reimplementar as seis condicoes — duplicando uma regra de seguranca
    que so poderia divergir com o tempo.
    """
    if conversation.state_version != version_esperada:
        return _ERRO_STATE_VERSION_DIVERGENTE
    if conversation.state != state_esperado:
        return _ERRO_STATE_DIVERGENTE
    if conversation.is_bot_active is False:
        return _ERRO_BOT_INATIVO
    if conversation.atendente_id is not None:
        return _ERRO_HUMANO_ASSUMIU
    if conversation.status != _STATUS_ABERTA:
        return _ERRO_CONVERSA_ENCERRADA
    if not service_window_open(conversation.last_customer_msg_at):
        return _ERRO_JANELA_FECHADA
    return None


def autorizar_envio(
    db: Session,
    *,
    conversation_id: int,
    state_esperado: str,
    version_esperada: int,
) -> bool:
    """
    Ultima checagem antes de enviar a resposta que a IA acabou de gerar.

    Rele o estado ATUAL da conversa (descartando cache da sessao - ver
    "RELE SOB LOCK" no docstring do modulo) e recusa se qualquer uma das
    seis condicoes documentadas ali for verdadeira - devolve `False` e
    emite `AI_RESPONSE_DISCARDED`. Se nenhuma for verdadeira, devolve `True`
    sem emitir evento.

    `state_esperado`/`version_esperada` sao o `state`/`state_version` que o
    CALLER leu ANTES de comecar a gerar a resposta da IA. `state_esperado`
    aceita tanto a string crua quanto um membro de `Estado`
    (`app/v2/estados.py`) - `Estado` e `StrEnum`, entao a comparacao por
    igualdade funciona identica nos dois casos, sem precisar coagir tipo.

    Levanta `ValueError` se `conversation_id` nao corresponder a nenhuma
    linha - nao e uma das seis condicoes de recusa, e erro de chamador.
    """
    conversation = db.get(Conversation, conversation_id, populate_existing=True)
    if conversation is None:
        raise ValueError(f"autorizar_envio: conversation_id {conversation_id} nao encontrada")

    motivo = motivo_recusa(
        conversation, state_esperado=state_esperado, version_esperada=version_esperada
    )
    if motivo is not None:
        return _recusar(db, conversation_id, motivo)

    return True
