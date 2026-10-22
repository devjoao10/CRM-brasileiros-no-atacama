"""
Maquina de estados da V2 - BIA-V2, Fase 4.

`aplicar_transicao()` aplica UMA transicao de `Estado` a uma conversa, ou
levanta `TransicaoInvalida` sem mutar nada. `decidir_reabertura()` e
`dentro_janela_reabertura()` decidem o DESTINO de uma reabertura a partir de
fatos ja resolvidos por quem chama. As tres sao PURAS: sem `Session`, sem
IO, sem evento, sem relogio proprio (exceto `_agora_utc()`, isolado de
proposito - ver abaixo).

PUREZA: `datetime` E PERMITIDO AQUI, AO CONTRARIO DA FASE 3 (decisao 14)
A allowlist de imports desta fase e `datetime`, `enum`, `re`, `typing`,
`types`, `__future__` (`types` foi acrescentado para permitir
`types.MappingProxyType` em `TRANSICOES_PERMITIDAS` - stdlib puro, sem
acoplamento de infra, mesma categoria de `enum`/`datetime` ja permitidos) -
DIVERGE da Fase 3 (`prefiltro.py`), que PROIBE `datetime`
porque e funcao de texto sem relogio. Esta fase PRECISA de `datetime.now`
(fecho de `closed_at`, decisao 7) e de `timedelta` (janela de reabertura,
decisao 9); copiar a lista da Fase 3 sem esse ajuste proibiria exatamente o
que esta fase requer. Continuam PROIBIDOS, inclusive dentro de
`if TYPE_CHECKING` (a prova de pureza percorre a arvore com `ast.walk`, que
enxerga import dentro de bloco condicional): `sqlalchemy`, `fastapi`,
`httpx`, `requests`, `os`, `pathlib`, `subprocess`, `sqlite3`, `psycopg2`,
`app.database`, `app.models`, `app.services`, `app.config`, `app.routers`,
`app.schemas`, `app.main`, `app.v2.eventos`.

`conversation` TIPADO POR `Protocol` LOCAL, NAO PELA CLASSE ORM (decisao 14)
Anotar com a classe ORM real exigiria `from app.models.conversation import
Conversation`, que arrasta `app.database` (Base) - proibido acima. Esconder
o import sob `if TYPE_CHECKING` nao escapa disso: a prova de pureza usa
`ast.walk`. A saida e um `typing.Protocol` LOCAL a este modulo
(`_ConversationEstado`, definido logo antes de `_validar_campos_operacionais`),
com so os nove atributos que as quatro funcoes abaixo de fato leem ou
escrevem - nunca a classe ORM inteira. `typing` ja esta na allowlist desta
fase, entao a anotacao nao abre import proibido; como o projeto nao roda
typechecker, ela e documentacao reforcada, nao enforcement - por isso o
Protocol NAO e `@runtime_checkable` e nenhum `isinstance` roda contra ele
aqui. Ha precedente na vizinhanca: `service_window_open` em
`conversas/app/models/conversation.py` deixa o parametro sem anotacao
nenhuma; aqui um Protocol local resolve a mesma tensao sem esse custo.

`aplicar_transicao` NAO PROVA HANDOFF (decisao 4)
O grafo `TRANSICOES_PERMITIDAS` representa POSSIBILIDADE ESTRUTURAL:
`AI_TRIAGE -> WAITING_HUMAN` e `CLOSED -> WAITING_HUMAN` existem nele porque
sao estruturalmente possiveis, nao porque esta funcao verificou handoff. Ela
nao recebe `db`, nao consulta `handoff_operations`, nao conhece o CRM - so
valida invariantes locais observaveis no objeto que recebeu. A obrigacao
"nao existe WAITING_HUMAN sem handoff confirmado" e PRECONDICAO DO CALLER
que possui a operacao de handoff (Fase 6), nao desta funcao.

TRES SELF-TRANSITIONS SAO TRANSICOES REAIS, NAO NO-OP
`AI_TRIAGE->AI_TRIAGE`, `WAITING_HUMAN->WAITING_HUMAN` e
`HUMAN_ACTIVE->HUMAN_ACTIVE` incrementam `state_version` como qualquer
outra - a segunda e a terceira sao troca de dono, a mudanca mais
consequente que existe para uma resposta da IA em voo (decisao 3). `NEW` e
`LEGACY` nunca sao destino valido nas Fases 0-10: nenhuma aresta de
`TRANSICOES_PERMITIDAS` leva a eles (`NEW` e so origem; `LEGACY` so sai
desta fase pela "Promocao LEGACY -> V2", fora de 0-10).

`closed_at` E CAMPO DE ESTADO V2, `aplicar_transicao` E A UNICA DONA
(decisao 7). Entrando em `CLOSED`: grava `_agora_utc()`. Saindo de `CLOSED`
(reabertura): limpa para `None`, e exige que o valor ATUAL nao seja `None`
antes disso (`CLOSED` com `closed_at=None` e corrupcao, nao estado valido).
Transicao aberto->aberto (inclusive self-transition): exige que `closed_at`
JA seja `None` - nao "cura" valor antigo em silencio. Os SEIS campos
operacionais (`is_bot_active`, `atendente_id`, `responsavel_id`,
`queued_at`, `primeira_resposta_humana_at`, `status`) sao so LIDOS, nunca
escritos - quem os escreve e o caller, na mesma unidade transacional.

ESTADO DESCONHECIDO OU `None`: FAIL CLOSED, NUNCA `LEGACY` (decisao 10)
`state` persiste como `String(32)` livre, sem CHECK de vocabulario - uma
linha corrompida cabe em 32 caracteres. Por isso a maquina NUNCA mapeia
`None` ou string fora do vocabulario para `LEGACY`: isso faria `LEGACY`
significar duas coisas indistinguiveis (nunca-promovida OU
corrompida-e-mascarada), e uma conversa corrompida seguiria sendo tocada
pela V1 em silencio, sem o sinal de fail-closed que esta decisao promete.
`state is None` ou fora do vocabulario levanta
`TransicaoInvalida(estado_atual_invalido)` - o `ValueError` cru do proprio
enum nunca atravessa a fronteira desta funcao.

UMA EXCECAO, SEIS TOKENS, `motivo` NUNCA APARECE (decisao 6)
`TransicaoInvalida(ValueError)` fica na forma minima - sem DTO, sem
`error_code`, sem atributo customizado. O vocabulario fechado
(`estado_atual_invalido`, `destino_invalido`, `aresta_nao_permitida`,
`state_version_invalido`, `invariante_destino`, `motivo_invalido`) e o que
recupera diagnosticabilidade sem abrir canal de PII. Regra POR TOKEN:
`estado_atual_invalido`, `destino_invalido`, `state_version_invalido` e
`motivo_invalido` levam SO o token, nunca o valor cru recebido ou lido do
banco; `aresta_nao_permitida` e `invariante_destino` podem incluir os
estados envolvidos, porque a essa altura ja sao membros validados de
`Estado` (vocabulario fechado, nao dado de cliente). `motivo` nunca aparece
em mensagem nenhuma, sob nenhum token - mesmo cuidado que
`PayloadInvalido`/`CampoInvalido` ja tomam em `eventos_validacao.py`.

ORDEM ATOMICA (decisao 7)
Todas as validacoes rodam ANTES de qualquer mutacao, nesta ordem: motivo,
estado atual, destino, aresta, `state_version`, e por ultimo as invariantes
do destino - passo unico que cobre os seis campos operacionais E o
`closed_at` PROSPECTIVO (calculado, mas nao escrito ainda). Se qualquer uma
falhar, `state`, `state_version` e `closed_at` permanecem EXATAMENTE como
estavam. So depois de todas passarem os tres campos sao escritos, juntos.

`decidir_reabertura`/`dentro_janela_reabertura`: PURAS, SEM `db` (decisoes
8 e 9). A primeira recebe fatos ja resolvidos (`dentro_janela`,
`dono_anterior_elegivel`, `novo_dono_disponivel`) e devolve o `Estado`
PRETENDIDO - nao executa handoff. `dono_anterior_elegivel is None` significa
"nao existe dono humano anterior utilizavel", nao "elegibilidade
desconhecida". A segunda e UM PREDICADO UNICO
(`closed_at_utc <= agora_utc < closed_at_utc + janela`), nao a soma de
quatro regras independentes - elas sao CONSEQUENCIA da formula, e por isso
nao podem ser lidas em separado. `agora` e OBRIGATORIO (sem relogio
interno), para o teste ser deterministico; `janela` nao-positiva nao
levanta, so produz `False` (intervalo vazio/invertido e entrada valida).
"""
import re
import types
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Protocol


