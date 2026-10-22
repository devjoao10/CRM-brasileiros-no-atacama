"""
Triagem deterministica da V2 — BIA-V2, Fase 2.

`avaliar_triagem()` responde UMA pergunta: quais campos ainda faltam para
fechar a triagem deste snapshot. Nada mais. Sem banco, sem HTTP, sem n8n,
sem CRM, sem LLM, sem relogio, sem env — funcao PURA e deterministica: a
mesma `TriageData` devolve sempre o mesmo `ResultadoTriagem`.

Sem relogio em particular (D-4/D-5/D-6 sao EXAUSTIVAS): nao existe regra de
"data no passado" aqui. `data_inicio >= date.today()` acoplaria o resultado
ao dia da execucao — a fixture que passa hoje quebraria sozinha amanha — e
mataria o determinismo que este modulo promete. Se um dia for desejada, vira
decisao de subgate propria, nao acrescimo de implementacao.

NAO MUTA `dados`. `TriageData` nao e `frozen`, entao a nao-mutacao e
disciplina desta funcao, nao garantia do modelo: um `dados.nome.strip()` de
conveniencia gravado de volta compilaria e vazaria — o mesmo objeto e
`AIInterpretationResult.extracted`, e a mutacao alcancaria fases posteriores.
`contratos.py` preserva de proposito a diferenca entre "a IA nao extraiu" e
"a IA extraiu vazio"; aqui vazio conta como ausente (D-4) SEM apagar essa
diferenca no objeto.

O MODO E GLOBAL, NAO POR DESTINO
`datas_definidas` escolhe um unico regime para a viagem inteira: `True`
exige `data_inicio`+`data_fim` de todo destino valido, `False` exige `dias`.
Modo misto (um destino com datas, outro com dias) e triagem incompleta por
construcao — o modo que vale e o global, e o que ele exige e o que se cobra.
`quantidade_dias_pretendida` existe no schema como fato extraivel e NUNCA e
emitido como campo faltante (D-1): pedi-lo ao cliente nao fecha triagem
nenhuma.

AVALIACAO SEPARADA DA EMISSAO
As funcoes `_avaliar_*` decidem O QUE falta e devolvem conjuntos; so
`_emitir_em_ordem` decide EM QUE ORDEM sai. A separacao existe por causa das
invariantes cruzadas: a soma incoerente acrescenta `total_pessoas`,
`adultos` e `criancas` de uma vez, no fim da avaliacao — se cada regra
fizesse `append` direto na lista final, a ordem de D-8 dependeria da ordem
em que as regras rodaram, e um `append` novo no lugar errado a quebraria em
silencio.
"""
from app.v2.contratos import DestinoTriagem, ResultadoTriagem, TriageData

# ---------------------------------------------------------------------------
# Vocabulario canonico — D-1 (campos base), D-2 (campos por destino) e a
# ordem de emissao de D-8. Nenhum texto do cliente entra aqui: o destino e
# enderecado por INDICE (`destinos[N].dias`), nunca por nome. Nome de destino
# vem de linguagem livre de LLM — pode repetir, pode vir vazio, nao e chave
# estavel e nao vira protocolo interno.
# ---------------------------------------------------------------------------
_ORDEM_BASE = (
    "nome",
    "email",
    "destinos",
    "total_pessoas",
    "adultos",
    "criancas",
    "datas_definidas",
)
_ORDEM_POR_DESTINO = ("data_inicio", "data_fim", "dias")

# Os tres operandos da soma `adultos + criancas == total_pessoas`.
_CAMPOS_DE_CONTAGEM = frozenset({"total_pessoas", "adultos", "criancas"})


def _texto_ausente(valor: str | None) -> bool:
    """
    D-4: alem de `None`, `""` e string so de whitespace contam como AUSENTES
    em `nome`, `email` e `destino` — carregam a mesma zero informacao. Le o
    valor, nunca o normaliza de volta no objeto.
    """
    return valor is None or valor.strip() == ""


def _avaliar_campos_base(dados: TriageData) -> set[str]:
    """Campos base (D-1) sem valor valido suficiente para fechar a triagem (D-7)."""
    faltantes: set[str] = set()

    if _texto_ausente(dados.nome):
        faltantes.add("nome")
    if _texto_ausente(dados.email):
        faltantes.add("email")

    # Decisao 3: `destinos` sai UMA unica vez, e cobre os tres casos — lista
    # vazia, todos os itens sem identidade, e lista PARCIALMENTE valida. No
    # caso parcial ele e o identificador do item sem identidade que nao foi
    # descartado; nao se cria `destinos[N].destino` para apontar o item.
    if not dados.destinos or any(_texto_ausente(destino.destino) for destino in dados.destinos):
        faltantes.add("destinos")

    # D-5: negativo, e zero onde o minimo e 1, sao INVALIDOS — e valor
    # invalido entra em `campos_faltantes` igual a valor ausente (D-7).
    # `criancas == 0` e valido.
    if dados.total_pessoas is None or dados.total_pessoas < 1:
        faltantes.add("total_pessoas")
    if dados.adultos is None or dados.adultos < 1:
        faltantes.add("adultos")
    if dados.criancas is None or dados.criancas < 0:
        faltantes.add("criancas")

    # Decisao 2: `None` e o estado da primeira mensagem, nao um `False`
    # implicito. Comparacao por `is None` — e nunca `if dados.datas_definidas:`,
    # que colapsaria `None` em `False` e escolheria o modo "dias" calado.
    if dados.datas_definidas is None:
        faltantes.add("datas_definidas")

    # D-3 (guard da invariante cruzada): a soma so e avaliada quando os TRES
    # operandos estao presentes e individualmente validos — e a checagem
    # acima garante as duas coisas, porque ausencia e invalidez caem no mesmo
    # conjunto. Sem o guard, `TriageData()` vazio faria `None + None`
    # levantar `TypeError`, e a funcao existe justamente para receber
    # parciais. Soma incoerente emite os TRES campos: nao ha informacao para
    # dizer qual esta errado, e inventar um `soma_incoerente` e proibido.
    if not (_CAMPOS_DE_CONTAGEM & faltantes) and (
        dados.adultos + dados.criancas != dados.total_pessoas
    ):
        faltantes |= _CAMPOS_DE_CONTAGEM

    return faltantes


