"""
BIA-V2 R15/R16 — a FILA de `handoff_operations`: particao e reclamacao.

Fonte normativa: `bia-v2-r15-r17-design-v4.md`, secoes 4.2 (particao entre os
dois workers) e 5 (protocolo de reclamacao).

POR QUE ESTE MODULO EXISTE, EM VEZ DE CADA WORKER TER O SEU
------------------------------------------------------------
Os dois workers sao PROCESSOS SEPARADOS com laco proprio (`worker_retomada.py`
e `worker_reconciliacao.py`) — isso e requisito, nao acidente: eles fazem
coisas diferentes com linhas diferentes, e um laco unico que tentasse as duas
reintroduziria o acoplamento que a secao 4.2 existe para desfazer.

O que eles COMPARTILHAM e uma unica coisa: a regra de que suas fatias sao
DISJUNTAS. Essa regra so e verificavel se as duas metades estiverem escritas
lado a lado — duas copias do predicado, uma em cada worker, sao duas coisas
livres para divergir, e o dia em que divergirem os dois workers vao disputar a
MESMA linha com intencoes contraditorias (R16 quer restaurar o alvo ORIGINAL,
R15-B quer convergir para o dono LOCAL atual; nenhum e idempotente em relacao
ao outro, e o CAS decidiria por escalonamento). Por isso `criterios_r16()` e
`criterios_r15()` moram no mesmo arquivo, e `tests/test_v2_workers.py` prova a
disjuncao contra um universo de linhas construido de proposito.

A PARTICAO, LITERAL (secao 4.2)
-------------------------------
- **R16 (retomada)**: `status='REQUESTED'` OU (`status='FAILED'` E
  `error_class IN ('NAO_APLICADO_RETRIAVEL','AMBIGUO_RETRIAVEL')`). Nunca uma
  linha com `error_code IN ('CRM_LEAD_JA_ATRIBUIDO','ESTADO_LOCAL_RECUSADO')`.
  A exclusao por CODIGO e explicita mesmo parecendo redundante: hoje
  `_registrar_falha` grava `NAO_APLICADO_TERMINAL` para o primeiro e `NULL`
  para o segundo, entao nenhum dos dois cairia na lista de classes — mas
  "nenhum dos dois cai hoje" e uma propriedade de OUTRO arquivo, e a secao 4.2
  exige que a exclusividade de R15 seja estrutural aqui.
- **R15 (reconciliacao)**: `status='FAILED'` E `error_code IN
  ('CRM_LEAD_JA_ATRIBUIDO','ESTADO_LOCAL_RECUSADO')` E `reconciled_at IS NULL`.
  Reenviar o `event_id` original (o que R16 faz) NAO e a correcao certa para
  essas linhas: elas precisam da convergencia de R15, com `operation_id`
  DERIVADO.

O TETO DE TENTATIVAS VIVE NO PREDICADO, NAO NUM ESTADO NOVO
------------------------------------------------------------
`attempt_count < TETO_TENTATIVAS` e clausula de ELEGIBILIDADE, e nao uma
checagem feita depois de reclamar. Consequencia deliberada: dead-letter nao
precisa de status novo nem de coluna nova — a linha simplesmente para de ser
elegivel quando o contador chega ao teto, e volta a ser se alguem zerar o
contador (unico caminho de recuperacao manual, e ele nao exige tocar em mais
nada). `next_attempt_at = NULL` NAO pode ser o marcador de dead-letter: NULL ja
significa "elegivel agora" na janela abaixo, entao usa-lo para "nunca mais"
diria o oposto do pretendido. O worker ainda zera `next_attempt_at` ao mandar
para dead-letter, mas so como sinal visual de "nao ha proxima tentativa
agendada" — quem de fato segura a linha fora da fila e o contador.

RELOGIO: SEMPRE O DO BANCO, NUNCA O DO PROCESSO
-----------------------------------------------
Toda comparacao e toda escrita de `next_attempt_at` usa o relogio do BANCO
(`clock_timestamp()` no PostgreSQL), nunca `datetime.now()` do processo —
fecha R-E da v3: dois workers em maquinas com relogios levemente diferentes
concordam sobre "ja passou da hora?" apenas se perguntarem ao mesmo relogio.
`clock_timestamp()` e nao `now()`/`CURRENT_TIMESTAMP`: estes dois sao o
instante de INICIO DA TRANSACAO no PostgreSQL, entao um lote inteiro
reclamado na mesma transacao receberia todos o mesmo instante-base, e um ciclo
longo comparia contra um "agora" ja velho.

`IS_SQLITE` como no resto do repositorio (`app/query_filters.py`,
`app/routers/leads.py`): dev/CI rodam SQLite, que nao tem `clock_timestamp()`
nem `interval`. O equivalente ali e `datetime('now')`, que ja e UTC e ja sai no
mesmo formato textual que o SQLAlchemy usa para gravar `DateTime` no SQLite —
entao a comparacao lexicografica coincide com a cronologica.

Escrita: SOMENTE pelos dois workers. Nenhum fluxo da V1 conhece esta fila.
"""
import logging
from typing import Any