class Estado(StrEnum):
    """
    Os seis estados da conversa na V2. O valor de cada membro e o proprio
    NOME, em maiusculo (decisao 3) - `auto()` geraria o nome em minusculas e
    romperia o protocolo persistido em `conversation.state`, o CHECK do
    banco (que compara contra as strings literais) e a coercao
    string->Estado feita por `aplicar_transicao`. A ORDEM DE DECLARACAO
    abaixo tambem e contrato: e a ordem que `list(Estado)` devolve.
    """

    NEW = "NEW"
    AI_TRIAGE = "AI_TRIAGE"
    WAITING_HUMAN = "WAITING_HUMAN"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"
    CLOSED = "CLOSED"
    LEGACY = "LEGACY"


# Decisao 3 - ALLOWLIST exata, 15 arestas. Par ausente = proibido, e a
# proibicao e ESTRUTURAL: a ausencia da chave no conjunto e o que recusa.
# `NEW` e `LEGACY` nunca aparecem em nenhum conjunto de destino: `NEW` e so
# origem (estado inicial - ninguem transiciona PARA ele) e `LEGACY` so sai
# desta fase pela "Promocao LEGACY -> V2" (fora das Fases 0-10). As tres
# self-transitions (`AI_TRIAGE`, `WAITING_HUMAN`, `HUMAN_ACTIVE`) sao
# transicoes REAIS, nao no-op - ver docstring do modulo.
#
# Valores em `frozenset`, mesma razao de `_VALORES_ESTADO` mais abaixo: um
# `TRANSICOES_PERMITIDAS[Estado.LEGACY].add(Estado.AI_TRIAGE)` acidental (ou
# de um bug em qualquer lugar do processo, ja que isto e um objeto global
# avaliado uma vez no import) reabriria essa promocao em silencio para o
# resto do processo; `frozenset` recusa `.add()` com `AttributeError`. O
# dicionario EXTERNO e `types.MappingProxyType`, nao `dict`: recusa
# reatribuicao de chave (`TRANSICOES_PERMITIDAS[x] = ...`) e
# `.clear()`/`.pop()`/`.update()` com `TypeError`. `types` esta na
# allowlist de imports desta fase (checagem H1 em tests/test_v2_estados.py)
# especificamente para isto - e stdlib puro, sem acoplamento de infra.
TRANSICOES_PERMITIDAS: types.MappingProxyType[Estado, frozenset[Estado]] = types.MappingProxyType({
    Estado.NEW:           frozenset({Estado.AI_TRIAGE, Estado.CLOSED}),
    Estado.AI_TRIAGE:     frozenset({Estado.AI_TRIAGE, Estado.WAITING_HUMAN, Estado.CLOSED}),
    Estado.WAITING_HUMAN: frozenset({Estado.WAITING_HUMAN, Estado.HUMAN_ACTIVE,
                                      Estado.AI_TRIAGE, Estado.CLOSED}),
    Estado.HUMAN_ACTIVE:  frozenset({Estado.HUMAN_ACTIVE, Estado.AI_TRIAGE, Estado.CLOSED}),
    Estado.CLOSED:        frozenset({Estado.HUMAN_ACTIVE, Estado.WAITING_HUMAN, Estado.AI_TRIAGE}),
    Estado.LEGACY:        frozenset(),
})


