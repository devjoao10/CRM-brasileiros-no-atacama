"""
BIA-V2 SOMBRA — `conversas/app/v2/sombra.py`.

Responde a UMA pergunta: "o que a V2 teria decidido para esta conversa REAL?".
Nada mais. Zero efeito operacional, garantido por CONSTRUCAO — nunca por
configuracao.

POR QUE ISTO EXISTE E NAO E O ENDPOINT DA FASE 10
==================================================
As conversas reais em producao estao em `state = 'LEGACY'`, e
`executar_pipeline` (`pipeline.py`, passo 3) as RECUSA de proposito com
`ConversationInvalidaParaV2` -> HTTP 409. Essa recusa esta certa e NAO muda:
promover uma conversa real para `AI_TRIAGE` so para observa-la seria
exatamente a violacao do R9 que aquele filtro existe para impedir.

A sombra e o caminho oposto: aceita QUALQUER `state` (inclusive `LEGACY`,
`NEW` e valor desconhecido) justamente porque nao escreve nada em lugar
nenhum. O workflow n8n "Agente Bia" (V1) continua 100% dono do trafego real;
a sombra so observa, em paralelo, para que se possa comparar V1 x V2 antes do
corte.

COMO O EFEITO ZERO E GARANTIDO — TRES CAMADAS ESTRUTURAIS
===========================================================
Nenhuma delas depende de `V2_MODE`, e este modulo NEM IMPORTA `V2_MODE` (o
teste de sombra checa a ausencia dessa string no fonte). `V2_MODE=dry_run`
sera configurado no deploy como defesa em profundidade, mas a sombra ja e
segura com `V2_MODE=live`.

  1. AUSENCIA DE IMPORT. Este modulo nao importa — logo nao pode chamar —
     `registrar_evento` (`eventos.py`), `handoff_to_human` (`handoff.py`),
     `aplicar_transicao`/`aplicar_estado_humano` (`estados.py`/V1),
     `autorizar_envio` (`gate.py`, que PERSISTE `AI_RESPONSE_DISCARDED` em
     toda recusa) nem qualquer cliente HTTP do CRM. O que nao esta no
     namespace nao e alcancavel por engano numa edicao futura.

  2. BARREIRA DE FLUSH NA PROPRIA SESSAO. `_sessao_somente_leitura` instala
     um `before_flush` NESTA instancia de `Session` que levanta
     `EscritaProibidaNaSombra` se houver QUALQUER objeto em
     `session.new/dirty/deleted`. Um `db.commit()` tambem passa por flush,
     entao a barreira cobre os dois. Se um dia alguem acrescentar aqui uma
     chamada que suja a sessao, o pedido morre RUIDOSAMENTE (500) em vez de
     gravar em silencio — que e o desfecho correto para um bug de invariante.

     ALCANCE EXATO DESTA CAMADA — E O PONTO CEGO QUE ELA TEM. `before_flush`
     so ve a UNIDADE DE TRABALHO do ORM: objeto adicionado, mutado ou apagado
     via `Session`. DML de nivel Core NAO passa por flush nenhum e portanto
     NAO e vista por esta barreira — `db.execute(update(...))`,
     `db.execute(text("UPDATE ..."))` e amigos escreveriam direto na conexao.
     Some-se a isso `autoflush=False` na `SessionLocal` (database.py): objeto
     apenas SUJO nao dispara flush sozinho, so um `flush()`/`commit()`
     explicito o faz.

     Hoje nada no caminho alcancavel daqui emite DML de Core (verificado na
     cadeia de chamadas inteira), e quem cobre esse ponto cego e a camada 3:
     o `rollback()` incondicional descarta a transacao aberta, DML de Core
     inclusa, porque nada neste modulo commita. O teste de AST (Cenario L de
     `tests/test_v2_sombra.py`) trava a ausencia dos nomes que abririam essa
     porta (`execute`, `text`, `update`, `add`, `merge`, `flush`, ...) — ou
     seja: o ponto cego e coberto por rollback em tempo de execucao e por
     ausencia de nome em tempo de teste, nunca pelo `before_flush`.

  3. `db.rollback()` INCONDICIONAL no `finally`. Cinto e suspensorio: mesmo
     numa saida por excecao, nada que porventura tenha entrado na sessao
     sobrevive ao pedido. NAO e a garantia principal — e a ultima. E, pelo
     paragrafo acima, e a UNICA que cobre DML de Core.

A CONEXAO NAO FICA PRESA DURANTE O GEMINI
===========================================
`_soltar_conexao` devolve a conexao ao pool antes de CADA chamada ao Gemini
(interpretacao e redacao) — mesmo remedio, pelo mesmo motivo, do incidente
AUDIT-2026-08-WF2 (D1) documentado em `routers/webhook.py`: o pool do
Conversas tem teto de 15 (pool_size=5 + max_overflow=10) e e compartilhado
com TODAS as outras rotas, o webhook real da Meta inclusive. Segurar uma
conexao durante duas idas ao modelo transformaria uma rota de OBSERVACAO em
causa de indisponibilidade do trafego de verdade.

Soltar a conexao NAO desarma a barreira: o listener e registrado na INSTANCIA
de `Session`, e `Session.close()` libera a conexao e desanexa os objetos sem
tocar no dispatch de eventos — a mesma `Session` volta a ficar barrada assim
que a proxima consulta reabre a transacao (provado no Cenario M de
`tests/test_v2_sombra.py`, que injeta escrita DEPOIS do release). Por isso
`_soltar_conexao` tambem recusa soltar uma sessao suja: `close()` sozinho
DESCARTARIA em silencio uma escrita pendente, e silencio e exatamente o que
este modulo nao pode produzir.

A barreira `before_flush` de `app/v2/compat.py` (registrada em toda a
`SessionLocal` do processo) so reage a `Conversation` SUJA. A sombra nunca
muta um objeto ORM, entao aquela barreira e inerte aqui — e, se deixasse de
ser, a camada 2 aborta o flush antes de qualquer commit.

CHAMAR O GEMINI E PERMITIDO — E O PONTO
=========================================
A interpretacao e a redacao REAIS sao o que se quer comparar. Gemini e I/O
pago, nao efeito operacional: nao muda linha nenhuma do nosso banco, nao
manda WhatsApp, nao mexe em lead/tag/funil. Falha do Gemini vira desfecho de
negocio (`INTERPRETACAO_FALHOU` / `COMUNICADOR_INDISPONIVEL`) com HTTP 200,
nunca um 500 em serie.

O QUE FOI DELIBERADAMENTE OMITIDO DO `executar_pipeline`
==========================================================
A ORDEM das decisoes e a mesma de `pipeline.py` — e o que torna o resultado
comparavel. O que sai sao so os efeitos:

  passo 2  `MESSAGE_RECEIVED`            OMITIDO — escreve `conversation_events`.
                                          Sem evento nao ha idempotency key, por
                                          isso `EntradaSombra` nao tem `event_id`
                                          nem `origem` (ambos so existiam para
                                          alimentar esse evento).
  passo 3  recusa de `state` fora da V2   OMITIDO — e a razao de ser da sombra.
  passo 4  `CLOSED`                       MANTIDO (leitura pura). Inerte em
                                          `LEGACY`, mas espelha o vivo.
  passo 5  `is_bot_active is False`       MANTIDO (leitura pura).
  passo 6  `MESSAGE_IGNORED`              OMITIDO — escreve. A DECISAO do
                                          pre-filtro e mantida.
  passo 7  eventos de `interpretar()`     OMITIDOS — usa `interpretar_sem_efeito`,
                                          que faz a MESMA chamada e a MESMA
                                          validacao sem os 3 eventos do ciclo.
  passo 8  `TRIAGE_*`                     OMITIDO — escreve. `avaliar_triagem`
                                          (pura) e chamada igual.
  passo 9b `HANDOFF_FAILED`               OMITIDO — escreve.
  passo 9b `handoff_to_human`             OMITIDO — e a UNICA funcao do pipeline
                                          com efeito real irreversivel. A
                                          SELECAO de atendente E feita (leitura
                                          pura), por
                                          `selecionar_atendente_sem_aviso` —
                                          mesmo resultado de
                                          `selecionar_atendente`, sem o
                                          `logger.warning` da V1 que anunciaria
                                          "conversa entra na fila sem dono"
                                          para uma conversa que a sombra jamais
                                          enfileirou. Ver o docstring daquela
                                          funcao em `selecao.py`.
  passo 11 `AI_RESPONSE_DISCARDED`        OMITIDO — usa `gate.motivo_recusa`, a
                                          funcao PURA com as mesmas seis
                                          condicoes na mesma ordem, sem o
                                          `_recusar` que persiste o evento.
  passo 12 `AI_RESPONSE_GENERATED`        OMITIDO — escreve. `redigir_resposta`
                                          (comunicador + output guard) roda
                                          igual: nao toca banco.

NENHUMA REGRA DE NEGOCIO E REIMPLEMENTADA AQUI. Pre-filtro, triagem, selecao,
gate, comunicador e guard sao as MESMAS funcoes do caminho vivo — inclusive a
selecao de atendente, cujo envelope silencioso apenas ADIA a chamada da regra
da V1 em vez de copia-la (ver `selecionar_atendente_sem_aviso` em `selecao.py`).

VOCABULARIO DO DESFECHO — `EtapaFinal` REUSADA, NAO CLONADA
=============================================================
`etapa_final` e o `EtapaFinal` de `pipeline.py`, sem enum paralelo: o objetivo
e comparar a decisao da sombra com a que a V2 viva tomaria, e dois vocabularios
diferentes destruiriam essa comparacao. Dois membros sao ESTRUTURALMENTE
inalcancaveis na sombra, porque dependem de executar o handoff de verdade:
`HANDOFF_CONCLUIDO` e `HANDOFF_INCONSISTENTE`.

`HANDOFF_SIMULADO_DRY_RUN` e o desfecho de "a V2 encaminharia para um humano
aqui". Na sombra ele NAO depende de `V2_MODE` — e o unico ramo possivel, e a
sombra para nele sem redigir resposta, exatamente como o pipeline vivo para
(em dry_run, por `HANDOFF_SIMULADO_DRY_RUN`; em live, por `HANDOFF_CONCLUIDO`
apos o handoff bem-sucedido). Nos dois modos o cliente nao recebe texto da IA
depois de um handoff, entao "sem resposta" e a leitura fiel.

UM SUB-CASO EM QUE A SOMBRA NAO MODELA O VIVO — LEIA ANTES DE COMPARAR
=======================================================================
Com `V2_MODE=live` E o handoff real FALHANDO (CRM fora do ar, por exemplo),
`executar_pipeline` NAO para no handoff: ele cai no mesmo fallthrough do ramo
"sem atendente elegivel" e ainda compoe e envia uma resposta ao cliente
(pipeline.py, passo 9b — comentario "Falha (ex.: CRM indisponivel) NAO retorna
aqui"). A sombra, que nunca executa handoff, relata `HANDOFF_SIMULADO_DRY_RUN`
com `responderia=False` nesse mesmo cenario. Ou seja: DURANTE UMA QUEDA DO CRM
COM A V2 EM LIVE, "a sombra disse que nao responderia" NAO e verdade sobre o
vivo. Isto e INERTE hoje (o vivo tambem roda em dry_run) e nao tem conserto
estrutural — modelar esse ramo exigiria executar um handoff de verdade, que e
precisamente o efeito que esta rota existe para nao ter.

PII NO CONTRATO DE SAIDA
=========================
`ResultadoSombra` nao carrega telefone, email, nome, payload cru, prompt,
saida crua do modelo nem segredo. `texto_resposta` e a EXCECAO consciente: e o
proprio texto sob comparacao, e ele pode conter o primeiro nome do cliente
(a IA Comunicadora recebe `fatos_confirmados.nome`). Quem registrar a
comparacao do lado do n8n deve gravar um BOOLEANO ("houve resposta"), nunca o
texto.
"""
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.v2.comunicador import ComunicadorIndisponivel, EntradaComunicador, redigir_resposta
from app.v2.estados import Estado
from app.v2.gate import motivo_recusa
from app.v2.interpretador import InterpretacaoFalhou, interpretar_sem_efeito
# `_acao_comunicador` e `EtapaFinal` vem de `pipeline.py` de proposito, mesmo
# sendo o primeiro um nome privado: as duas SAO a decisao do caminho vivo, e
# reproduzi-las aqui faria a sombra divergir do que ela existe para medir
# (a `acao` muda o prompt da IA Comunicadora, logo muda o texto comparado).
# Importar de `pipeline.py` nao torna `handoff_to_human`/`V2_MODE` alcancaveis
# neste modulo: so estes dois nomes entram no namespace da sombra.
from app.v2.pipeline import EtapaFinal, _acao_comunicador
from app.v2.prefiltro import MensagemLote, avaliar_prefiltro
from app.v2.selecao import selecionar_atendente_sem_aviso
from app.v2.triagem import avaliar_triagem