from sqlalchemy import and_, func, or_, text
from sqlalchemy.orm import Query, Session
from sqlalchemy.sql.elements import ClauseElement, ColumnElement

from app.database import IS_SQLITE
from app.models.handoff_op import HandoffOperation, StatusHandoff
# `_ERRO_ESTADO_LOCAL` e `_TIMEOUT_CRM` sao privados de `handoff.py` e sao
# importados assim mesmo, de proposito: cada um e a DEFINICAO UNICA do seu
# valor. Reescrever a string literal (ou o numero de segundos) aqui criaria a
# segunda copia que o docstring acima explica ser o modo de falha desta
# particao — e, no caso do timeout, o dimensionamento do lote abaixo passaria a
# depender de um numero livre para divergir do que o cliente HTTP usa de fato.
from app.v2.handoff import _ERRO_ESTADO_LOCAL, _TIMEOUT_CRM, ClasseErroCrm, TokenErroCrm

logger = logging.getLogger(__name__)

# Periodo do laco dos dois workers (secao 5 do desenho).
CICLO_SEGUNDOS = 30

# Teto de tentativas REAIS (as que de fato chamaram a rede e foram julgadas
# pela logica de negocio do CRM). Ver secao 5: 429 nao conta, e a reclamacao
# tambem nao — so o desfecho conta.
TETO_TENTATIVAS = 5

# Espacamento entre tentativas da MESMA linha. Fixo, nao exponencial: a secao 5
# diz que o adiamento da reclamacao "serve exclusivamente para espacamento/
# exclusao entre workers" — quem limita o esforco total e o teto acima, nao a
# curva. 60s contra um ciclo de 30s da ~2 ciclos de folga por tentativa, e o
# orcamento total de uma linha ate o dead-letter fica na casa dos 5 minutos.
#
# ESTE NUMERO E UMA JANELA DE EXCLUSAO, E O LOTE PRECISA CABER DENTRO DELA —
# achado da revisao de concorrencia. O adiamento e gravado na RECLAMACAO, para
# o lote inteiro de uma vez; a partir dai o worker processa as linhas UMA A UMA.
# Se processar o lote demorar mais que `BACKOFF_SEGUNDOS`, as ultimas linhas do
# lote voltam a ficar elegiveis ANTES de a primeira instancia chegar nelas, e
# uma segunda instancia as reclama e liga para o CRM em paralelo — chamada
# duplicada (idempotente pelo `event_id`, mas queimando o teto compartilhado
# abaixo em dobro e produzindo trabalho concorrente sobre a mesma linha). A
# invariante que fecha isso e `LOTE * PIOR_CASO_POR_CHAMADA <= BACKOFF`, e ela
# e verificada por teste (grupo 2 de `tests/test_v2_workers.py`).
BACKOFF_SEGUNDOS = 60

# Pior caso de UMA chamada ao CRM, em segundos: uma requisicao que gasta o
# timeout de conexao E depois o de leitura sem nunca responder. Derivado do
# PROPRIO objeto de timeout do cliente (`handoff._TIMEOUT_CRM`), nunca
# recopiado como literal — ver a nota do import. `write`/`pool` ficam de fora
# de proposito: o corpo e minusculo e cada chamada abre o seu proprio
# `httpx.Client`, entao nao ha espera de pool a somar.
PIOR_CASO_POR_CHAMADA_SEGUNDOS = (_TIMEOUT_CRM.connect or 0.0) + (_TIMEOUT_CRM.read or 0.0)