class TransicaoInvalida(ValueError):
    pass


# ---------------------------------------------------------------------------
# Decisao 6 - vocabulario FECHADO de seis tokens. E o que distingue o modo de
# recusa na mensagem (`str(exc)`) sem abrir canal de PII: o token e fato
# INTERNO da maquina, nunca dado de cliente. `motivo` nunca aparece em
# mensagem nenhuma, sob nenhum token.
# ---------------------------------------------------------------------------
_TOKEN_ESTADO_ATUAL_INVALIDO = "estado_atual_invalido"
_TOKEN_DESTINO_INVALIDO = "destino_invalido"
_TOKEN_ARESTA_NAO_PERMITIDA = "aresta_nao_permitida"
_TOKEN_STATE_VERSION_INVALIDO = "state_version_invalido"
_TOKEN_INVARIANTE_DESTINO = "invariante_destino"
_TOKEN_MOTIVO_INVALIDO = "motivo_invalido"


def _erro_aresta_nao_permitida(estado_atual: Estado, destino: Estado) -> TransicaoInvalida:
    """Token + os dois estados - a esta altura, ja membros validados de `Estado`."""
    return TransicaoInvalida(f"{_TOKEN_ARESTA_NAO_PERMITIDA}: {estado_atual.value} -> {destino.value}")