def _avaliar_periodo(destino: DestinoTriagem, datas_definidas: bool) -> set[str]:
    """
    Campos de periodo que faltam NESTE destino, ja sob a precedencia do modo
    global (Decisao 1). O campo que o modo nao exige e irrelevante — nunca e
    necessario para completar a triagem, entao nunca e emitido. Irrelevante
    NAO significa normalizado nem removido: ele continua intacto em
    `TriageData`, so nao participa da decisao neste modo.
    """
    if datas_definidas is False:
        # Modo "dias": as datas sao irrelevantes — ausentes, incompletas ou
        # cronologicamente invertidas nao emitem nada. `dias > 0` (D-5).
        if destino.dias is None or destino.dias <= 0:
            return {"dias"}
        return set()

    # Modo "datas": `dias` e irrelevante — ausente, `0` ou negativo nao emite.
    faltantes: set[str] = set()
    if destino.data_inicio is None:
        faltantes.add("data_inicio")
    if destino.data_fim is None:
        faltantes.add("data_fim")

    # D-3 + D-6: a cronologia so e avaliada com AS DUAS datas presentes; com
    # uma faltando, emite-se so a que falta. `date` nao tem regra propria em
    # D-5/D-6, entao "individualmente valido" aqui se reduz a presenca.
    # `data_inicio == data_fim` e valido (o passeio de um dia); so
    # `data_fim < data_inicio` e invalido, e emite AS DUAS (D-7) — nao se
    # sabe qual das duas o cliente informou errado.
    if not faltantes and destino.data_fim < destino.data_inicio:
        return {"data_inicio", "data_fim"}

    return faltantes


def _avaliar_destinos(dados: TriageData) -> list[tuple[int, set[str]]]:
    """
    Problemas por destino, como pares `(indice, campos)` na ordem da lista
    recebida. `indice` e a posicao REAL em `dados.destinos` (D-2).
    """
    # Decisao 2: sem modo global definido, nenhum `destinos[N].*` e emitido —
    # o motor ainda nao sabe qual conjunto de campos e requerido. Nao se
    # exigem datas e dias ao mesmo tempo, e nao se adivinha o modo a partir
    # do que ja esta preenchido.
    if dados.datas_definidas is None:
        return []

    problemas: list[tuple[int, set[str]]] = []
    for indice, destino in enumerate(dados.destinos):
        # Decisao 3: sem identidade de destino nao ha o que perguntar sobre o
        # periodo dele. O item nao e descartado nem reindexado — `enumerate`
        # sobre a lista recebida preserva o indice dos itens seguintes.
        if _texto_ausente(destino.destino):
            continue
        campos = _avaliar_periodo(destino, dados.datas_definidas)
        if campos:
            problemas.append((indice, campos))
    return problemas


def _emitir_em_ordem(
    faltantes_base: set[str], problemas_destinos: list[tuple[int, set[str]]]
) -> list[str]:
    """
    D-8: ordem deterministica e sem duplicatas. Campos base primeiro, na
    ordem de `_ORDEM_BASE`; depois os problemas por destino, na ordem da
    lista, e dentro de cada destino na ordem de `_ORDEM_POR_DESTINO`. A
    ordem sai daqui, das constantes — nunca da ordem em que as regras
    rodaram. Cada identificador aparece no maximo uma vez porque cada
    conjunto de origem o contem no maximo uma vez.
    """
    campos = [campo for campo in _ORDEM_BASE if campo in faltantes_base]
    for indice, faltantes in problemas_destinos:
        campos.extend(
            f"destinos[{indice}].{campo}" for campo in _ORDEM_POR_DESTINO if campo in faltantes
        )
    return campos


def avaliar_triagem(dados: TriageData) -> ResultadoTriagem:
    """
    Avalia a completude de um snapshot de triagem. Pura: nao toca banco, IO
    nem relogio, e NAO muta `dados`.
    """
    campos_faltantes = _emitir_em_ordem(_avaliar_campos_base(dados), _avaliar_destinos(dados))
    # D-9: `campos_faltantes` e a UNICA fonte de verdade; `completa` e
    # derivado dela, aqui, na construcao. Calcular completude por um caminho
    # independente e depois sincronizar os dois e exatamente o padrao que
    # produziu o `pronto_para_humano`.
    return ResultadoTriagem(completa=len(campos_faltantes) == 0, campos_faltantes=campos_faltantes)
