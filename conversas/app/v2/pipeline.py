"""
BIA-V2 Fase 10 — pipeline.py (`conversas/app/v2/pipeline.py`).

Orquestra os módulos das Fases 1-9 num único fluxo, disparado por
`conversas/app/routers/v2_agent.py`. Testável sem HTTP: `executar_pipeline`
recebe `db: Session` e um `InboundContextV2` já validado, e é uma função
comum — sem FastAPI, sem Request/Response.

ORDEM DE ETAPAS DECIDIDA NESTA FASE, E POR QUÊ
================================================
O plano (Fase 10) deixa a ordem exata para esta fase decidir, "escrito
quando a fase é alcançada". Ordem adotada, com a justificativa de cada
posição:

  1. Carregar a `Conversation` (`conversation_id`). Não encontrada ->
     `ValueError` (mesmo idioma de `gate.py`/`handoff.py` para erro de
     chamador). É a ÚNICA coisa que precisa existir antes de qualquer escrita.

  2. `MESSAGE_RECEIVED` (commit=True), usando `entrada.event_id` como
     idempotency key. Vem ANTES da checagem de estado (passo 3) de propósito:
     mesmo uma tentativa que será REJEITADA por mirar uma conversa fora do
     vocabulário V2 (LEGACY/NEW) fica registrada — é exatamente o tipo de
     evidência que ajudaria a diagnosticar uma violação do R9 ("conversa real
     promovida a V2 acidentalmente"). Reenviar a MESMA chamada com o MESMO
     `event_id` levanta `EventoDuplicado` aqui, mapeado para 409 — é o único
     ponto desta função com uma idempotency key vinda do chamador.

  3. Validação de `state`. `NEW`/`LEGACY`/valor desconhecido ->
     `ConversationInvalidaParaV2` (definida abaixo). Nem `gate.py` nem
     `handoff.py` recusam uma linha LEGACY antes de produzir efeito — o gate
     só compara divergência contra o que o PRÓPRIO chamador leu (uma linha
     LEGACY nunca "diverge" de si mesma) e `aplicar_transicao` só recusaria
     DEPOIS de já termos gasto uma seleção de atendente. Fase 10 integra
     vários módulos que, cada um isoladamente, confia que quem os chama já
     filtrou isso — então é aqui, no orquestrador, que o filtro mora.
     `CLOSED` é tratado à parte (ver abaixo) porque é um estado V2 legítimo,
     só que sem reabertura implementada nesta fase.

  4. `CLOSED` -> pára com `CONVERSA_FECHADA_REABERTURA_NAO_IMPLEMENTADA`,
     sem chamar `decidir_reabertura`/`dentro_janela_reabertura` (Fase 4).
     GAP CONHECIDO, documentado aqui de propósito: "quem seria o dono
     anterior elegível" (`dono_anterior_elegivel` de `decidir_reabertura`)
     não tem função existente que resolva isso a partir de um `user_id` —
     integrar essa peça exigiria inventar uma regra de negócio nova, o que
     esta fase não deve fazer ("Fase 10 integra, não redesenha"). Fica
     registrado no relatório desta fase como algo para um subgate próprio.

  5. `is_bot_active is False` -> pára com `BOT_INATIVO`, ANTES do
     pré-filtro. Cobre `WAITING_HUMAN`/`HUMAN_ACTIVE` de graça: as invariantes
     de `_validar_campos_operacionais` (estados.py) já exigem
     `is_bot_active is False` para os dois. Evita gastar pré-filtro/IA quando
     um humano já é dono da conversa.

  6. Pré-filtro (`avaliar_prefiltro`, Fase 3) sobre `entrada.lote`. Ignorar ->
     `MESSAGE_IGNORED` (com `action=decisao.motivo`) e pára. Continuar -> segue.

  7. IA Interpretadora (`interpretar`, Fase 8) sobre
     `entrada.mensagem_interpretacao`. `InterpretacaoFalhou` é convertida em
     `ResultadoPipeline` (nunca deixada escapar como exceção não mapeada) —
     mesmo espírito de `HandoffResult.sucesso=False`: é desfecho de negócio
     esperado, já registrado em `AI_INTERPRETATION_FAILED` pelo próprio
     `interpretar()`, não um bug.

  8. Triagem (`avaliar_triagem`, Fase 2) sobre `resultado_ia.extracted`.
     Emite `TRIAGE_COMPLETED` ou `TRIAGE_DATA_UPDATED` (nunca os campos de
     `campos_faltantes` no evento — ver R14 do plano: essa allowlist de
     payload é fechada de propósito, e reabri-la não é decisão desta fase).

  9. Decisão de encaminhamento — SOMENTE sobre fatos validados, NUNCA sobre
     `AIInterpretationResult.intent` (R-INTENT, absoluto, vale nesta fase):
     `resultado_ia.explicit_human_request` (bool) OU
     `resultado_triagem.completa` (bool). Nenhum dos dois é texto livre.

     9a. Sem gatilho -> segue para o passo 11 (gate) com
         `acao="solicitar_campos_faltantes"` ou `"informar_fatos_confirmados"`
         conforme `resultado_triagem.campos_faltantes` (ver `_acao_comunicador`).

     9b. Com gatilho -> `selecionar_atendente` (Fase 5).
         - `None` -> `HANDOFF_FAILED` com `error_code="SEM_ATENDENTE_ELEGIVEL"`
           (token literal do R5 do plano) e segue para o passo 11. Esta escrita
           NÃO depende do modo (dry_run/live): é leitura pura + fato real, sem
           efeito em `conversations`.
         - achou atendente e `V2_MODE == "live"` -> `handoff_to_human` (Fase 6)
           de verdade. Sucesso -> a função PÁRA aqui, SEM chamar o gate/
           comunicador: o bot já foi desligado por `aplicar_estado_humano`
           dentro do handoff, e mandar a IA falar depois disso é exatamente o
           que o gate recusaria (`BOT_INATIVO`) — não vale a chamada. Falha ->
           segue para o passo 11 com `acao="informar_fatos_confirmados"` (o
           handoff já registrou `HANDOFF_FAILED` com o motivo real).
         - achou atendente e modo é dry_run (o ÚNICO exercitado nesta fase,
           ver bloco DRY_RUN abaixo) -> a função PÁRA aqui com
           `HANDOFF_SIMULADO_DRY_RUN`, e `handoff_to_human` NUNCA é chamado.

  10. (dobra do passo 9b) — nada a fazer; listado para não quebrar a
      numeração do texto acima.

  11. Gate (`autorizar_envio`, Fase 7), relendo o `state`/`state_version`
      capturados no passo 1 (ANTES de qualquer chamada de IA). Recusa ->
      `AI_RESPONSE_DISCARDED` (já emitido pelo próprio `gate.py`) e pára.

  12. IA Comunicadora + output guard (`redigir_resposta`, Fase 9) —
      `filtrar_saida` já roda DENTRO de `redigir_resposta`; esta função NUNCA
      chama `output_guard.filtrar_saida` separadamente (chamaria duas vezes
      sobre o mesmo texto, ou pior, esqueceria de chamar no caminho errado).
      `permitido=True` -> `AI_RESPONSE_GENERATED` e devolve o texto.
      `permitido=False` -> `AI_RESPONSE_DISCARDED`, com
      `error_code=motivo.upper()` (os dois motivos de `output_guard.py`,
      `acao_nao_autorizada`/`valor_monetario_detectado`, já batem o formato
      `^[A-Z][A-Z0-9_]*$` depois de maiusculizados). `ComunicadorIndisponivel`
      (Gemini indisponível - chave ausente ou chamada falhou) é convertida em
      `ResultadoPipeline` com `EtapaFinal.COMUNICADOR_INDISPONIVEL` — mesmo
      espírito do passo 7 (`InterpretacaoFalhou`): falha de infraestrutura é
      desfecho de negócio esperado, nunca exceção não mapeada escapando para
      um 500 genérico (achado da mega review pós-Fase 10).

  Nenhum envio real (WhatsApp) acontece em NENHUM ramo — não implementado
  nesta fase, fora do escopo do contrato da Fase 10 (endpoint + pipeline,
  não "V2 100% ao vivo"). O texto devolvido em `RESPOSTA_GERADA` é para o
  CHAMADOR decidir o que fazer com ele.

DRY_RUN × LIVE — O QUE `V2_MODE` REALMENTE GOVERNA NESTA FASE
==============================================================
Interpretação, triagem, pré-filtro, gate e comunicador+guard NUNCA escrevem
em `conversations` e não têm efeito externo irreversível (a chamada real ao
Gemini é I/O, mas não é o que o contrato "nenhuma linha de conversations é
escrita" protege, e os testes desta fase substituem essa fronteira, como já é
convenção — ver docstrings de `interpretador.py`/`comunicador.py`). Rodam
IGUAL em dry_run e live.

`handoff_to_human` é a ÚNICA função de todo este pipeline com efeito real
irreversível: grava em `conversations` (via `aplicar_estado_humano` +
`aplicar_transicao`) e em `handoff_operations`, e faz uma chamada HTTP real ao
CRM. É por isso — e SÓ por isso — que `V2_MODE` existe: gatear essa ÚNICA
chamada. `V2_MODE` é lido de `app.config` (Fase 10 criou a variável lá,
default `"dry_run"`); fail-closed significa que QUALQUER valor diferente do
literal `"live"` se comporta como dry_run — nunca o inverso.

Quando o modo é dry_run e um handoff seria acionado, NENHUM evento novo é
gravado para essa decisão (nem um `HANDOFF_REQUESTED` "hipotético"): não há
`TipoEvento` fechado para "handoff decidido mas suprimido por dry_run", e
criar um exigiria editar `eventos_validacao.py` — fora do escopo autorizado
desta fase. Gravar `HANDOFF_REQUESTED` sem a operação real por trás mentiria
na trilha de auditoria (pareceria que a etapa 1 do commit atômico de
`handoff_to_human` realmente rodou). A decisão fica SÓ na resposta HTTP
(`ResultadoPipeline.etapa_final=HANDOFF_SIMULADO_DRY_RUN` +
`.atendente_id`), nunca persistida.

`InboundContextV2` — POR QUE MORA AQUI, NÃO EM `contratos.py`
================================================================
A Fase 1 diferiu `InboundContextV2` explicitamente para esta fase ("nenhum
schema de payload existe até o endpoint existir") — `tests/test_v2_contratos.py`
(check 23) trava a AUSÊNCIA deste nome em `contratos.py`. Mesmo precedente de
`MensagemLote` (`prefiltro.py`, Fase 3, decisão P-2) e `EntradaComunicador`
(`comunicador.py`, Fase 9): contrato LOCAL ao módulo que o consome, para não
acoplar `contratos.py` a um único caller. Fica em `pipeline.py`, não em
`v2_agent.py`, porque é o tipo do PARÂMETRO de `executar_pipeline` — a mesma
razão pela qual o requisito desta fase pede pipeline testável "recebendo
`db: Session` e o payload já validado": o router só faz o parse HTTP->Pydantic
e repassa.

`event_id` NÃO tem NENHUM validator neste schema, de propósito — ver o bloco
"Normalização de event_id" do prompt desta tarefa e a nota correspondente na
Fase 10 do plano: um valor informado-mas-vazio ("", "   ", 0, False) precisa
virar `CampoInvalido`, NUNCA `None`. Um `field_validator`/`BeforeValidator`
fazendo `v or None` aqui rodaria ANTES de `eventos_validacao.validar_event_id`
e reabriria exatamente o bug que aquela função existe para fechar. O campo
atravessa este schema exatamente como chegou; `registrar_evento` (via
`validar_event_id`) é a ÚNICA fonte de verdade da normalização.

OUTRAS DECISÕES DESTA FASE, NÃO PRESCRITAS PELO PLANO
=======================================================
  - `acao` do comunicador é derivada SÓ de `resultado_triagem.campos_faltantes`
    (vazio -> "informar_fatos_confirmados", não-vazio ->
    "solicitar_campos_faltantes") — nunca de POR QUE chegamos até aqui (pedido
    explícito sem atendante, handoff que falhou, ou triagem simplesmente
    incompleta). `output_guard.ACOES_PERMITIDAS` só tem esses dois membros
    (vocabulário PROVISÓRIO, ver docstring de `output_guard.py`); inventar um
    terceiro (ex.: "aguardar_atendente") exigiria o subgate de contrato que a
    Fase 1 deixou pendente para `AuthorizedResponse` — não é desta fase.
  - `handoff_to_human` recebe `event_id=evento_recebido.event_id` (o MESMO
    id, já canonicalizado, do `MESSAGE_RECEIVED` desta chamada) — não um id
    derivado. Consequência aceita, documentada: como `MESSAGE_RECEIVED` já
    recusa (`EventoDuplicado`/409) uma repetição do MESMO `event_id` antes de
    chegar perto do handoff, a RETOMADA de uma operação `REQUESTED`/`FAILED`
    presa (crash a meio caminho) não é alcançável reenviando o MESMO payload
    a este endpoint — precisaria de uma chamada direta/administrativa a
    `handoff_to_human` com o `event_id` conhecido, fora do escopo desta fase.
    A alternativa (id do `MESSAGE_RECEIVED` dissociado do id do handoff) foi
    considerada e descartada por adicionar uma segunda noção de identidade
    sem um requisito de teste que a justifique agora.
  - Nenhuma escrita em `conversations` acontece por conta desta função
    diretamente: cada mutação real (só existe uma, dentro de
    `handoff_to_human`, só em modo live) é feita e comitada por um módulo já
    testado nas Fases 1-9. `executar_pipeline` nunca chama `db.commit()`.
"""
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from sqlalchemy.orm import Session