class EscritaProibidaNaSombra(RuntimeError):
    """
    A sessao da sombra tentou emitir um flush com objeto novo/sujo/apagado.

    NAO e desfecho de negocio e NAO vira HTTP 200: significa que alguem
    introduziu uma escrita num caminho que o contrato desta rota promete ser
    somente-leitura. Sobe ate virar 500, ruidosamente, para que o bug
    apareca — e o flush aborta antes de qualquer commit.
    """


@contextmanager
def _sessao_somente_leitura(db: Session) -> Iterator[None]:
    """
    Camada 2 do efeito zero (ver docstring do modulo). Listener local a ESTA
    `Session` — nunca em `SessionLocal`, que e compartilhada pelo processo
    inteiro e nao pode ficar somente-leitura para o resto da aplicacao.

    O listener e removido e a sessao sofre `rollback()` no `finally`, mesmo
    quando o corpo sai por excecao.
    """

    # `flush_context`/`instances` sao objetos internos do SQLAlchemy (um
    # `UOWTransaction` e a colecao de instancias) que esta barreira nao inspeciona
    # — anotados como `object` de proposito: a assinatura e ditada pelo evento
    # `before_flush`, e importar os tipos internos so para nomea-los acoplaria
    # este modulo a detalhe interno do ORM.
    def _bloquear_flush(session: Session, flush_context: object, instances: object) -> None:
        if session.new or session.dirty or session.deleted:
            raise EscritaProibidaNaSombra(
                "sombra: flush com mudanca pendente — a rota sombra nao escreve "
                f"(novos={len(session.new)}, sujos={len(session.dirty)}, "
                f"apagados={len(session.deleted)})"
            )

    event.listen(db, "before_flush", _bloquear_flush)
    try:
        yield
    finally:
        # `rollback()` PRIMEIRO, e os dois num try/finally aninhado: se
        # `event.remove` levantasse, a ordem anterior (remove -> rollback)
        # PULARIA o rollback, perdendo a ultima camada do efeito zero justo
        # numa saida ja anormal. Descartar a transacao e o que nao pode faltar;
        # remover o listener e higiene de uma `Session` que `get_db` fecha em
        # seguida de qualquer jeito.
        try:
            db.rollback()
        finally:
            event.remove(db, "before_flush", _bloquear_flush)