def _erro_invariante_destino(destino: Estado) -> TransicaoInvalida:
    """Token + destino, ja validado (decisao 6)."""
    return TransicaoInvalida(f"{_TOKEN_INVARIANTE_DESTINO}: {destino.value}")


# Decisao 13 - gramatica de FORMA, sem vocabulario fixo. `re.fullmatch`,
# NUNCA `re.match` com padrao terminado em `$`: sem `re.MULTILINE`, `$` casa
# tambem antes de UM `\n` final, entao `match(r"^...$", "ok\n")`
# retornaria match. O padrao vulneravel ja existe em `_validar_token` de
# `eventos_validacao.py` (aplica `regex.match` com padroes `^...$`); nao
# repetir aqui.
_REGEX_MOTIVO = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _validar_motivo(motivo: object) -> None:
    """`isinstance` primeiro: `None` (ou qualquer nao-str) nunca chega a `.fullmatch()`."""
    if not isinstance(motivo, str) or _REGEX_MOTIVO.fullmatch(motivo) is None:
        raise TransicaoInvalida(_TOKEN_MOTIVO_INVALIDO)


_VALORES_ESTADO = frozenset(membro.value for membro in Estado)


def _coagir_estado_atual(state: object) -> Estado:
    """
    Coage `conversation.state` (string livre, `String(32)` sem CHECK de
    vocabulario - decisao 10) para `Estado`, fail-closed: `None` e qualquer
    string fora do vocabulario levantam `TransicaoInvalida`, nunca o
    `ValueError` cru do enum (decisao 3).

    A checagem e por PERTENCIMENTO, nao por `try/except Estado(state)`, e a
    diferenca importa para PII. `Estado(valor_invalido)` levanta um
    `ValueError` cuja mensagem ECOA o valor recebido; capturado e reencadeado
    com `from None`, ele some do traceback impresso (`__suppress_context__`)
    mas CONTINUA vivo em `__context__`, alcancavel por qualquer coletor de
    erro que percorra a cadeia - e `state` e coluna livre que pode carregar
    dado de cliente corrompido. Testar `state not in _VALORES_ESTADO` antes
    nunca constroi a excecao interna: nao ha `__context__` para vazar.
    """
    if not isinstance(state, str) or state not in _VALORES_ESTADO:
        raise TransicaoInvalida(_TOKEN_ESTADO_ATUAL_INVALIDO)
    return Estado(state)


def _validar_destino(destino: object) -> None:
    """
    `isinstance` EXPLICITO (decisao 6): `Estado` e `StrEnum`, que herda
    igualdade e hash de `str` - uma string crua igual ao VALOR de um membro
    (`"WAITING_HUMAN"`) compara igual a ele sem ser instancia da classe, e
    passaria por qualquer teste de pertencimento em `TRANSICOES_PERMITIDAS`
    como se fosse membro do enum.
    """
    if not isinstance(destino, Estado):
        raise TransicaoInvalida(_TOKEN_DESTINO_INVALIDO)


def _validar_aresta(estado_atual: Estado, destino: Estado) -> None:
    if destino not in TRANSICOES_PERMITIDAS[estado_atual]:
        raise _erro_aresta_nao_permitida(estado_atual, destino)


def _validar_state_version(valor: object) -> int:
    """
    `bool` e subclasse de `int` em Python (`isinstance(True, int)` e
    `True`) - rejeitar EXPLICITAMENTE e ANTES de aceitar como `int`, mesmo
    formato de `validar_inteiro` em `conversas/app/v2/eventos_validacao.py`.
    `None` e qualquer outro tipo (`"7"`, `7.0`, `object()`) caem no
    `not isinstance(valor, int)` seguinte; `valor < 0` so roda depois que
    `valor` ja e sabidamente `int`.
    """
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
        raise TransicaoInvalida(_TOKEN_STATE_VERSION_INVALIDO)
    return valor


def _agora_utc() -> datetime:
    """Relogio isolado num unico ponto nomeado, para o teste substituir por `unittest.mock.patch`."""
    return datetime.now(timezone.utc)