# Orcamento PROPRIO de cada worker, abaixo do teto do CRM (secao 5, correcao 1).
# O teto e GLOBAL e por IP (`200/minute`, `SlowAPIMiddleware`) e e compartilhado
# com todo o trafego Conversas->CRM.
#
# DOIS limites, e o menor manda:
#
# 1. JANELA DE EXCLUSAO (o que de fato fixa o valor hoje): cada linha custa no
#    maximo uma chamada ao CRM, entao um lote custa `LOTE * 15s`. Para caber nos
#    60s de `BACKOFF_SEGUNDOS`, `LOTE <= 4`. Fica em 3, e nao em 4, porque 4
#    daria 60s CRAVADOS: cada linha tambem faz idas ao banco (recarregar,
#    contabilizar, os tres commits de `handoff_to_human`) que sao
#    milissegundos em operacao normal mas nao sao ZERO — e um banco degradado
#    junto do CRM e o cenario mais provavel, nao o menos. 3 * 15s = 45s deixa
#    15s (25%) de folga para isso.
# 2. TETO COMPARTILHADO: ciclo de 30s = 2 ciclos/min, entao cada instancia de
#    cada worker emite no maximo `2 * LOTE` req/min. Com 3 + 3, UM par de
#    instancias faz 12 req/min = 6% do teto.
#
# A aritmetica anterior (20 + 10 = 60 req/min = 30% do teto) media UMA instancia
# so — mas rodar varias e o modo PRETENDIDO de escalar (`FOR UPDATE SKIP
# LOCKED`, ver `reclamar`), e K instancias multiplicam a taxa por K. Com os
# valores atuais, K pares fazem `K * 12` req/min: 6 pares ainda ficam em 72
# req/min (pouco mais de um terco do teto), e so ~16 pares ameacariam os
# 200/min. Sob os valores antigos, TRES pares ja consumiam 180 req/min = 90% do
# teto, deixando o trafego normal do Conversas sem espaco e tornando o 429
# exatamente o modo de falha rotineiro autoinduzido que a secao 5 manda evitar.
#
# O preco e vazao por instancia (6 linhas/min por worker em vez de 40/20): a
# resposta do desenho para backlog e escalar horizontalmente, e e justamente
# isso que estes valores tornam seguro.
LOTE_R16 = 3
LOTE_R15 = 3

# As duas fatias, lado a lado — ver "A PARTICAO, LITERAL" no docstring.
CODIGOS_EXCLUSIVOS_R15 = (TokenErroCrm.LEAD_JA_ATRIBUIDO.value, _ERRO_ESTADO_LOCAL)
CLASSES_RETOMAVEIS_R16 = (
    ClasseErroCrm.NAO_APLICADO_RETRIAVEL.value,
    ClasseErroCrm.AMBIGUO_RETRIAVEL.value,
)


def agora_do_banco() -> ColumnElement[Any]:
    """Expressao SQL do instante ATUAL segundo o banco — ver "RELOGIO" no topo."""
    if IS_SQLITE:
        return func.datetime("now")
    return func.clock_timestamp()


def instante_futuro(segundos: int) -> ClauseElement:
    """
    Expressao SQL de "agora + N segundos", pelo relogio do BANCO.

    `int(segundos)` interpolado num `text()` no ramo PostgreSQL: a coercao para
    `int` e o que torna a interpolacao segura (nao ha string de chamador
    chegando aqui), e um bind param nao serve porque `INTERVAL` nao aceita
    parametro no lugar da constante literal sem um cast explicito.

    Retorno `ClauseElement` e nao `ColumnElement`: os dois ramos devolvem tipos
    diferentes (`Function` no SQLite, `TextClause` no PostgreSQL) e `TextClause`
    NAO e um `ColumnElement` — `ClauseElement` e o supertipo comum mais preciso.
    """
    if IS_SQLITE:
        return func.datetime("now", f"+{int(segundos)} seconds")
    return text(f"clock_timestamp() + interval '{int(segundos)} seconds'")


def _janela_chegou() -> ColumnElement[bool]:
    """`next_attempt_at` no passado, ou nunca agendado (linha que ainda nao falhou)."""
    return or_(
        HandoffOperation.next_attempt_at.is_(None),
        HandoffOperation.next_attempt_at <= agora_do_banco(),
    )