def _soltar_conexao(db: Session) -> None:
    """
    Devolve a conexao ao pool antes de uma espera longa pelo Gemini.

    Ver "A CONEXAO NAO FICA PRESA DURANTE O GEMINI" no docstring do modulo para
    o incidente que originou este padrao (AUDIT-2026-08-WF2 D1, em
    `routers/webhook.py`) e para a prova de que a barreira continua armada.

    `close()` e nao `commit()` — mesma escolha do precedente: alem de soltar a
    conexao ele DESANEXA os objetos, entao um acesso distraido a um objeto lido
    antes da espera falha alto em vez de devolver valor velho em silencio. Nao
    ha nada a perder no fechamento: este modulo so le. A proxima consulta abre
    transacao nova na MESMA `Session`, ainda com a barreira instalada.

    A recusa abaixo existe porque `close()` sozinho DESCARTARIA uma escrita
    pendente sem dizer nada — trocaria o 500 ruidoso da barreira por um sumico
    calado. Aqui a sessao suja vira a mesma excecao de invariante que um flush
    barrado produziria.
    """
    if db.new or db.dirty or db.deleted:
        raise EscritaProibidaNaSombra(
            "sombra: sessao suja ao soltar a conexao — a rota sombra nao escreve "
            f"(novos={len(db.new)}, sujos={len(db.dirty)}, apagados={len(db.deleted)})"
        )
    db.close()