class _ConversationEstado(Protocol):
    """
    Forma estrutural minima que `aplicar_transicao` e as tres auxiliares
    privadas abaixo exigem de `conversation` - so os nove atributos
    efetivamente lidos ou escritos por elas, nunca a classe ORM inteira
    (ver "conversation tipado por Protocol local" no docstring do modulo).

    `state` e `state_version` ficam `object`: as funcoes que os recebem
    (`_coagir_estado_atual`, `_validar_state_version`) ja os tipam como
    `object`, porque validam o tipo em runtime a partir de uma coluna
    livre - reafirmar aqui um tipo mais especifico seria uma garantia que
    o proprio codigo nao da.
    """

    state: object
    state_version: object
    closed_at: datetime | None
    is_bot_active: bool
    atendente_id: int | None
    responsavel_id: int | None
    queued_at: datetime | None
    primeira_resposta_humana_at: datetime | None
    status: str


def _validar_campos_operacionais(conversation: _ConversationEstado, destino: Estado) -> None:
    """
    Confere os SEIS campos operacionais - ja escritos pelo caller na mesma
    unidade transacional - contra a tabela de invariantes da aplicacao
    (decisao 5). `aplicar_transicao` nunca escreve estes campos, so le.
    `CLOSED` preserva os quatro campos de dono/fila sem exigir valor
    especifico (decisao 7, C.2). `NEW` e `LEGACY` nunca chegam aqui como
    `destino`: nenhuma aresta de `TRANSICOES_PERMITIDAS` leva a eles.
    """
    is_bot_active = conversation.is_bot_active
    atendente_id = conversation.atendente_id
    responsavel_id = conversation.responsavel_id
    queued_at = conversation.queued_at
    primeira_resposta_humana_at = conversation.primeira_resposta_humana_at
    status = conversation.status

    if destino in (Estado.NEW, Estado.AI_TRIAGE):
        ok = (
            is_bot_active is True
            and atendente_id is None
            and queued_at is None
            and primeira_resposta_humana_at is None
            and status == "aberta"
        )
    elif destino is Estado.WAITING_HUMAN:
        ok = (
            is_bot_active is False
            and atendente_id is not None
            and responsavel_id is not None
            and responsavel_id == atendente_id
            and queued_at is not None
            and primeira_resposta_humana_at is None
            and status == "aberta"
        )
    elif destino is Estado.HUMAN_ACTIVE:
        ok = (
            is_bot_active is False
            and atendente_id is not None
            and responsavel_id is not None
            and responsavel_id == atendente_id
            and queued_at is not None
            and primeira_resposta_humana_at is not None
            and status == "aberta"
        )
    else:  # Estado.CLOSED
        ok = is_bot_active is False and status == "encerrada"

    if not ok:
        raise _erro_invariante_destino(destino)


def _calcular_closed_at_prospectivo(
    conversation: _ConversationEstado, estado_atual: Estado, destino: Estado
) -> datetime | None:
    """
    Deriva o `closed_at` PROSPECTIVO da transicao e valida a consistencia do
    valor ATUAL contra a decisao 7 - sem mutar `conversation`. Chamada como
    parte de "validar as invariantes do destino": qualquer falha usa o
    mesmo token `invariante_destino` das demais invariantes, porque a
    decisao 6 fecha o vocabulario em seis tokens e nao abre um setimo so
    para `closed_at` (que a decisao 7 trata como campo de estado V2, nao
    como um dos seis campos operacionais - mas continua sendo, ele proprio,
    uma invariante do destino).
    """
    if destino is Estado.CLOSED:
        return _agora_utc()
    if estado_atual is Estado.CLOSED:
        # Reabertura: closed_at ATUAL precisa existir - CLOSED com
        # closed_at=None e corrupcao, nao estado valido (decisao 7).
        if conversation.closed_at is None:
            raise _erro_invariante_destino(destino)
        return None
    # aberto -> aberto (inclusive self-transition): closed_at ATUAL precisa
    # JA ser None - nao "curar" valor antigo em silencio (decisao 7).
    if conversation.closed_at is not None:
        raise _erro_invariante_destino(destino)
    return None


