"""
Pre-filtro deterministico da V2 — BIA-V2, Fase 3.

`avaliar_prefiltro()` responde UMA pergunta: este LOTE de mensagens pendentes
pode ser descartado sem chamar a IA? Nada mais. Sem banco, sem HTTP, sem n8n,
sem eventos, sem relogio, sem filesystem — funcao PURA e deterministica: a
mesma lista de `MensagemLote` devolve sempre a mesma `DecisaoPrefiltro`.

FAIL-OPEN E A REGRA CENTRAL (P-3)
O pre-filtro elimina SOMENTE o que e deterministicamente seguro descartar. A
assimetria de custo justifica: falso negativo (ignoravel vai a IA) custa uma
chamada; falso positivo (legitima e descartada) e cliente ignorado em
silencio, sem IA e sem humano. Na duvida, CONTINUA. Vale para tipo
desconhecido, midia, `interactive`, e qualquer sequencia Unicode que o
scanner de emoji nao reconheca com seguranca.

CLASSIFICACAO E POR `msg_type`, NUNCA POR STRING-MATCH NO CONTEUDO (P-4)
`msg_type == "reaction"` e `== "sticker"` sao ignoraveis; `== "text"` aplica
texto-vazio (P-5) e o scanner emoji-only (P-6); QUALQUER OUTRO tipo —
midia, `interactive`, tipo futuro da Meta — e substantivo. A comparacao e
EXATA (`==`), sem `strip()`, `lower()` nem normalizacao Unicode: "TEXT" ou
" reaction " caem em "qualquer outro" e continuam, que e o lado que P-3
exige. Nunca `content.startswith("Reacao:")` nem `content == "Sticker"`: um
cliente que digite literalmente esse texto com `msg_type="text"` precisa
continuar, e so a fonte (`msg_type`) garante isso.

TEXTO VAZIO SO PARA `text` (P-5)
`content.strip() == ""` e ignoravel apenas quando `msg_type == "text"`. Para
qualquer outro tipo, mesmo `content==""` e substantivo (P-3): e o fato normal
de midia, e nao existe regra de completude de conteudo fora de texto.

SCANNER EMOJI-ONLY — ALLOWLIST, NUNCA FAIXA DE BLOCO (P-6)
Avaliado so para `msg_type == "text"` com conteudo nao-vazio. A allowlist
tem EXATAMENTE nove codepoints-base — nada de faixa `U+1F000`-`U+1FAFF` ou
similar: esses blocos contem `check`/`ballot x` usados como resposta
sim/nao, e as *squared latin letters* com as quais "NAO" se escreve em tres
caracteres supostamente-emoji. Faixa por bloco classificaria "NAO" como
emoji-only e descartaria uma negativa do cliente — o oposto de conservador.

O reconhecedor e um SCANNER de indice, esquerda->direita, NUNCA regex com
quantificador aninhado: a gramatica de uma sequencia (`base
(modifier|VS16)? (ZWJ base (modifier|VS16)?)*`) e backtracking catastrofico
sobre entrada do cliente se expressa por regex. A cadeia por ZWJ e
percorrida por um LACO, nunca por recursao — uma mensagem com milhares de
bases ligadas por ZWJ nao pode depender do limite de recursao do Python.

Bandeira e o PAR EXATO `U+1F1E7 U+1F1F7` (🇧🇷) e so ele — nunca faixa de
regional indicator, que aceitaria os 676 pares possiveis a partir de uma
unica fixture (e `U+1F1F4 U+1F1F0` renderiza as letras "OK").

Nao existe producao de keycap: `U+20E3` nao e consumido por nenhuma
producao, e digito/`#`/`*` nao estao na allowlist de bases — qualquer
sequencia de keycap cai em "nao reconhecida" e o lote continua. Decidido em
2026-09-02: keycap codifica quantidade de pessoas, ano e orcamento, e
ignora-lo seria descarte silencioso de um numero que o cliente escreveu.

SEMANTICA DO LOTE (P-7)
A funcao decide o LOTE INTEIRO, nunca um item isolado: lote vazio ignora;
existindo ao menos um item substantivo, o lote inteiro continua; sendo
todos ignoraveis, o lote inteiro ignora. Nao filtra, nao devolve lote
transformado, nao remove, nao reordena, nao muta nenhum item — o lote
pertence a quem chama.

MOTIVO: UM UNICO VALOR, SEM INTERPOLACAO (P-8)
`ignorar=True` <=> `motivo="lote_sem_conteudo_substantivo"`;
`ignorar=False` <=> `motivo=None`. E o UNICO valor nao-nulo que esta fase
produz. Nenhum dado do cliente — `content`, `msg_type`, telefone, e-mail —
e interpolado no motivo, mesmo quando o lote inteiro e ignoravel.

SEM EVENTO NESTA FUNCAO (P-9)
Esta funcao nao importa `eventos.py`, nao chama `registrar_evento`, nao usa
banco. A decisao vira evento depois, no orquestrador que a consumir —
transformar `motivo` em `action`/`error_code` sem passar pela allowlist de
paylod daquele modulo seria o caminho de menor resistencia, e o inseguro.

FRONTEIRA — PRE-CONDICAO APROVADA PARA QUEM INTEGRAR ESTE MODULO
Isto e DOCUMENTACAO da fronteira que a integracao futura precisa respeitar.
Nao implementa integracao nenhuma, e `webhook.py` (V1) permanece intocado.

  1. A lista de `MensagemLote` precisa ser TOTAL: um item para CADA
     mensagem pendente do lote real, nunca um subconjunto.
  2. Falha de construcao de um item na integracao e FAIL-OPEN: o item
     PRECISA entrar como substantivo. Um `try/except` que descarta o item
     malformado encolheria o lote — possivelmente ate `[]` — e o cliente
     seria ignorado sem que nenhuma regra desta funcao tivesse sido violada.
  3. `msg_type` precisa ser PRESERVADO ate chegar aqui. Tipo ausente ou nao
     confiavel e DESCONHECIDO — nunca presumido `"text"`, `"reaction"` nem
     `"sticker"`. Presumir `"text"` inverteria P-3: e o UNICO ramo em que
     conteudo vazio significa ignorar.
"""
from pydantic import BaseModel, ConfigDict, StrictStr