class EntradaSombra(BaseModel):
    """
    Corpo de `POST /webhook/agent-bia-v2-shadow`.

    MINIMO DE PROPOSITO: so o que o no do n8n realmente tem em maos — o id da
    conversa e a mensagem do cliente. Comparado com `InboundContextV2`
    (`pipeline.py`), faltam dois campos, e a ausencia dos dois e estrutural,
    nao esquecimento: `origem` e `event_id` existiam SOMENTE para alimentar o
    evento `MESSAGE_RECEIVED`, e a sombra nao registra evento nenhum — nao ha
    o que rotular nem o que tornar idempotente.

    `lote` e obrigatorio e explicito (nunca derivado de
    `mensagem_interpretacao`): e ele que alimenta o pre-filtro, e o `msg_type`
    — nao o conteudo — e o que decide se a mensagem seria ignorada. Sem ele o
    resultado da sombra nao seria comparavel a decisao real da V2 para uma
    reacao ou um sticker. `MensagemLote` e o MESMO contrato do caminho vivo
    (`prefiltro.py`).

    TETO DE TAMANHO — POR QUE ELE EXISTE SO AQUI E NAO EM `InboundContextV2`.
    Cada pedido aceito paga UMA chamada de modelo por conta do corpo recebido.
    Sem teto, um corpo de 1.000.000 de caracteres ou um `lote` de 5.000 itens
    passava pelo contrato e virava custo (e, antes do timeout de
    `interpretador.py`/`comunicador.py`, espera sem fim). A rota VIVA fica como
    esta: mudar o contrato dela nao e assunto desta tarefa, e ela nao aceita
    conversa real.

    Os dois valores tem folga deliberada sobre o maior lote REALISTA, para que
    o teto nunca vire fonte de divergencia entre sombra e vivo:

      `lote` <= 50      o lote e o que se acumula na janela de debounce de 15s
                        (`AGENT_DEBOUNCE_SECONDS`, webhook.py). Nem uma rajada
                        agressiva de digitacao humana chega perto de 50
                        mensagens em 15 segundos.
      `mensagem_interpretacao` <= 16000
                        ~4x o limite de 4096 caracteres de UMA mensagem de
                        texto do WhatsApp, ou seja, cabe a concatenacao de
                        varias mensagens longas do mesmo lote. Um teto de 4000
                        seria APERTADO DEMAIS: reprovaria uma unica mensagem
                        de tamanho maximo, que e trafego perfeitamente normal.

    SEM `min_length`: string vazia continua aceita DE PROPOSITO. `lote` de
    reaction/sticker chega com `mensagem_interpretacao` vazia, e esse e
    exatamente o caso que deve ser observado terminando em `MENSAGEM_IGNORADA`
    (o pre-filtro decide pelo `msg_type` e nem chama o modelo). Recusar com 422
    ali cegaria a sombra no desfecho mais comum que ela existe para medir, sem
    economizar chamada nenhuma.
    """

    model_config = ConfigDict(extra="forbid")

    conversation_id: StrictInt = Field(ge=1)
    lote: list[MensagemLote] = Field(max_length=50)
    mensagem_interpretacao: StrictStr = Field(max_length=16000)


