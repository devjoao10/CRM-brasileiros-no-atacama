"""
BIA-V2 Fase 5 - selecao de quem recebe a proxima conversa.

`selecionar_atendente` e um ENVELOPE fino sobre `resolver_atendente_elegivel`
(V1, conversas/app/services/atendimento.py) - nao reimplementa nem altera a
regra de elegibilidade/balanceamento de carga, so a expoe para o codigo V2.

POR QUE ESTE ENVELOPE EXISTE, MESMO SENDO TAO FINO
A regra em si (menor carga primeiro, configuravel por ATENDENTES_ELEGIVEIS)
ja esta correta e testada na V1 - reimplementa-la aqui duplicaria logica que
so pode divergir com o tempo. O que faltava e uma FRONTEIRA DE CAMADA: a
partir da Fase 6 (handoff), codigo V2 importa de `app.v2.*`, nunca alcanca
direto dentro de `app.services.atendimento` da V1. Sem este modulo, o
handoff precisaria importar a V1 diretamente, misturando as duas arvores de
import. `atendimento.py` e um dos 8 arquivos protegidos por hash em
tests/test_v2_v1_intacta.py (Familia A) - este modulo nunca o modifica, so
o chama.

NENHUM ID HARDCODED. `selecionar_atendente` nao decide nada por conta
propria: devolve exatamente o que `resolver_atendente_elegivel` devolver,
incluindo `None` quando nao ha ninguem elegivel. `None` NAO e erro (ver o
docstring de `resolver_atendente_elegivel`) - decidir o que fazer com a
ausencia de atendente e responsabilidade de quem chama (Fase 6, handoff);
esta funcao so consulta e repassa.
"""
from sqlalchemy.orm import Session

from app.services.atendimento import atendentes_elegiveis, resolver_atendente_elegivel


def selecionar_atendente(db: Session) -> int | None:
    """
    Devolve o id de quem deve receber a proxima conversa, ou None se nao
    houver ninguem elegivel agora. Delega inteiramente a
    `resolver_atendente_elegivel` (V1) - ver o docstring do modulo para o
    porque deste envelope existir apesar de nao acrescentar logica.
    """
    return resolver_atendente_elegivel(db)


def selecionar_atendente_sem_aviso(db: Session) -> int | None:
    """
    MESMA selecao de `selecionar_atendente`, sem o `logger.warning` que a V1
    emite quando nao ha ninguem elegivel.

    POR QUE EXISTE - so para a SOMBRA (`app/v2/sombra.py`).
    `resolver_atendente_elegivel` loga
    "Handoff sem atendente elegivel - conversa entra na fila sem dono" quando
    a lista vem vazia. Essa frase e byte a byte a da V1 e nao carrega marcador
    nenhum: durante a janela de observacao da sombra ela produziria uma rajada
    de avisos de "conversa sem dono" para conversas que NUNCA entraram em fila
    - sinal operacional falso justamente quando alguem esta olhando o log.
    O caminho VIVO continua usando `selecionar_atendente` e continua logando:
    la o aviso e verdadeiro.

    A REGRA DE SELECAO NAO E REIMPLEMENTADA AQUI. O aviso da V1 so acontece no
    ramo da lista VAZIA, e nesse ramo o retorno e sempre `None`. Entao basta
    perguntar antes, com a consulta de nivel mais baixo (`atendentes_elegiveis`,
    o MESMO SELECT que a V1 faz primeiro), e so chamar
    `resolver_atendente_elegivel` quando ja se sabe que ha alguem - caso em que
    ela nao loga. Menor carga primeiro e id como desempate continuam morando na
    V1, chamados pela V1: nao ha copia daquela regra que possa divergir com o
    tempo. `app/services/atendimento.py` e um dos 8 arquivos protegidos por
    hash (tests/test_v2_v1_intacta.py) e NAO e tocado por isto.

    Custo: um SELECT a mais em `users` quando ha atendente elegivel. A sombra
    roda no maximo uma vez por conversa observada e essa consulta nao segura
    conexao durante espera nenhuma - ver `_soltar_conexao` em `sombra.py`.
    """
    if not atendentes_elegiveis(db):
        return None
    return resolver_atendente_elegivel(db)