from app.config import V2_MODE
from app.models.conversation import Conversation
from app.v2.comunicador import ComunicadorIndisponivel, EntradaComunicador, redigir_resposta
from app.v2.estados import Estado
from app.v2.eventos import registrar_evento
from app.v2.eventos_validacao import OrigemEvento, ResultadoEvento, TipoEvento
from app.v2.gate import autorizar_envio
from app.v2.handoff import HandoffInconsistente, handoff_to_human
from app.v2.interpretador import InterpretacaoFalhou, interpretar
from app.v2.prefiltro import MensagemLote, avaliar_prefiltro
from app.v2.selecao import selecionar_atendente
from app.v2.triagem import avaliar_triagem

_V2_MODE_LIVE = "live"


def _modo_dry_run() -> bool:
    """Fail-closed: só o literal exato "live" habilita efeito real (ver
    bloco DRY_RUN x LIVE no docstring do módulo)."""
    return V2_MODE != _V2_MODE_LIVE


class ConversationInvalidaParaV2(ValueError):
    """
    A conversa existe, mas seu `state` não está no vocabulário que este
    pipeline processa (`NEW`, `LEGACY` ou qualquer valor fora de `Estado`).

    Decisão desta fase — ver o passo 3 do docstring do módulo para o porquê
    de nenhum módulo das Fases 1-9 fechar essa fronteira sozinho. `motivo`
    nunca ecoa `state` cru na mensagem exposta por `str(exc)`; o valor fica
    disponível estruturado em `.state`, mesmo padrão de `EventoDuplicado`
    (`eventos.py`) e `HandoffInconsistente` (`handoff.py`).
    """

    def __init__(self, conversation_id: int, state: str | None):
        self.conversation_id = conversation_id
        self.state = state
        super().__init__(
            f"conversation_id {conversation_id}: state fora do vocabulario "
            "processavel pela V2 (NEW/LEGACY/desconhecido)"
        )