@dataclass(frozen=True)
class ResultadoSombra:
    """
    O que a V2 TERIA decidido. Nenhum campo descreve algo que aconteceu — o
    unico efeito desta rota e a chamada ao Gemini e a latencia dela.

    `etapa_final`        desfecho no vocabulario de `EtapaFinal` (`pipeline.py`).
    `triagem_completa`   `None` quando a triagem nem chegou a rodar (conversa
                         fechada, bot desligado, lote ignorado, interpretacao
                         falhou) — nunca `False` presumido.
    `handoff_sugerido`   `True` quando a V2 quereria encaminhar para humano,
                         MESMO que nao houvesse atendente elegivel (nesse caso
                         `atendente_sugerido_id` fica `None` e o fluxo segue
                         para o comunicador, como no caminho vivo).
    `responderia`        derivado: houve texto aprovado pelo output guard.
    `texto_resposta`     o texto sugerido; pode conter o primeiro nome do
                         cliente — ver "PII" no docstring do modulo.
    `erro_tecnico`       token de diagnostico do passo que interrompeu o fluxo
                         (`CHAMADA_LLM_FALHOU`, `RESPOSTA_INVALIDA`,
                         `chamada_llm_falhou`, `gemini_api_key_ausente`, um dos
                         seis tokens de recusa do gate, ou um dos dois motivos
                         do output guard). SEMPRE de vocabulario fechado —
                         nunca texto livre, nunca excecao crua do SDK.
    `latencia_ms`        tempo de parede da simulacao inteira, Gemini incluido.
    """

    conversation_id: int
    etapa_final: EtapaFinal
    triagem_completa: bool | None
    handoff_sugerido: bool
    atendente_sugerido_id: int | None
    responderia: bool
    texto_resposta: str | None
    erro_tecnico: str | None
    latencia_ms: int