from app.v2.contratos import DecisaoPrefiltro

# ---------------------------------------------------------------------------
# P-8: unico motivo nao-nulo que esta fase produz. Nao criar segundo valor,
# nao reaproveitar vocabulario do n8n (`reacao_emoji_isolada` e V1 e nao
# vincula), nao ressuscitar o `MotivoPrefiltro` removido em `8580df3`.
# ---------------------------------------------------------------------------
_MOTIVO_LOTE_SEM_CONTEUDO = "lote_sem_conteudo_substantivo"


# ---------------------------------------------------------------------------
# P-4: os dois unicos `msg_type` ignoraveis independente do `content`.
# Comparacao por pertencimento em frozenset e comparacao EXATA (`==`/hash),
# sem strip/lower/normalizacao — a mesma garantia de um `or` de `==`.
# ---------------------------------------------------------------------------
_TIPOS_SEMPRE_IGNORAVEIS = frozenset({"reaction", "sticker"})


# ---------------------------------------------------------------------------
# P-6: allowlist de CODEPOINTS-BASE, exatamente estes nove. Semeada pelas
# fixtures aprovadas e so por elas — acrescentar exige fixture nova, nunca
# ampliacao por faixa de bloco Unicode (ver o motivo no docstring do modulo).
# ---------------------------------------------------------------------------
_BASES_PERMITIDAS: frozenset[int] = frozenset({
    0x1F44D,  # THUMBS UP SIGN
    0x2764,   # HEAVY BLACK HEART
    0x1F64F,  # PERSON WITH FOLDED HANDS
    0x2705,   # WHITE HEAVY CHECK MARK
    0x1F468,  # MAN
    0x1F469,  # WOMAN
    0x1F467,  # GIRL
    0x1F466,  # BOY
    0x2695,   # STAFF OF AESCULAPIUS
})