class EtapaFinal(StrEnum):
    """Vocabulário fechado do desfecho de `executar_pipeline` — ver
    `ResultadoPipeline`. Local a esta fase; não é `TipoEvento` (que continua
    fechado em `eventos_validacao.py`, intocado por esta fase)."""

    CONVERSA_FECHADA_REABERTURA_NAO_IMPLEMENTADA = "conversa_fechada_reabertura_nao_implementada"
    BOT_INATIVO = "bot_inativo"
    MENSAGEM_IGNORADA = "mensagem_ignorada"
    INTERPRETACAO_FALHOU = "interpretacao_falhou"
    HANDOFF_SIMULADO_DRY_RUN = "handoff_simulado_dry_run"
    HANDOFF_CONCLUIDO = "handoff_concluido"
    HANDOFF_INCONSISTENTE = "handoff_inconsistente"
    RESPOSTA_DESCARTADA_GATE = "resposta_descartada_gate"
    COMUNICADOR_INDISPONIVEL = "comunicador_indisponivel"
    RESPOSTA_BLOQUEADA_GUARD = "resposta_bloqueada_guard"
    RESPOSTA_GERADA = "resposta_gerada"


@dataclass(frozen=True)
class ResultadoPipeline:
    """Retorno de `executar_pipeline` — o que `v2_agent.py` serializa como
    corpo HTTP 200. Nenhum campo carrega dado bruto do cliente exceto
    `texto_resposta` (a própria resposta redigida para o cliente, que o
    chamador já teria de qualquer forma para efetivamente enviá-la)."""

    etapa_final: EtapaFinal
    conversation_id: int
    texto_resposta: str | None = None
    atendente_id: int | None = None
    detalhe: str | None = None