def simular_pipeline(db: Session, entrada: EntradaSombra) -> ResultadoSombra:
    """
    Percorre as decisoes de `executar_pipeline` sem nenhum efeito operacional.

    Levanta `ValueError` se `conversation_id` nao existir — mesmo idioma de
    `pipeline.py`/`gate.py` para erro de chamador, traduzido em 404 pelo
    router. Nunca levanta por `state` (aceitar `LEGACY` e o proposito da rota).
    """
    inicio = time.monotonic()

    def _resultado(
        etapa: EtapaFinal,
        conversation_id: int,
        *,
        triagem_completa: bool | None = None,
        handoff_sugerido: bool = False,
        atendente_sugerido_id: int | None = None,
        texto_resposta: str | None = None,
        erro_tecnico: str | None = None,
    ) -> ResultadoSombra:
        return ResultadoSombra(
            conversation_id=conversation_id,
            etapa_final=etapa,
            triagem_completa=triagem_completa,
            handoff_sugerido=handoff_sugerido,
            atendente_sugerido_id=atendente_sugerido_id,
            responderia=texto_resposta is not None,
            texto_resposta=texto_resposta,
            erro_tecnico=erro_tecnico,
            latencia_ms=int((time.monotonic() - inicio) * 1000),
        )

    with _sessao_somente_leitura(db):
        conversation = db.get(Conversation, entrada.conversation_id)
        if conversation is None:
            raise ValueError(
                f"simular_pipeline: conversation_id {entrada.conversation_id} nao encontrada"
            )

        # Valores simples, nao referencia viva ao objeto ORM — mesma disciplina
        # do passo 1 de `executar_pipeline`. Sem o passo 3 (recusa por `state`):
        # `LEGACY` e justamente o caso que esta rota existe para observar.
        conversation_id = conversation.id
        state_esperado = conversation.state
        version_esperada = conversation.state_version

        if conversation.state == Estado.CLOSED.value:
            return _resultado(
                EtapaFinal.CONVERSA_FECHADA_REABERTURA_NAO_IMPLEMENTADA, conversation_id
            )

        if not conversation.is_bot_active:
            return _resultado(EtapaFinal.BOT_INATIVO, conversation_id)

        decisao_prefiltro = avaliar_prefiltro(entrada.lote)
        if decisao_prefiltro.ignorar:
            return _resultado(
                EtapaFinal.MENSAGEM_IGNORADA,
                conversation_id,
                erro_tecnico=decisao_prefiltro.motivo,
            )

        # Tudo que ainda sera preciso do banco ja esta em `conversation_id`,
        # `state_esperado` e `version_esperada` (escalares, acima) — nada abaixo
        # depende do objeto ORM lido antes desta linha. Solta a conexao: segue
        # uma ida ao Gemini de duracao nao controlada por nos.
        _soltar_conexao(db)
        try:
            resultado_ia = interpretar_sem_efeito(entrada.mensagem_interpretacao)
        except InterpretacaoFalhou as exc:
            return _resultado(
                EtapaFinal.INTERPRETACAO_FALHOU, conversation_id, erro_tecnico=exc.error_code
            )

        resultado_triagem = avaliar_triagem(resultado_ia.extracted)

        # R-INTENT: mesma decisao do passo 9 do pipeline vivo — SOMENTE os dois
        # booleanos validados por schema, nunca o texto livre de `intent`.
        deve_encaminhar_humano = resultado_ia.explicit_human_request or resultado_triagem.completa

        # Reabre transacao nova na MESMA `Session` (barreira ainda instalada).
        atendente_id = None
        if deve_encaminhar_humano:
            atendente_id = selecionar_atendente_sem_aviso(db)
            if atendente_id is not None:
                # Espelha o unico ramo de handoff que a sombra pode ter: a V2
                # pararia aqui, sem redigir resposta. `handoff_to_human` nao e
                # importado neste modulo.
                return _resultado(
                    EtapaFinal.HANDOFF_SIMULADO_DRY_RUN,
                    conversation_id,
                    triagem_completa=resultado_triagem.completa,
                    handoff_sugerido=True,
                    atendente_sugerido_id=atendente_id,
                )
            # Sem atendente elegivel: mesmo fallthrough do caminho vivo — segue
            # para compor resposta com o que ja se sabe, sem anunciar
            # encaminhamento.

        # Gate, avaliado SEM persistir a recusa. `populate_existing=True` e a
        # releitura que `autorizar_envio` faz (leitura pura); `motivo_recusa` e
        # a mesma funcao que ele usa para decidir. O parametro e MANTIDO mesmo
        # tendo virado redundante depois de `_soltar_conexao` (com o mapa de
        # identidade vazio, `db.get` ja vai ao banco): o que ele documenta e a
        # exigencia — "esta leitura tem de ser fresca" — e ela continua valendo
        # se alguem remover o release acima. O SELECT emitido e o mesmo.
        atual = db.get(Conversation, conversation_id, populate_existing=True)
        if atual is None:
            raise ValueError(
                f"simular_pipeline: conversation_id {conversation_id} nao encontrada"
            )
        motivo = motivo_recusa(
            atual, state_esperado=state_esperado, version_esperada=version_esperada
        )
        if motivo is not None:
            return _resultado(
                EtapaFinal.RESPOSTA_DESCARTADA_GATE,
                conversation_id,
                triagem_completa=resultado_triagem.completa,
                handoff_sugerido=deve_encaminhar_humano,
                erro_tecnico=motivo,
            )

        # Ultima leitura do banco ja aconteceu (o gate, logo acima) e `motivo`
        # ja foi decidido. Solta a conexao antes da SEGUNDA ida ao Gemini —
        # `atual` fica desanexado de proposito e nao e tocado daqui para baixo.
        _soltar_conexao(db)
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
            return _resultado(
                EtapaFinal.COMUNICADOR_INDISPONIVEL,
                conversation_id,
                triagem_completa=resultado_triagem.completa,
                handoff_sugerido=deve_encaminhar_humano,
                erro_tecnico=str(exc),
            )

        if not resultado_guard.permitido:
            return _resultado(
                EtapaFinal.RESPOSTA_BLOQUEADA_GUARD,
                conversation_id,
                triagem_completa=resultado_triagem.completa,
                handoff_sugerido=deve_encaminhar_humano,
                erro_tecnico=resultado_guard.motivo,
            )

        return _resultado(
            EtapaFinal.RESPOSTA_GERADA,
            conversation_id,
            triagem_completa=resultado_triagem.completa,
            handoff_sugerido=deve_encaminhar_humano,
            texto_resposta=resultado_guard.texto_final,
        )