def _validar_invariantes_destino(
    conversation: _ConversationEstado, estado_atual: Estado, destino: Estado
) -> datetime | None:
    """Passo unico da ordem atomica: campos operacionais + closed_at prospectivo. Devolve o closed_at a escrever."""
    _validar_campos_operacionais(conversation, destino)
    return _calcular_closed_at_prospectivo(conversation, estado_atual, destino)


def aplicar_transicao(conversation: _ConversationEstado, destino: Estado, *, motivo: str) -> None:
    """
    Aplica UMA transicao de estado a `conversation`, ou levanta
    `TransicaoInvalida` sem mutar nada (decisao 7, "ordem atomica"): TODAS
    as validacoes rodam antes de QUALQUER mutacao, na ordem motivo -> estado
    atual -> destino -> aresta -> state_version -> invariantes do destino
    (que inclui o `closed_at` prospectivo). So entao `state`,
    `state_version` e `closed_at` sao escritos, juntos.

    NAO recebe `Session`, nao abre sessao, nao faz add/flush/commit/rollback,
    nao registra evento, nao faz IO (decisao 14) - so muta os tres campos de
    estado V2 no proprio objeto, e LE os seis campos operacionais para
    validar o destino. `conversation` e tipado como `_ConversationEstado`
    (`Protocol` local, nao a classe ORM) - ver o docstring do modulo.
    """
    _validar_motivo(motivo)
    estado_atual = _coagir_estado_atual(conversation.state)
    _validar_destino(destino)
    _validar_aresta(estado_atual, destino)
    state_version = _validar_state_version(conversation.state_version)
    novo_closed_at = _validar_invariantes_destino(conversation, estado_atual, destino)

    conversation.state = destino.value
    conversation.state_version = state_version + 1
    conversation.closed_at = novo_closed_at


# ---------------------------------------------------------------------------
# Reabertura (decisoes 8 e 9) - subsistema independente de aplicar_transicao:
# funcoes puras que DECIDEM o destino pretendido a partir de fatos ja
# resolvidos pelo caller. Nenhuma das duas chama nem e chamada por
# aplicar_transicao nesta fase.
# ---------------------------------------------------------------------------
JANELA_REABERTURA_PADRAO = timedelta(hours=24)


def _para_utc(momento: datetime) -> datetime:
    """`datetime` naive e interpretado como UTC (precedente de `conversation.py`); aware e convertido para UTC."""
    if momento.tzinfo is None:
        return momento.replace(tzinfo=timezone.utc)
    return momento.astimezone(timezone.utc)


def dentro_janela_reabertura(
    closed_at: datetime | None,
    *,
    agora: datetime,
    janela: timedelta = JANELA_REABERTURA_PADRAO,
) -> bool:
    """
    PREDICADO UNICO (decisao 9), nao a soma de quatro regras separadas -
    elas sao CONSEQUENCIA da formula, nao pontos de decisao independentes:

        closed_at_utc <= agora_utc < closed_at_utc + janela

    `closed_at is None` produz `False` antes mesmo de normalizar. `janela`
    nao-positiva NAO levanta - intervalo vazio/invertido e entrada VALIDA,
    so produz `False` (decisao 9); fica assim de proposito, para que ninguem
    acrescente um `raise` que a spec nao pede.
    """
    if closed_at is None:
        return False
    closed_at_utc = _para_utc(closed_at)
    agora_utc = _para_utc(agora)
    return closed_at_utc <= agora_utc < closed_at_utc + janela


def decidir_reabertura(
    *,
    dentro_janela: bool,
    dono_anterior_elegivel: bool | None,
    novo_dono_disponivel: bool,
) -> Estado:
    """
    Funcao PURA (decisao 8): recebe fatos ja resolvidos por quem chama - sem
    `db`, sem IDs, sem efeito colateral. `dono_anterior_elegivel is None`
    significa "nao existe dono humano anterior utilizavel", nunca
    "elegibilidade desconhecida". Decide o DESTINO PRETENDIDO da reabertura;
    nao executa handoff - a regra 4 (`WAITING_HUMAN`) exige
    `handoff_operation` completa por quem for executa-la (Fase 6).
    """
    if not dentro_janela:
        return Estado.AI_TRIAGE
    if dono_anterior_elegivel is None:
        return Estado.AI_TRIAGE
    if dono_anterior_elegivel:
        return Estado.HUMAN_ACTIVE
    if novo_dono_disponivel:
        return Estado.WAITING_HUMAN
    return Estado.AI_TRIAGE