class InboundContextV2(BaseModel):
    """Payload de entrada de `POST /webhook/agent-bia-v2`. Ver o bloco
    "InboundContextV2 — por que mora aqui" no docstring do módulo."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: StrictInt = Field(ge=1)
    origem: OrigemEvento
    lote: list[MensagemLote]
    mensagem_interpretacao: StrictStr
    event_id: StrictStr | None = None


# Estados em que este pipeline aceita operar. NEW/LEGACY (e qualquer valor
# fora do vocabulario de Estado) ficam de fora -> ConversationInvalidaParaV2.
# CLOSED fica DENTRO (não levanta a exceção) mas é tratado à parte, antes de
# qualquer outra etapa — ver passo 4 do docstring do módulo.
_ESTADOS_PROCESSAVEIS = frozenset({
    Estado.AI_TRIAGE.value,
    Estado.WAITING_HUMAN.value,
    Estado.HUMAN_ACTIVE.value,
    Estado.CLOSED.value,
})


def _acao_comunicador(campos_faltantes: list[str]) -> str:
    """Único ponto de decisão do `acao` passado à IA Comunicadora — ver
    bloco "Outras decisões desta fase" no docstring do módulo."""
    return "solicitar_campos_faltantes" if campos_faltantes else "informar_fatos_confirmados"


def executar_pipeline(db: Session, entrada: InboundContextV2) -> ResultadoPipeline:
    """Percorre o pipeline inteiro para UM lote de mensagens pendentes de UMA
    conversa. Ver o docstring do módulo para a ordem completa e o porquê de
    cada posição. Nunca chama `db.commit()` diretamente — cada escrita real é
    feita e comitada por um módulo das Fases 1-9 já testado isoladamente."""
    conversation = db.get(Conversation, entrada.conversation_id)
    if conversation is None:
        raise ValueError(
            f"executar_pipeline: conversation_id {entrada.conversation_id} nao encontrada"
        )

    evento_recebido = registrar_evento(
        db,
        tipo=TipoEvento.MESSAGE_RECEIVED,
        commit=True,
        event_id=entrada.event_id,
        conversation_id=conversation.id,
        lead_id=conversation.lead_id,
        payload={"origem": entrada.origem.value},
    )

    if conversation.state not in _ESTADOS_PROCESSAVEIS:
        raise ConversationInvalidaParaV2(conversation.id, conversation.state)

    # Baseline lido ANTES de qualquer chamada de IA — é o que o gate (passo
    # 11) vai reconferir depois. Valores simples (int/str), não uma
    # referência viva ao objeto ORM: permanecem corretos mesmo que `conversation`
    # seja expirado por um commit posterior desta própria função.
    state_esperado = conversation.state
    version_esperada = conversation.state_version

    if conversation.state == Estado.CLOSED.value:
        return ResultadoPipeline(
            EtapaFinal.CONVERSA_FECHADA_REABERTURA_NAO_IMPLEMENTADA, conversation.id
        )

    if not conversation.is_bot_active:
        return ResultadoPipeline(EtapaFinal.BOT_INATIVO, conversation.id)

    decisao_prefiltro = avaliar_prefiltro(entrada.lote)
    if decisao_prefiltro.ignorar:
        registrar_evento(
            db,
            tipo=TipoEvento.MESSAGE_IGNORED,
            commit=True,
            conversation_id=conversation.id,
            lead_id=conversation.lead_id,
            action=decisao_prefiltro.motivo,
            result=ResultadoEvento.IGNORADO,
        )
        return ResultadoPipeline(EtapaFinal.MENSAGEM_IGNORADA, conversation.id)

    try:
        resultado_ia = interpretar(
            db,
            mensagem=entrada.mensagem_interpretacao,
            commit=True,
            conversation_id=conversation.id,
            lead_id=conversation.lead_id,
            model_attempt=0,
        )
    except InterpretacaoFalhou as exc:
        return ResultadoPipeline(
            EtapaFinal.INTERPRETACAO_FALHOU, conversation.id, detalhe=exc.error_code
        )

    resultado_triagem = avaliar_triagem(resultado_ia.extracted)
    registrar_evento(
        db,
        tipo=TipoEvento.TRIAGE_COMPLETED if resultado_triagem.completa else TipoEvento.TRIAGE_DATA_UPDATED,
        commit=True,
        conversation_id=conversation.id,
        lead_id=conversation.lead_id,
        result=ResultadoEvento.SUCESSO,
    )

    # R-INTENT: a decisão abaixo usa SOMENTE fatos validados por schema
    # (bool) — `explicit_human_request` e `.completa` — NUNCA o texto livre
    # de `resultado_ia.intent`. Vale nesta fase também (Fase 1 a 10).
    deve_encaminhar_humano = resultado_ia.explicit_human_request or resultado_triagem.completa

    if deve_encaminhar_humano:
        atendente_id = selecionar_atendente(db)
        if atendente_id is None:
            registrar_evento(
                db,
                tipo=TipoEvento.HANDOFF_FAILED,
                commit=True,
                conversation_id=conversation.id,
                lead_id=conversation.lead_id,
                result=ResultadoEvento.FALHA,
                error_code="SEM_ATENDENTE_ELEGIVEL",
            )
            # Conversa permanece no estado atual (R5) — segue para compor
            # resposta com o que ja se sabe, sem anunciar encaminhamento.
        elif _modo_dry_run():
            return ResultadoPipeline(
                EtapaFinal.HANDOFF_SIMULADO_DRY_RUN,
                conversation.id,
                atendente_id=atendente_id,
                detalhe=(
                    "dry_run: handoff_to_human nao foi chamado - nenhum efeito "
                    "real, nenhum evento novo (ver docstring de pipeline.py)."
                ),
            )
        else:
            reason = "pedido_explicito_humano" if resultado_ia.explicit_human_request else "triagem_completa"
            try:
                resultado_handoff = handoff_to_human(
                    db,
                    conversation_id=conversation.id,
                    target_user_id=atendente_id,
                    reason=reason,
                    event_id=evento_recebido.event_id,
                )
            except HandoffInconsistente as exc:
                return ResultadoPipeline(
                    EtapaFinal.HANDOFF_INCONSISTENTE, conversation.id, detalhe=str(exc)
                )
            if resultado_handoff.sucesso:
                # Bot ja foi desligado por aplicar_estado_humano (dentro do
                # handoff) - nao ha gate/comunicador a chamar depois disso.
                return ResultadoPipeline(
                    EtapaFinal.HANDOFF_CONCLUIDO, conversation.id, atendente_id=atendente_id
                )
            # Falha (ex.: CRM indisponivel) NAO retorna aqui - mesmo
            # fallthrough do ramo irmao `atendente_id is None` acima: segue
            # para compor uma resposta via comunicador em vez de deixar o
            # cliente sem NENHUMA resposta (o pior desfecho possivel num
            # sistema de atendimento). `handoff_to_human` ja registrou
            # HANDOFF_FAILED com o error_code real
            # (`resultado_handoff.error_code`) antes de devolver o controle
            # aqui - nao ha evento novo para emitir.

    autorizado = autorizar_envio(
        db,
        conversation_id=conversation.id,
        state_esperado=state_esperado,
        version_esperada=version_esperada,
    )
    if not autorizado:
        return ResultadoPipeline(EtapaFinal.RESPOSTA_DESCARTADA_GATE, conversation.id)

    try:
        resultado_guard = redigir_resposta(
            EntradaComunicador(
                acao=_acao_comunicador(resultado_triagem.campos_faltantes),
                fatos_confirmados=resultado_ia.extracted,
                campos_faltantes=resultado_triagem.campos_faltantes,
                duvidas=resultado_ia.duvidas,
            )
        )
    except ComunicadorIndisponivel as exc:
        return ResultadoPipeline(
            EtapaFinal.COMUNICADOR_INDISPONIVEL, conversation.id, detalhe=str(exc)
        )

    if not resultado_guard.permitido:
        registrar_evento(
            db,
            tipo=TipoEvento.AI_RESPONSE_DISCARDED,
            commit=True,
            conversation_id=conversation.id,
            lead_id=conversation.lead_id,
            result=ResultadoEvento.IGNORADO,
            error_code=resultado_guard.motivo.upper(),
        )
        return ResultadoPipeline(
            EtapaFinal.RESPOSTA_BLOQUEADA_GUARD, conversation.id, detalhe=resultado_guard.motivo
        )

    registrar_evento(
        db,
        tipo=TipoEvento.AI_RESPONSE_GENERATED,
        commit=True,
        conversation_id=conversation.id,
        lead_id=conversation.lead_id,
        result=ResultadoEvento.SUCESSO,
    )
    return ResultadoPipeline(
        EtapaFinal.RESPOSTA_GERADA, conversation.id, texto_resposta=resultado_guard.texto_final
    )