def criterios_r16() -> tuple:
    """Fatia EXCLUSIVA do worker de retomada (R16) — ver "A PARTICAO, LITERAL"."""
    return (
        HandoffOperation.attempt_count < TETO_TENTATIVAS,
        or_(
            HandoffOperation.status == StatusHandoff.REQUESTED.value,
            and_(
                HandoffOperation.status == StatusHandoff.FAILED.value,
                HandoffOperation.error_class.in_(CLASSES_RETOMAVEIS_R16),
                # `IS NULL` EXPLICITO, e nao so o `NOT IN` — logica de tres
                # valores do SQL: `NULL NOT IN (...)` nao e TRUE, e NULL, que o
                # WHERE descarta como se fosse falso. Sem este ramo, uma linha
                # com `error_code IS NULL` e `error_class` retriavel ficaria
                # ORFA: fora de R16 (o `NOT IN` a rejeita) e fora de R15 (o
                # `IN` daquela fatia tambem a rejeita) — nenhum dos dois
                # workers a corrigiria e nada acusaria a ausencia. Nao e
                # alcancavel hoje (`_registrar_falha` sempre grava um codigo
                # junto de uma classe), mas a particao da secao 4.2 e uma
                # afirmacao sobre TODA linha possivel da tabela, nao sobre as
                # que um outro arquivo produz hoje.
                or_(
                    HandoffOperation.error_code.is_(None),
                    HandoffOperation.error_code.not_in(CODIGOS_EXCLUSIVOS_R15),
                ),
            ),
        ),
        _janela_chegou(),
    )


def criterios_r15() -> tuple:
    """Fatia EXCLUSIVA do worker de reconciliacao (R15) — ver "A PARTICAO, LITERAL"."""
    return (
        HandoffOperation.attempt_count < TETO_TENTATIVAS,
        HandoffOperation.status == StatusHandoff.FAILED.value,
        HandoffOperation.error_code.in_(CODIGOS_EXCLUSIVOS_R15),
        HandoffOperation.reconciled_at.is_(None),
        _janela_chegou(),
    )


def consulta_de_reclamacao(db: Session, *, criterios: tuple, limite: int) -> Query[Any]:
    """
    A SELECT da reclamacao, isolada de `reclamar` — nao por reuso (ha um
    chamador so), mas porque e a UNICA forma de PROVAR o `SKIP LOCKED` num CI
    que roda SQLite: o compilador SQLite do SQLAlchemy DESCARTA `FOR UPDATE`
    silenciosamente, entao nem executar nem inspecionar a SQL emitida em dev
    mostra a clausula. Com a consulta separada, o teste a compila contra o
    dialeto PostgreSQL e confere a clausula literalmente, sem PostgreSQL
    disponivel. Ver `tests/test_v2_workers.py`, grupo 3.
    """
    consulta = (
        db.query(HandoffOperation.id)
        .filter(*criterios)
        .order_by(
            HandoffOperation.next_attempt_at.asc().nullsfirst(),
            HandoffOperation.id.asc(),
        )
        .limit(limite)
    )
    if not IS_SQLITE:
        # SQLite ignora `FOR UPDATE` SILENCIOSAMENTE (nao levanta) — deixar a
        # clausula ali daria a impressao de uma exclusao que nao existe. O ramo
        # e o mesmo idioma de `_lock_conversation`/`_aplicar_estado_local`. Em
        # dev/CI o banco inteiro ja e serializado por lock de arquivo.
        consulta = consulta.with_for_update(skip_locked=True)
    return consulta


def reclamar(db: Session, *, criterios: tuple, limite: int) -> tuple:
    """
    TRANSACAO CURTA de reclamacao — devolve os ids que ESTE worker passou a
    ser dono nesta rodada, e commita antes de qualquer chamada de rede.

    `FOR UPDATE SKIP LOCKED`: duas instancias do MESMO worker rodando ao mesmo
    tempo nunca reclamam a mesma linha — a segunda PULA as linhas travadas pela
    primeira em vez de esperar por elas. E o que torna o worker escalavel
    horizontalmente sem lease, heartbeat nem particionamento por hash.

    SO ADIA `next_attempt_at`. NAO incrementa `attempt_count` — achado HIGH da
    revisao (secao 5): o contador e o gate de dead-letter, entao incrementa-lo
    aqui faria um crash entre a reclamacao e a chamada de rede (crash loop,
    OOM-kill, rollout) esgotar o teto de uma operacao que NUNCA foi tentada
    nenhuma vez. O adiamento sozinho ja entrega o que a reclamacao precisa
    entregar: espacamento entre tentativas e exclusao entre workers.

    `ORDER BY next_attempt_at NULLS FIRST`: linhas nunca agendadas (tipicamente
    `REQUESTED` deixada por um crash — o caso mais urgente, ver Legenda A da
    secao 6.1) vem antes das que ja estao em retry. `NULLS FIRST` e explicito
    porque o default diverge entre os dois dialetos (PostgreSQL poe NULL por
    ultimo no ASC; SQLite poe primeiro).

    `synchronize_session=False`: nada nesta sessao le os objetos depois — o
    commit encerra a transacao e cada operacao e recarregada fresca no seu
    proprio desfecho.
    """
    consulta = consulta_de_reclamacao(db, criterios=criterios, limite=limite)
    ids = tuple(linha[0] for linha in consulta.all())
    if ids:
        db.query(HandoffOperation).filter(HandoffOperation.id.in_(ids)).update(
            {"next_attempt_at": instante_futuro(BACKOFF_SEGUNDOS)},
            synchronize_session=False,
        )
    db.commit()
    return ids