# Componente OPCIONAL de uma base: no maximo UM, modifier de tom de pele OU
# VS16 — nunca os dois na mesma base (ver o caso "dois modifiers" do teste).
_MODIFICADOR_MIN = 0x1F3FB  # EMOJI MODIFIER FITZPATRICK TYPE-1-2
_MODIFICADOR_MAX = 0x1F3FF  # EMOJI MODIFIER FITZPATRICK TYPE-6
_VS16 = "️"            # VARIATION SELECTOR-16
_ZWJ = "‍"             # ZERO WIDTH JOINER

# Bandeira: o PAR EXATO allowlistado, nunca faixa de regional indicator.
_BANDEIRA_PERMITIDA = "\U0001F1E7\U0001F1F7"  # 🇧🇷 — REGIONAL INDICATOR B + R


def _e_modificador_ou_vs16(caractere: str) -> bool:
    """Componente opcional de uma base: modifier de tom de pele OU VS16."""
    if caractere == _VS16:
        return True
    return _MODIFICADOR_MIN <= ord(caractere) <= _MODIFICADOR_MAX


def _consumir_componente_opcional(texto: str, indice: int) -> int:
    """Avanca UM indice se `texto[indice]` for modifier ou VS16; senao, mantem."""
    if indice < len(texto) and _e_modificador_ou_vs16(texto[indice]):
        return indice + 1
    return indice


def _tentar_base_com_zwj(texto: str, indice: int) -> int | None:
    """
    Tenta casar uma sequencia BASE comecando em `indice`: um codepoint da
    allowlist, componente opcional (modifier OU VS16, no maximo um), e entao
    zero ou mais grupos ZWJ + nova base + componente PROPRIO opcional. Sem
    essa recursividade da base ligada por ZWJ, "profissao" (`mulher ZWJ
    esculapio VS16`) deixaria o VS16 final sem dono.

    Devolve o indice seguinte ao ultimo codepoint consumido, ou `None` se
    `texto[indice]` nao inicia uma base valida.

    ITERATIVO, NUNCA RECURSIVO: uma cadeia de milhares de bases ligadas por
    ZWJ (mensagem adversarial) nao pode depender da pilha de chamadas do
    Python — e o motivo de ser um `while`, nao uma funcao que chama a si
    mesma a cada elo da cadeia.

    Um ZWJ que NAO e seguido por uma base valida (fim da string, whitespace,
    codepoint fora da allowlist) NAO e consumido: a cadeia para ali, o ZWJ
    solto fica para a proxima iteracao do scanner decidir — que vai falhar
    nele, porque ZWJ isolado nunca inicia sequencia. E assim que
    "polegar+ZWJ final" (sequencia que nao fecha) e rejeitado.
    """
    if indice >= len(texto) or ord(texto[indice]) not in _BASES_PERMITIDAS:
        return None
    posicao = _consumir_componente_opcional(texto, indice + 1)
    while posicao < len(texto) and texto[posicao] == _ZWJ:
        proxima_base = posicao + 1
        if proxima_base < len(texto) and ord(texto[proxima_base]) in _BASES_PERMITIDAS:
            posicao = _consumir_componente_opcional(texto, proxima_base + 1)
        else:
            break  # ZWJ sem base valida depois: nao consumido, cadeia termina aqui.
    return posicao


def _tentar_bandeira(texto: str, indice: int) -> int | None:
    """Tenta casar o PAR EXATO da bandeira allowlistada comecando em `indice`."""
    fim = indice + len(_BANDEIRA_PERMITIDA)
    if texto[indice:fim] == _BANDEIRA_PERMITIDA:
        return fim
    return None


