"""
Output guard da V2 - BIA-V2, Fase 9.

`filtrar_saida(texto, *, acao)` decide se um texto pronto para envio pode
sair. Funcao PURA, DETERMINISTICA, sem LLM, sem IO - mesmo espirito de
`prefiltro.py` (Fase 3) e `estados.py` (Fase 4): allowlist/regras explicitas,
nunca heuristica vaga. Duas responsabilidades independentes, nesta ordem:

  1. `acao` precisa estar em `ACOES_PERMITIDAS` - "o sistema escolheu O QUE",
     e este modulo e quem cobra essa autorizacao.
  2. `texto` nao pode conter valor monetario - simbolo (R$), codigo de moeda
     (USD/BRL/EUR) ou numero (algarismo OU por extenso) seguido de palavra de
     moeda (real/reais/dolar/dolares/euro/euros).

FAIL-CLOSED, AO CONTRARIO DO PRE-FILTRO (decisao desta implementacao)
`prefiltro.py` e fail-OPEN de proposito: na duvida, a mensagem do CLIENTE
continua (o custo de um falso positivo la e cliente ignorado em silencio).
Aqui a logica se inverte: o dado sensivel e um valor monetario que o sistema
decidiu nao cotar por este canal, e o custo de um falso NEGATIVO (deixar
vazar) e maior que o custo de um falso positivo (recusar um texto legitimo
que por coincidencia parece dinheiro). Por isso o scanner por extenso abaixo
e deliberadamente mais generoso que o minimo do plano (ve o bloco "DECISOES
DOCUMENTADAS" abaixo) - cobrir mais formas de "numero-vira-moeda" e o lado
seguro do erro aqui.

O GATILHO E SEMPRE NUMERO+MOEDA, NUNCA NUMERO SOZINHO
"temos 5000 clientes" ou "a viagem tem 5 pessoas" nao podem ser bloqueados -
o numero sozinho nao significa nada. Estruturalmente garantido: os tres
detectores abaixo (simbolo, codigo, extenso-ou-digito) SO disparam quando uma
palavra de moeda (ou o simbolo/codigo) esta adjacente ao numero. Uma palavra
de moeda ISOLADA, sem numero do lado, tambem nao dispara nada ("Aceitamos
reais e dolares" passa livre) - o vocabulario de moeda so importa quando
alguem verifica o que vem DEPOIS de um numero, nunca por presenca solta no
texto.

ACOES_PERMITIDAS - VOCABULARIO PROVISORIO, PROVENANCIA DECLARADA
`AuthorizedResponse` (o contrato de entrada desta fase) foi DEFERIDO na Fase
1 porque `allowed_action` aparece UMA UNICA VEZ em todo o corpus, em prosa,
sem vocabulario nenhum (docs/superpowers/plans/2026-08-29-bia-v2.md, linha
371: "allowed_action aparece uma vez, em prosa, sem vocabulario"; confirmado
por grep - zero outras ocorrencias). Este modulo PRECISA de uma allowlist
concreta para recusar "acao nao autorizada" de forma testavel (teste
obrigatorio da Fase 9), entao a decisao desta implementacao e: os DOIS
membros abaixo vem literalmente das DUAS unicas coisas que a propria frase da
Fase 9 descreve como entrada - "fatos confirmados" e "campos faltantes"
(linha 1969: "Entrada: allowed_action + fatos confirmados + campos
faltantes"). Nenhum termo de negocio adicional (handoff, encerramento, etc.)
foi inventado aqui - esses pertencem as fases donas deles (5, 6), com o
vocabulario delas. Expandir este conjunto e decisao explicita de quem rodar
o subgate de contrato que a Fase 1 registrou como pendente para
`AuthorizedResponse` - este modulo nao reivindica ser esse subgate.

DECISOES DOCUMENTADAS DO SCANNER POR EXTENSO

  - Vocabulario BASE: unidades, dez-a-dezenove, dezenas, "mil", "milhao(oes)"
    - exatamente as categorias que o plano cita (linha 1970). CENTENAS
      (cem/cento/duzentos/.../novecentos) foram ACRESCENTADAS: sem elas
      "quinhentos reais" (uma frase corriqueira) passaria intacta, e o
      vocabulario de "milhao" citado pelo plano so aparece na fala real
      como "um MILHAO DE reais" (ver proximo item) - ficar estrito demais
      aqui abriria exatamente o vazamento que esta fase existe para fechar.
      Nao ha forma feminina de centena (duzentaS, trezentaS...): "real",
      "dolar" e "euro" sao substantivos MASCULINOS em portugues, a forma
      feminina nunca antecede nenhum deles - omissao deliberada, nao lacuna.

  - CONECTOR ADITIVO "e" (vinte E cinco, cento E vinte): aceito SOMENTE
    quando ladeado por token numerico dos dois lados - "e" sozinho (a
    palavra mais comum do portugues) nunca e tratado como sinal por si so.

  - CASO DE FRONTEIRA #1 (arriscado, documentado de proposito): CONECTOR
    PARTITIVO "de" entre ESCALA (mil/milhao/milhoes) e a palavra de moeda -
    "um milhao DE reais". Sem esta regra extra, o vocabulario "milhao" que
    o plano exige seria pratica morta: ninguem fala "um milhao reais" em
    portugues. A regra e ESTREITA de proposito - "de" so conta nesta unica
    adjacencia (escala -> de -> moeda), nunca como conector generico entre
    dois numerais quaisquer (isso incluiria frases sem nenhuma relacao com
    dinheiro, ex.: contagens partitivas genericas).

  - CASO DE FRONTEIRA #2 (arriscado, verificado seguro): a palavra "real" e
    AMBIGUA em portugues (moeda vs. adjetivo "genuino/verdadeiro, ex.:
    "um problema real"). A gramatica protege este scanner: o adjetivo nunca
    fica colado a um numeral cardinal ("trinta motivos reais" tem o
    substantivo "motivos" entre os dois) - o teste correspondente prova que
    essa frase passa livre porque o token IMEDIATAMENTE apos o numero
    ("motivos") nao e palavra de moeda. O risco so existiria se alguem
    escrevesse um numeral colado a "real"-adjetivo, construcao que o
    portugues nao forma.

  - Acentos sao NORMALIZADOS antes de qualquer comparacao ("dolar"/"dólar",
    "milhao"/"milhão" comparam iguais) - cliente de WhatsApp digita sem
    acento com frequencia, e perder esse caso seria vazamento por
    formatacao, nao por conteudo.

QUANDO BLOQUEIA: `texto_final=""` (NUNCA o texto original, nunca o valor
detectado). Decisao deliberada - `texto_final` ecoando qualquer parte do
texto recusado tornaria o bloqueio decorativo (o dado vazaria de qualquer
jeito pra frente). `motivo` e um token FECHADO e interno (nunca interpola
`texto` nem `acao` recebidos) - mesmo padrao de `TransicaoInvalida` em
estados.py: "motivo nunca aparece em mensagem nenhuma, sob nenhum token".

ORDEM: `acao` e checado ANTES do texto. Uma acao nao autorizada bloqueia
INDEPENDENTE do conteudo (mesmo um texto inofensivo e recusado) - e o
comportamento que o teste da Fase 9 exige literalmente ("acao nao autorizada
bloqueada", sem condicionar a nenhum texto especifico).
"""
import re
import unicodedata