def carregar(db: Session, operacao_id: int) -> HandoffOperation | None:
    """
    Recarrega a linha do banco, FRESCA. `populate_existing()` e obrigatorio:
    as escritas deste modulo usam `synchronize_session=False`, entao o identity
    map da sessao ficaria com valores obsoletos e as decisoes de desfecho
    (teto, classe, `reconciled_at`) seriam tomadas contra um retrato velho.
    """
    return (
        db.query(HandoffOperation)
        .filter(HandoffOperation.id == operacao_id)
        .populate_existing()
        .one_or_none()
    )


def contabilizar_tentativa(db: Session, operacao_id: int, *, conta: bool) -> HandoffOperation | None:
    """
    TRANSACAO DE DESFECHO — roda DEPOIS que a chamada de rede retornou (ou
    levantou), nunca antes (secao 5). Incrementa `attempt_count` e devolve a
    linha ja recarregada.

    `conta` E DECIDIDO PELO CHAMADOR, POR UM FATO EXPLICITO — nunca por
    inferencia. Uma tentativa REAL e a conjuncao de duas coisas, e cada worker
    computa as duas no ponto em que elas sao conhecidas:
      1. a operacao foi ENTREGUE a `handoff_to_human`
         (`worker_reconciliacao._Passo.tentou_rede`; o `tentou_rede` local de
         `worker_retomada._retomar_uma`). Todo early-return anterior a entrega
         vale `False` — ele nao tocou a rede, e ninguem julgou nada;
      2. a logica de negocio do CRM chegou a julga-la — o termo do 429 abaixo.
    Derivar (1) do `error_code`, como se fez ate a revisao dos workers, conta
    early-returns como tentativa (`None != "CRM_LIMITE_TAXA"` e True); deriva-lo
    de "a funcao retornou sem lancar" deixa de contar o veredito que a linha
    permanentemente ruim precisa para chegar ao dead-letter.

    `conta=False` e o caso 429/`CRM_LIMITE_TAXA`: a requisicao tocou a rede,
    mas o `SlowAPIMiddleware` a recusou ACIMA do roteamento, antes de
    `aplicar_handoff` sequer rodar — a logica de negocio jamais viu a operacao.
    Da perspectiva dela, isso e estruturalmente identico a nunca ter sido
    tentada, so descoberto uma camada mais funda; queimar orcamento de
    tentativas ali mandaria para dead-letter uma operacao nunca julgada.

    O RESULTADO em si (`status`/`error_code`/`error_class`) ja foi gravado por
    `handoff_to_human`, na transacao dele. Aqui so entra a contabilidade que e
    do worker.
    """
    if conta:
        db.query(HandoffOperation).filter(HandoffOperation.id == operacao_id).update(
            {"attempt_count": HandoffOperation.attempt_count + 1},
            synchronize_session=False,
        )
        db.commit()
    return carregar(db, operacao_id)


def enviar_para_dead_letter(db: Session, operacao: HandoffOperation) -> None:
    """
    Para de reagendar a linha. Quem de fato a segura fora da fila e
    `attempt_count >= TETO_TENTATIVAS` no predicado de elegibilidade — este
    `next_attempt_at = NULL` e o sinal legivel de "nao ha proxima tentativa
    agendada" (e, se um humano zerar o contador para reprocessar, NULL ja
    significa "elegivel agora", que e a recuperacao desejada).
    """
    db.query(HandoffOperation).filter(HandoffOperation.id == operacao.id).update(
        {"next_attempt_at": None}, synchronize_session=False
    )
    db.commit()