def _emoji_only(texto: str) -> bool:
    """
    Varredura de indice, esquerda para direita: casa whitespace (mesmo
    criterio de `str.isspace()` de P-5 — `U+200B` nao conta), bandeira
    allowlistada ou base (com sua cadeia ZWJ). QUALQUER posicao nao
    reconhecida derruba o texto INTEIRO para SUBSTANTIVO — nao ha
    "parcialmente emoji-only". Nunca chega aqui com `texto` vazio: P-5
    intercepta antes, no chamador.
    """
    indice = 0
    tamanho = len(texto)
    encontrou_alguma_sequencia = False
    while indice < tamanho:
        if texto[indice].isspace():
            indice += 1
            continue
        consumido = _tentar_bandeira(texto, indice)
        if consumido is None:
            consumido = _tentar_base_com_zwj(texto, indice)
        if consumido is None:
            return False
        encontrou_alguma_sequencia = True
        indice = consumido
    return encontrou_alguma_sequencia


class MensagemLote(BaseModel):
    """
    Contrato LOCAL desta fase (P-1/P-2) — nao mora em `contratos.py`: a Fase
    1 diferiu `MensagemLote` de proposito, e o check de `test_v2_contratos.py`
    protege a ausencia deste nome la.

    Dois campos, os dois obrigatorios, sem default. Nenhum outro campo:
    nada de `message_id`, `whatsapp_msg_id`, `conversation_id`, telefone,
    nome, timestamp, `media_url`, alvo de reacao, caption separado ou
    metadado da Meta — a funcao precisa saber so duas coisas, que TIPO de
    mensagem e e qual CONTEUDO textual ja esta disponivel.

    Herda `BaseModel` DIRETO, nunca `_ContratoV2`: aquele e nome privado de
    `contratos.py`, e herdar seu comportamento acoplaria esta fase a
    decisao que a Fase 1 tomar para aquela base. `model_config` e repetido
    de proposito — a duplicacao e menor que o acoplamento.

    `msg_type` nao vira enum nem `Literal`: tipo desconhecido precisa
    atravessar o contrato (ver a FRONTEIRA no docstring do modulo).
    """

    model_config = ConfigDict(extra="forbid")

    msg_type: StrictStr
    content: StrictStr


def _item_ignoravel(item: MensagemLote) -> bool:
    """
    Decide UM item, isoladamente — `avaliar_prefiltro` decide o LOTE (P-7).

    P-4, nesta ordem: `reaction`/`sticker` ignoram sempre, qualquer que seja
    o `content` (o tipo e a fonte, nunca o conteudo — e o que garante que
    `msg_type="reaction", content="Quero viajar em janeiro"` continua
    ignoravel). `text` aplica P-5 (vazio) e depois P-6 (emoji-only).
    QUALQUER outro tipo — midia, `interactive`, tipo futuro da Meta — e
    substantivo por P-3, mesmo com `content=""`.
    """
    if item.msg_type in _TIPOS_SEMPRE_IGNORAVEIS:
        return True
    if item.msg_type != "text":
        return False
    if item.content.strip() == "":
        return True
    return _emoji_only(item.content)


def avaliar_prefiltro(lote: list[MensagemLote]) -> DecisaoPrefiltro:
    """
    Decide se o LOTE inteiro pode ser descartado sem chamar a IA. Pura: nao
    toca banco, IO, relogio nem registra evento (P-9), e NAO muta `lote`
    nem nenhum `MensagemLote` dele — o lote pertence a quem chama.

    P-7: lote vazio ignora — coberto pelo `any()` abaixo sem ramo especial,
    porque `any(...)` de um gerador vazio e `False`, e cai direto no
    `return` de ignorar. Existindo ao menos um item substantivo, o LOTE
    INTEIRO continua, em qualquer posicao da lista (ver os casos do grupo K
    do teste, com o substantivo primeiro, no meio ou por ultimo).
    """
    algum_item_substantivo = any(not _item_ignoravel(item) for item in lote)
    if algum_item_substantivo:
        return DecisaoPrefiltro(ignorar=False, motivo=None)
    return DecisaoPrefiltro(ignorar=True, motivo=_MOTIVO_LOTE_SEM_CONTEUDO)