from app.v2.contratos import ResultadoGuard

# ---------------------------------------------------------------------------
# Vocabulario de acoes permitidas - ver "ACOES_PERMITIDAS" no docstring do
# modulo para a provenancia (as duas UNICAS coisas que a Fase 9 descreve como
# entrada: fatos confirmados e campos faltantes).
# ---------------------------------------------------------------------------
ACOES_PERMITIDAS: frozenset[str] = frozenset({
    "informar_fatos_confirmados",
    "solicitar_campos_faltantes",
})

# ---------------------------------------------------------------------------
# Vocabulario FECHADO de motivo - dois tokens, nunca interpolam dado
# recebido (nem `texto`, nem `acao`, nem o valor monetario detectado).
# ---------------------------------------------------------------------------
_MOTIVO_ACAO_NAO_AUTORIZADA = "acao_nao_autorizada"
_MOTIVO_VALOR_MONETARIO = "valor_monetario_detectado"

# Ver "QUANDO BLOQUEIA" no docstring do modulo - nunca o texto original.
_TEXTO_BLOQUEADO = ""


# ---------------------------------------------------------------------------
# Detector 1 - SIMBOLO. "R$" (case-insensitive, raro mas barato de cobrir)
# seguido de opcional espaco e um digito - cobre "R$ 5.000", "R$5000" e
# "R$ 5.000,00" com o mesmo padrao (separador de milhar/decimal em pt-BR usa
# "." e "," livremente, entao a classe [\d.,]* aceita ambos sem precisar
# decidir qual e qual - a distincao nao importa para "isto e dinheiro?").
# ---------------------------------------------------------------------------
_RE_SIMBOLO_REAL = re.compile(r"r\$\s*\d[\d.,]*", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Detector 2 - CODIGO DE MOEDA. Bidirecional ("USD 300" e "300 USD"),
# case-insensitive. `\b` dos dois lados evita casar "EUR" dentro de uma
# palavra maior (ex.: nomes proprios) - so o codigo isolado conta.
# ---------------------------------------------------------------------------
_CODIGOS_MOEDA_PERMITIDOS: frozenset[str] = frozenset({"USD", "BRL", "EUR"})
_ALTERNATIVAS_CODIGO = "|".join(sorted(_CODIGOS_MOEDA_PERMITIDOS))
_RE_CODIGO_MOEDA = re.compile(
    rf"\b(?:{_ALTERNATIVAS_CODIGO})\b\s*\d[\d.,]*"
    rf"|\b\d[\d.,]*\s*\b(?:{_ALTERNATIVAS_CODIGO})\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Detector 3 - EXTENSO OU DIGITO SEGUIDO DE PALAVRA DE MOEDA. Ver "DECISOES
# DOCUMENTADAS DO SCANNER POR EXTENSO" no docstring do modulo para a
# provenancia de cada conjunto abaixo.
# ---------------------------------------------------------------------------
_UNIDADES = frozenset({
    "zero", "um", "uma", "dois", "duas", "tres", "quatro", "cinco", "seis",
    "sete", "oito", "nove",
})
_DEZ_A_DEZENOVE = frozenset({
    "dez", "onze", "doze", "treze", "catorze", "quatorze", "quinze",
    "dezesseis", "dezessete", "dezoito", "dezenove",
})
_DEZENAS = frozenset({
    "vinte", "trinta", "quarenta", "cinquenta", "sessenta", "setenta",
    "oitenta", "noventa",
})
# Acrescentado alem do minimo do plano - ver docstring do modulo.
_CENTENAS = frozenset({
    "cem", "cento", "duzentos", "trezentos", "quatrocentos", "quinhentos",
    "seiscentos", "setecentos", "oitocentos", "novecentos",
})
_ESCALAS = frozenset({"mil", "milhao", "milhoes"})

_PALAVRAS_NUMERO: frozenset[str] = _UNIDADES | _DEZ_A_DEZENOVE | _DEZENAS | _CENTENAS | _ESCALAS

_PALAVRAS_MOEDA: frozenset[str] = frozenset({"real", "reais", "dolar", "dolares", "euro", "euros"})

_CONECTOR_ADITIVO = "e"     # "vinte E cinco" - so entre dois numericos.
_CONECTOR_PARTITIVO = "de"  # "um milhao DE reais" - so entre escala e moeda.

_RE_TOKEN_DIGITO = re.compile(r"^\d[\d.,]*$")

# Pontuacao de BORDA removida de cada token - nunca pontuacao interna (o "."
# de "5.000" ou "," de "5,00" precisam sobreviver para o regex de digito
# acima reconhecer o numero inteiro). Inclui os marcadores de markdown que o
# WhatsApp reconhece nativamente - "*negrito*", "_italico_", "~riscado~" - e
# colchetes: a resposta da propria IA (Gemini), formatada pra WhatsApp, usa
# essa sintaxe rotineiramente, entao "*5000*" ou "[5000]" tem que virar
# "5000" apos o strip - senao o token inteiro (com os marcadores colados)
# nunca bate no regex de digito puro (`_RE_TOKEN_DIGITO`) nem e reconhecido
# como palavra de moeda. Achado da segunda revisao de seguranca (python-
# reviewer, rodando DEPOIS da correcao dos invisiveis Cf acima) - ver
# tests/test_v2_output_guard.py, casos L*.
_BORDA_PONTUACAO = ".,!?;:()\"'‘’“”*_~[]"


def _sem_acentos(palavra: str) -> str:
    """Remove marcas de acento via decomposicao NFKD - 'dólar' e 'dolar'
    comparam iguais, mesmo criterio para 'milhão'/'milhao'. Ver "Acentos sao
    NORMALIZADOS" no docstring do modulo."""
    forma_decomposta = unicodedata.normalize("NFKD", palavra)
    return "".join(c for c in forma_decomposta if not unicodedata.combining(c))


def _normalizar_token(token: str) -> str:
    """Minusculo + sem acento. Aplicado a TODO token antes de qualquer
    comparacao - digito puro atravessa sem alteracao (nenhum digito tem
    acento nem maiuscula)."""
    return _sem_acentos(token).lower()


def _tokenizar(texto: str) -> list[str]:
    """Divide por espaco e remove pontuacao de BORDA de cada token (nunca
    interna - ver `_BORDA_PONTUACAO`). Tokens que viram string vazia (ex.:
    um token que era so pontuacao) sao descartados.

    Espera receber o texto ja passado por `_substituir_invisiveis_por_espaco`
    (feito em `_contem_valor_monetario`, nao aqui) - `texto.split()` so
    reconhece espaco de verdade, entao um caractere Cf (zero-width space
    etc.) ainda cru colaria "5000<ZWSP>reais" num unico token, invisivel
    para este detector."""
    brutos = (bruto.strip(_BORDA_PONTUACAO) for bruto in texto.split())
    return [_normalizar_token(bruto) for bruto in brutos if bruto]


def _eh_numerico(token_normalizado: str) -> bool:
    """True se o token (ja normalizado) for um numero em algarismos OU uma
    das palavras-numero da allowlist (`_PALAVRAS_NUMERO`)."""
    if _RE_TOKEN_DIGITO.fullmatch(token_normalizado):
        return True
    return token_normalizado in _PALAVRAS_NUMERO


def _fim_do_run_numerico(tokens: list[str], inicio: int) -> int:
    """
    A partir de `tokens[inicio]` (ja sabido numerico), avanca o maior run
    contiguo de tokens numericos, aceitando o CONECTOR ADITIVO ("e") somente
    quando ha um token numerico dos dois lados dele. Devolve o indice do
    ULTIMO token numerico do run (nunca o indice de um conector).

    ITERATIVO (while, nao recursao) - mesmo motivo do scanner de emoji em
    prefiltro.py: um run adversarial de milhares de tokens nao pode depender
    da pilha de chamadas do Python.
    """
    fim = inicio
    j = inicio + 1
    total = len(tokens)
    while j < total:
        if _eh_numerico(tokens[j]):
            fim = j
            j += 1
        elif tokens[j] == _CONECTOR_ADITIVO and j + 1 < total and _eh_numerico(tokens[j + 1]):
            j += 1  # consome o conector; o token numerico seguinte estende o run na proxima iteracao.
        else:
            break
    return fim


def _run_seguido_de_moeda(tokens: list[str], fim_run: int) -> bool:
    """
    True se o token logo apos o run numerico (que termina em `fim_run`) for
    uma palavra de moeda - OU se for o CASO DE FRONTEIRA #1 documentado no
    modulo: run termina em ESCALA (mil/milhao/milhoes), seguido do conector
    PARTITIVO "de" e entao a palavra de moeda ("um milhao DE reais").
    """
    total = len(tokens)
    proximo = fim_run + 1
    if proximo < total and tokens[proximo] in _PALAVRAS_MOEDA:
        return True
    if (
        tokens[fim_run] in _ESCALAS
        and proximo < total
        and tokens[proximo] == _CONECTOR_PARTITIVO
        and proximo + 1 < total
        and tokens[proximo + 1] in _PALAVRAS_MOEDA
    ):
        return True
    return False


def _valor_por_extenso_ou_digito_com_moeda(texto: str) -> bool:
    """
    Varredura de indice, esquerda para direita (mesmo estilo do scanner de
    emoji de prefiltro.py): a cada token numerico encontrado, estende o run
    e verifica se uma palavra de moeda vem logo depois. QUALQUER token
    numerico sem moeda adjacente e inofensivo - e assim que "5000 clientes"
    e "5 pessoas" atravessam sem bloquear (ver "O GATILHO E SEMPRE
    NUMERO+MOEDA" no docstring do modulo).
    """
    tokens = _tokenizar(texto)
    total = len(tokens)
    i = 0
    while i < total:
        if not _eh_numerico(tokens[i]):
            i += 1
            continue
        fim_run = _fim_do_run_numerico(tokens, i)
        if _run_seguido_de_moeda(tokens, fim_run):
            return True
        i = fim_run + 1
    return False


def _remover_invisiveis(texto: str) -> str:
    """Remove (sem substituir) caracteres Unicode de categoria Cf
    (zero-width space, soft hyphen, word joiner, BOM, ZWNJ/ZWJ etc.). Correto
    SO para o detector 1 (simbolo): `_RE_SIMBOLO_REAL` casa "r\\$" literal
    (sem `\\s*` entre as duas), entao um invisivel bem ALI so pode ser
    removido, nunca virar espaco, ou quebraria o proprio "R$" colado que o
    padrao exige; e o `\\s*` que already existe entre "$" e o digito ja
    tolera zero espacos, entao remover funciona nos dois pontos do padrao.
    NAO usar para os detectores 2/3 - ver `_substituir_invisiveis_por_espaco`."""
    return "".join(c for c in texto if unicodedata.category(c) != "Cf")


# ---------------------------------------------------------------------------
# Achado da segunda revisao de seguranca (python-reviewer, apos a correcao
# dos invisiveis Cf acima): digito colado DIRETO numa letra - sem espaco, sem
# pontuacao, sem invisivel nenhum no meio - atravessa tanto o Detector 2
# quanto o Detector 3. Nao e um ataque exotico: e a formatacao normal que a
# propria IA (Gemini) gera pro WhatsApp ("5000reais", "300USD"). Ver detalhe
# de cada mecanismo na docstring da funcao abaixo.
# ---------------------------------------------------------------------------
_RE_FRONTEIRA_DIGITO_LETRA = re.compile(r"(?<=\d)(?=[^\W\d_])|(?<=[^\W\d_])(?=\d)")


def _inserir_espaco_fronteira_digito_letra(texto: str) -> str:
    """Insere um espaco - sem consumir nenhum caractere, as duas alternativas
    do padrao sao lookaround puro - em toda fronteira onde um digito encosta
    direto numa letra, nos dois sentidos: "5000reais" -> "5000 reais",
    "5mil" -> "5 mil", "300USD" -> "300 USD", "USD300" -> "USD 300".

    Necessaria para os detectores 2 e 3:
      - `_RE_CODIGO_MOEDA` usa `\\b` dos dois lados do codigo, mas `\\b` so
        marca transicao \\w/nao-\\w - digito e letra sao os DOIS \\w, entao
        nao ha fronteira nenhuma dentro de "300USD" pro `\\b` marcar.
      - `_tokenizar` so corta em espaco de verdade (`texto.split()`); sem
        este passo "5000reais" e um unico token, que nao bate nem no regex
        de numero puro (`_RE_TOKEN_DIGITO`) nem em nenhuma palavra de moeda.

    `[^\\W\\d_]` (letra) e Unicode-aware no `re` do Python 3 - "dolar" e
    "dólar" fazem fronteira igual - entao rodar isto ANTES de `_sem_acentos`
    (que so age token a token, dentro de `_tokenizar`) e seguro: o acento em
    si nunca decide SE ha fronteira aqui, so decide (depois, ja com o token
    separado) se aquela palavra e ou nao "dolar".

    So digito<->letra conta como fronteira - um simbolo entre os dois (ex.:
    "R$5000", onde "$" nao e letra nem digito) nao e afetado, e texto ja
    espacado tambem nao muda (lookaround so casa onde NAO ha nada entre os
    dois caracteres). Achado da segunda revisao de seguranca (python-
    reviewer) - ver tests/test_v2_output_guard.py, casos L*."""
    return _RE_FRONTEIRA_DIGITO_LETRA.sub(" ", texto)


def _substituir_invisiveis_por_espaco(texto: str) -> str:
    """Troca (nao remove) caracteres Cf por espaco. Correto para os
    detectores 2 (codigo de moeda) e 3 (extenso/tokenizado): os dois dependem
    de uma fronteira REAL entre dois trechos alfanumericos - o `\\b` de
    `_RE_CODIGO_MOEDA` nao marca posicao nenhuma entre "USD" e um digito
    colados (letra e digito sao os dois \\w, `\\b` so marca transicao
    \\w/nao-\\w), e `_tokenizar` so corta em espaco de verdade. Remover em vez
    de substituir colaria "USD"+"300" ou "5000"+"reais" num unico token sem
    fronteira nenhuma - pior que nao normalizar, porque cala o achado sem
    corrigi-lo."""
    return "".join(" " if unicodedata.category(c) == "Cf" else c for c in texto)


def _contem_valor_monetario(texto: str) -> bool:
    """Combina os tres detectores (OR curto-circuito) - qualquer um bastando
    para considerar o texto como portador de valor monetario. Cada detector
    recebe a normalizacao de invisiveis Unicode (categoria Cf) que ele
    realmente precisa - remocao para o 1, substituicao por espaco para 2 e 3
    - nunca a mesma para os tres (ver `_remover_invisiveis` e
    `_substituir_invisiveis_por_espaco` para o porque de cada escolha; achado
    de revisao de seguranca, provado em tests/test_v2_output_guard.py,
    casos K1-K5). Os detectores 2 e 3 recebem AINDA um segundo passo de
    normalizacao, so deles: `_inserir_espaco_fronteira_digito_letra` separa
    digito colado em letra (ver docstring da funcao) - segundo achado de
    revisao de seguranca, casos L*."""
    if _RE_SIMBOLO_REAL.search(_remover_invisiveis(texto)):
        return True
    texto_espacado = _inserir_espaco_fronteira_digito_letra(_substituir_invisiveis_por_espaco(texto))
    if _RE_CODIGO_MOEDA.search(texto_espacado):
        return True
    return _valor_por_extenso_ou_digito_com_moeda(texto_espacado)


def filtrar_saida(texto: str, *, acao: str) -> ResultadoGuard:
    """
    Decide se `texto` pode sair, para a `acao` informada. Pura: nenhum
    banco, IO, relogio ou chamada de LLM - ver docstring do modulo.

    ORDEM (decisao documentada no modulo): `acao` e checada ANTES do texto.
    Acao fora de `ACOES_PERMITIDAS` bloqueia MESMO que o texto nao tenha
    nenhum valor monetario - e o comportamento literal exigido pela Fase 9.

    Quando permite: `texto_final` e o `texto` recebido, SEM NENHUMA
    modificacao - este guard aprova ou recusa, nunca reescreve o texto.
    """
    if acao not in ACOES_PERMITIDAS:
        return ResultadoGuard(permitido=False, motivo=_MOTIVO_ACAO_NAO_AUTORIZADA, texto_final=_TEXTO_BLOQUEADO)
    if _contem_valor_monetario(texto):
        return ResultadoGuard(permitido=False, motivo=_MOTIVO_VALOR_MONETARIO, texto_final=_TEXTO_BLOQUEADO)
    return ResultadoGuard(permitido=True, motivo=None, texto_final=texto)
