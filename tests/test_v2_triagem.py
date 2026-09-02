"""
BIA-V2 Fase 2 - triagem deterministica de conversas/app/v2/triagem.py.

Trava `avaliar_triagem(dados: TriageData) -> ResultadoTriagem`: a tabela de 13
casos obrigatorios da FASE 2 do plano, as decisoes D-1 a D-9 do subgate de
especificacao, e as Decisoes 1 (precedencia do modo global), 2
(`datas_definidas is None`) e 3 (destino vazio e lista parcialmente valida).
Fonte normativa unica: docs/superpowers/plans/2026-08-29-bia-v2.md, secao
"FASE 2 - Triagem deterministica". Nenhuma regra aqui vem da V1 - a V2 diverge
dela de proposito em pelo menos dois pontos registrados no plano (endereco por
indice em vez de nome, e destino vazio nao descartado em silencio).

FORMA DA ASSERCAO: comparacao de LISTA EXATA, nunca `set`. A ordem de
`campos_faltantes` e parte do contrato (D-8: campos base na ordem `nome`,
`email`, `destinos`, `total_pessoas`, `adultos`, `criancas`,
`datas_definidas`, depois os problemas por destino na ordem da lista e, dentro
de cada destino, `data_inicio`, `data_fim`, `dias`). Comparar conjuntos
apagaria justamente a propriedade que o teste existe para travar. Pela mesma
razao a gramatica fechada dos identificadores entra como defesa ADICIONAL no
fim do arquivo, nunca como prova unica: pertencer a gramatica nao prova que o
`N` emitido e a posicao real do item na lista.

O caso mais importante deste arquivo e o do GRUPO M: um `destino` com cara de
indice (`"5551999999999"`, o PII canonico de test_v2_eventos_validacao.py) e
outro igual ao literal `email`. Sao os dois valores que escapam de uma
assercao de pertinencia a gramatica, e sao o que separa `f"destinos[{i}]..."`
de `f"destinos[{d.destino}]..."` - um deslize trivial ao iterar
`for d in dados.destinos`.

Roda standalone:  python tests/test_v2_triagem.py
"""
import ast
import pathlib
import re
import subprocess
import sys
import tempfile
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Conversas - o literal abaixo tambem e o discriminador de job do CI
# (.github/workflows/test.yml separa as suites com grep -l CONVERSAS_DIR).
CONVERSAS_DIR = ROOT / "conversas"

sys.path.insert(0, str(CONVERSAS_DIR))

failures = []


def check(cond, msg):
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


from app.v2.contratos import (  # noqa: E402
    DestinoTriagem,
    ResultadoTriagem,
    TriageData,
)
from app.v2.triagem import avaliar_triagem  # noqa: E402


# --- infraestrutura dos casos ---------------------------------------------
# Toda avaliacao passa por `caso()`, que faz DUAS afirmacoes: a lista exata
# esperada, e a invariante D-9 (`completa` e derivado de `campos_faltantes`,
# nunca construido por caminho independente). D-9 vale para TODO caso da
# tabela, entao nao e um check avulso no fim - e o segundo check de cada caso.
_AVALIACOES = []  # (rotulo, resultado, dados) - reusado pelos checks globais


def caso(rotulo, dados, esperado):
    """Avalia `dados`, compara a LISTA EXATA e assere D-9. Devolve o resultado."""
    try:
        resultado = avaliar_triagem(dados)
    except Exception as exc:  # noqa: BLE001 - relatar o tipo errado, nao mascarar
        check(False, f"{rotulo}: avaliar_triagem levantou {type(exc).__name__}: {exc!r}")
        check(False, f"{rotulo}: D-9 nao pode ser avaliada (a chamada levantou)")
        return None
    obtido = list(resultado.campos_faltantes)
    _AVALIACOES.append((rotulo, resultado, dados))
    check(obtido == esperado, f"{rotulo}: campos_faltantes == {esperado} (obtido={obtido})")
    check(
        resultado.completa == (obtido == []),
        f"{rotulo}: D-9 completa e derivado de campos_faltantes "
        f"(completa={resultado.completa!r}, campos_faltantes={obtido})",
    )
    return resultado


def td(**over):
    """`TriageData` com nome, email e contagens completos e coerentes.

    `destinos` e `datas_definidas` ficam FORA do default de proposito: os dois
    definem o modo global e a lista avaliada, entao todo caso os declara
    explicitamente (ou omite deliberadamente, como no GRUPO J). `over`
    sobrescreve qualquer campo.
    """
    campos = {
        "nome": "Joao",
        "email": "joao@example.com",
        "total_pessoas": 2,
        "adultos": 2,
        "criancas": 0,
    }
    campos.update(over)
    return TriageData(**campos)


def dst(nome_destino, **over):
    """`DestinoTriagem` novo a cada chamada.

    Nunca reaproveitar a MESMA instancia entre casos: o Pydantic v2 nao
    revalida instancia de modelo passada como campo (`revalidate_instances`
    e `never` por default), entao o objeto seria compartilhado de verdade e
    uma mutacao indevida em `avaliar_triagem` vazaria de um caso para o outro,
    mascarando ou espalhando a falha.
    """
    return DestinoTriagem(destino=nome_destino, **over)


INI = date(2026, 3, 10)
FIM = date(2026, 3, 15)
FIM2 = date(2026, 3, 20)

# Os sete campos base na ordem canonica de D-1/D-8 - usada como esperado nos
# casos em que absolutamente nada foi extraido.
BASE_TODOS = ["nome", "email", "destinos", "total_pessoas", "adultos", "criancas", "datas_definidas"]


# === GRUPO A - os 13 casos obrigatorios da tabela da FASE 2 ================
# Onde a tabela do plano nao fixa os campos base (casos 4, 5, 7, 9 a 13), o
# caso e montado com a base COMPLETA de proposito, para que a lista exata
# isole a parte por destino - a mesma tecnica que o plano usa nos dois
# exemplos literais da Decisao 3.

caso(
    "A1 (caso 1) Atacama com datas, datas_definidas=True, 2 adultos 0 criancas",
    td(destinos=[dst("Atacama", data_inicio=INI, data_fim=FIM)], datas_definidas=True),
    [],
)

caso(
    "A2 (caso 2) Atacama com dias=5, datas_definidas=False, total 3 = 2 adultos + 1 crianca",
    td(
        total_pessoas=3, adultos=2, criancas=1,
        destinos=[dst("Atacama", dias=5)], datas_definidas=False,
    ),
    [],
)

# Caso 3 da tabela: SO as tres contagens foram extraidas, e a soma nao fecha.
# Os tres sao individualmente validos (3>=1, 2>=1, 0>=0), entao o guard de
# D-3 libera a invariante e ela emite os tres. Nome, email, destinos e
# datas_definidas entram antes, na ordem de D-8.
caso(
    "A3 (caso 3) total 3, adultos 2, criancas 0 - soma incoerente e nada mais extraido",
    TriageData(total_pessoas=3, adultos=2, criancas=0),
    BASE_TODOS,
)

caso(
    "A4 (caso 4) Atacama+Uyuni, datas_definidas=True, datas so no Atacama",
    td(
        destinos=[dst("Atacama", data_inicio=INI, data_fim=FIM), dst("Uyuni")],
        datas_definidas=True,
    ),
    ["destinos[1].data_inicio", "destinos[1].data_fim"],
)

caso(
    "A5 (caso 5) Atacama+Uyuni, datas_definidas=False, so quantidade_dias_pretendida=8",
    td(
        destinos=[dst("Atacama"), dst("Uyuni")],
        datas_definidas=False, quantidade_dias_pretendida=8,
    ),
    ["destinos[0].dias", "destinos[1].dias"],
)

# Caso 6 - o UNICO identificador de campos_faltantes com provenance normativa
# anterior ao subgate.
caso(
    "A6 (caso 6) tudo menos email",
    td(email=None, destinos=[dst("Atacama", dias=5)], datas_definidas=False),
    ["email"],
)

caso(
    "A7 (caso 7) zero destinos",
    td(destinos=[], datas_definidas=True),
    ["destinos"],
)

caso(
    "A8 (caso 8) adultos=2, criancas=1, total=4",
    td(
        total_pessoas=4, adultos=2, criancas=1,
        destinos=[dst("Atacama", dias=5)], datas_definidas=False,
    ),
    ["total_pessoas", "adultos", "criancas"],
)

# Caso 9 - MODO MISTO. D-3 proibe identificador proprio: reporta-se o que o
# modo GLOBAL exige e falta, e o modo global e `datas_definidas`, nao a
# inspecao do que cada destino ja tem. Por isso o mesmo par de destinos
# produz listas diferentes conforme o modo declarado.
caso(
    "A9a (caso 9) Atacama com datas + Uyuni com dias, datas_definidas=True - falta o periodo do Uyuni",
    td(
        destinos=[dst("Atacama", data_inicio=INI, data_fim=FIM), dst("Uyuni", dias=4)],
        datas_definidas=True,
    ),
    ["destinos[1].data_inicio", "destinos[1].data_fim"],
)

caso(
    "A9b (caso 9) Atacama com datas + Uyuni com dias, datas_definidas=False - falta dias do Atacama",
    td(
        destinos=[dst("Atacama", data_inicio=INI, data_fim=FIM), dst("Uyuni", dias=4)],
        datas_definidas=False,
    ),
    ["destinos[0].dias"],
)

caso(
    "A10 (caso 10) Atacama+Uyuni ambos com datas, datas_definidas=True",
    td(
        destinos=[
            dst("Atacama", data_inicio=INI, data_fim=FIM),
            dst("Uyuni", data_inicio=FIM, data_fim=FIM2),
        ],
        datas_definidas=True,
    ),
    [],
)

caso(
    "A11 (caso 11) Atacama+Uyuni ambos so com dias, datas_definidas=False",
    td(destinos=[dst("Atacama", dias=5), dst("Uyuni", dias=3)], datas_definidas=False),
    [],
)

caso(
    "A12 (caso 12) datas_definidas=True mas destino so tem dias",
    td(destinos=[dst("Atacama", dias=5)], datas_definidas=True),
    ["destinos[0].data_inicio", "destinos[0].data_fim"],
)

caso(
    "A13 (caso 13) datas_definidas=False mas destino so tem datas",
    td(destinos=[dst("Atacama", data_inicio=INI, data_fim=FIM)], datas_definidas=False),
    ["destinos[0].dias"],
)


# === GRUPO B - campos base (D-1 / D-8), individualmente e combinados =======

caso(
    "B1 base: so nome ausente",
    td(nome=None, destinos=[dst("Atacama", dias=5)], datas_definidas=False),
    ["nome"],
)
caso(
    "B2 base: so email ausente",
    td(email=None, destinos=[dst("Atacama", dias=5)], datas_definidas=False),
    ["email"],
)
caso(
    "B3 base: so destinos ausente (lista vazia)",
    td(destinos=[], datas_definidas=False),
    ["destinos"],
)
caso(
    "B4 base: so total_pessoas ausente",
    td(total_pessoas=None, destinos=[dst("Atacama", dias=5)], datas_definidas=False),
    ["total_pessoas"],
)
caso(
    "B5 base: so adultos ausente",
    td(adultos=None, destinos=[dst("Atacama", dias=5)], datas_definidas=False),
    ["adultos"],
)
caso(
    "B6 base: so criancas ausente",
    td(criancas=None, destinos=[dst("Atacama", dias=5)], datas_definidas=False),
    ["criancas"],
)
caso(
    "B7 base: so datas_definidas ausente (destino ja completo em dias)",
    td(destinos=[dst("Atacama", dias=5)]),
    ["datas_definidas"],
)

# Combinados - a ordem de D-8 so e observavel com mais de uma falha.
caso(
    "B8 base combinado: nome + criancas ausentes, na ordem de D-8",
    td(nome=None, criancas=None, destinos=[dst("Atacama", dias=5)], datas_definidas=False),
    ["nome", "criancas"],
)
caso(
    "B9 base combinado: email + adultos + datas_definidas ausentes",
    TriageData(nome="Joao", email=None, destinos=[dst("Uyuni")], total_pessoas=2, adultos=None, criancas=0),
    ["email", "adultos", "datas_definidas"],
)
caso(
    "B10 base combinado: TriageData() vazio emite os sete na ordem canonica",
    TriageData(),
    BASE_TODOS,
)

# `quantidade_dias_pretendida` permanece no schema como fato extraivel, mas
# NAO satisfaz completude em nenhum dos dois modos e NUNCA e emitido (D-1).
caso(
    "B11 quantidade_dias_pretendida=7 NAO satisfaz dias no modo datas_definidas=False",
    td(destinos=[dst("Atacama")], datas_definidas=False, quantidade_dias_pretendida=7),
    ["destinos[0].dias"],
)
caso(
    "B12 quantidade_dias_pretendida=7 NAO satisfaz nada no modo datas_definidas=True",
    td(destinos=[dst("Atacama")], datas_definidas=True, quantidade_dias_pretendida=7),
    ["destinos[0].data_inicio", "destinos[0].data_fim"],
)
caso(
    "B13 quantidade_dias_pretendida presente nao atrapalha uma triagem completa",
    td(destinos=[dst("Atacama", dias=5)], datas_definidas=False, quantidade_dias_pretendida=99),
    [],
)


# === GRUPO C - strings vazias e whitespace (D-4) ===========================
# `""` e string so de whitespace contam como AUSENTES em nome, email e
# destino. A decisao vive dentro de avaliar_triagem(): o contrato preserva a
# diferenca entre "a IA nao extraiu" e "a IA extraiu vazio".

caso("C1 nome=''", td(nome="", destinos=[dst("Atacama", dias=5)], datas_definidas=False), ["nome"])
caso("C2 nome='   '", td(nome="   ", destinos=[dst("Atacama", dias=5)], datas_definidas=False), ["nome"])
caso("C3 nome='\\t\\n'", td(nome="\t\n", destinos=[dst("Atacama", dias=5)], datas_definidas=False), ["nome"])
caso("C4 email=''", td(email="", destinos=[dst("Atacama", dias=5)], datas_definidas=False), ["email"])
caso("C5 email='   '", td(email="   ", destinos=[dst("Atacama", dias=5)], datas_definidas=False), ["email"])

# Destino vazio/whitespace: emite `destinos` (Decisao 3) e NENHUM
# `destinos[N].*` para o item sem identidade - sem identidade de destino nao
# ha o que perguntar sobre o periodo dele.
caso("C6 destino=''", td(destinos=[dst("")], datas_definidas=False), ["destinos"])
caso("C7 destino='   '", td(destinos=[dst("   ")], datas_definidas=False), ["destinos"])
caso("C8 destino='\\t'", td(destinos=[dst("\t")], datas_definidas=False), ["destinos"])

# Whitespace em VOLTA de conteudo real nao torna o campo ausente.
caso(
    "C9 nome/email/destino com whitespace em volta de conteudo real continuam validos",
    td(
        nome="  Joao  ", email="  joao@example.com  ",
        destinos=[dst("  Atacama  ", dias=5)], datas_definidas=False,
    ),
    [],
)


# === GRUPO D - contagens (D-5) ============================================
# total_pessoas >= 1, adultos >= 1, criancas >= 0. Negativo e invalido, e
# D-7 diz que valor presente porem invalido ENTRA em campos_faltantes.

for _rot, _over in [
    ("D1 total_pessoas=None", {"total_pessoas": None}),
    ("D2 total_pessoas=0", {"total_pessoas": 0}),
    ("D3 total_pessoas=-1", {"total_pessoas": -1}),
]:
    caso(_rot, td(destinos=[dst("Atacama", dias=5)], datas_definidas=False, **_over), ["total_pessoas"])

for _rot, _over in [
    ("D4 adultos=None", {"adultos": None}),
    ("D5 adultos=0", {"adultos": 0}),
    ("D6 adultos=-1", {"adultos": -1}),
]:
    caso(_rot, td(destinos=[dst("Atacama", dias=5)], datas_definidas=False, **_over), ["adultos"])

for _rot, _over in [
    ("D7 criancas=None", {"criancas": None}),
    ("D8 criancas=-1", {"criancas": -1}),
]:
    caso(_rot, td(destinos=[dst("Atacama", dias=5)], datas_definidas=False, **_over), ["criancas"])

caso(
    "D9 criancas=0 e VALIDO (exigido pelo caso 1 da tabela)",
    td(criancas=0, total_pessoas=2, adultos=2, destinos=[dst("Atacama", dias=5)], datas_definidas=False),
    [],
)
caso(
    "D10 fronteira minima total_pessoas=1, adultos=1, criancas=0",
    td(
        total_pessoas=1, adultos=1, criancas=0,
        destinos=[dst("Atacama", dias=5)], datas_definidas=False,
    ),
    [],
)


# === GRUPO E - invariante da soma e o guard da invariante cruzada (D-3) ====
# A invariante so e avaliada quando os TRES operandos estao presentes e
# individualmente validos. Se algum ja entrou em campos_faltantes por ausencia
# ou invalidez propria, ela nao e avaliada e nao acrescenta identificador -
# a leitura literal nem seria implementavel (`None + None` levanta TypeError,
# e a funcao existe justamente para receber parciais).

caso(
    "E1 soma coerente 2 + 1 == 3",
    td(total_pessoas=3, adultos=2, criancas=1, destinos=[dst("Atacama", dias=5)], datas_definidas=False),
    [],
)
caso(
    "E2 soma incoerente 2 + 1 vs total=4 emite os TRES",
    td(total_pessoas=4, adultos=2, criancas=1, destinos=[dst("Atacama", dias=5)], datas_definidas=False),
    ["total_pessoas", "adultos", "criancas"],
)
caso(
    "E3 soma incoerente emite os tres na POSICAO CANONICA, depois de nome ausente",
    td(
        nome=None, total_pessoas=4, adultos=2, criancas=1,
        destinos=[dst("Atacama", dias=5)], datas_definidas=False,
    ),
    ["nome", "total_pessoas", "adultos", "criancas"],
)
caso(
    "E4 guard: total_pessoas=None, adultos=2, criancas=1 emite SO total_pessoas (nao os tres)",
    td(
        total_pessoas=None, adultos=2, criancas=1,
        destinos=[dst("Atacama", dias=5)], datas_definidas=False,
    ),
    ["total_pessoas"],
)
caso(
    "E5 guard: adultos=-1, criancas=0, total_pessoas=1 emite SO adultos (nao os tres)",
    td(
        adultos=-1, criancas=0, total_pessoas=1,
        destinos=[dst("Atacama", dias=5)], datas_definidas=False,
    ),
    ["adultos"],
)
caso(
    "E6 guard: criancas=-1 invalida a soma mas emite SO criancas",
    td(
        total_pessoas=5, adultos=2, criancas=-1,
        destinos=[dst("Atacama", dias=5)], datas_definidas=False,
    ),
    ["criancas"],
)

# O guard existe porque a leitura literal levantaria TypeError no parcial
# vazio - o caso mais comum na primeira mensagem.
try:
    avaliar_triagem(TriageData())
    _erro_vazio = None
except Exception as exc:  # noqa: BLE001 - o tipo importa para o diagnostico
    _erro_vazio = exc
check(
    _erro_vazio is None,
    f"E7 TriageData() vazio NAO levanta (guard de D-3 evita o None + None) "
    f"(levantou={type(_erro_vazio).__name__ if _erro_vazio else None})",
)


# === GRUPO F - destinos: vazio, parcial, ordem preservada (Decisao 3) ======
# Tres situacoes emitem `destinos` UMA UNICA VEZ: lista vazia, todos os itens
# com destino vazio/whitespace, e lista parcialmente valida. O item invalido
# NAO e descartado em silencio e o indice dos validos NAO e reatribuido.

caso("F1 lista destinos vazia", td(destinos=[], datas_definidas=False), ["destinos"])
caso(
    "F2 um destino valido e completo",
    td(destinos=[dst("Atacama", dias=5)], datas_definidas=False),
    [],
)
caso(
    "F3 multiplos destinos validos e completos",
    td(destinos=[dst("Atacama", dias=5), dst("Uyuni", dias=3), dst("Salar", dias=1)], datas_definidas=False),
    [],
)
caso(
    "F4 todos os itens com destino vazio/whitespace",
    td(destinos=[dst(""), dst("   ")], datas_definidas=False),
    ["destinos"],
)

# Os DOIS exemplos literais da Decisao 3, byte a byte do plano.
caso(
    "F5 (exemplo literal do plano) lista parcialmente valida: Atacama + '   '",
    TriageData(
        nome="Joao", email="j@x.com", total_pessoas=2, adultos=2, criancas=0,
        datas_definidas=False,
        destinos=[DestinoTriagem(destino="Atacama", dias=None),
                  DestinoTriagem(destino="   ", dias=None)],
    ),
    ["destinos", "destinos[0].dias"],
)
caso(
    "F6 (exemplo literal espelhado) '   ' + Atacama - indice NAO e reatribuido ao descartar o invalido",
    TriageData(
        nome="Joao", email="j@x.com", total_pessoas=2, adultos=2, criancas=0,
        datas_definidas=False,
        destinos=[DestinoTriagem(destino="   ", dias=None),
                  DestinoTriagem(destino="Atacama", dias=None)],
    ),
    ["destinos", "destinos[1].dias"],
)

caso(
    "F7 multiplos invalidos + um valido completo: destinos emitido UMA vez",
    td(destinos=[dst(""), dst("Atacama", dias=2), dst("  ")], datas_definidas=False),
    ["destinos"],
)
caso(
    "F8 invalido ANTES do valido incompleto",
    td(destinos=[dst("  "), dst("Atacama"), dst("\t")], datas_definidas=False),
    ["destinos", "destinos[1].dias"],
)
caso(
    "F9 invalido DEPOIS do valido, entre dois validos incompletos",
    td(destinos=[dst("Atacama"), dst(""), dst("Uyuni")], datas_definidas=False),
    ["destinos", "destinos[0].dias", "destinos[2].dias"],
)
caso(
    "F10 lista parcialmente valida no modo datas",
    td(destinos=[dst("Atacama"), dst("  ")], datas_definidas=True),
    ["destinos", "destinos[0].data_inicio", "destinos[0].data_fim"],
)

# D-2 justifica o endereçamento por INDICE dizendo que o nome do destino "pode
# repetir". F11/F12 sao a trava dessa propriedade: sem elas, uma implementacao
# que deduplicasse por nome — o anti-padrao da V1, onde `dias_por_destino` e um
# dict chaveado por nome (`app/models/lead.py:27`) e colapsa duplicatas por
# construcao — passaria na suite inteira e ainda assim devolveria `completa=True`
# com um destino sem periodo. Os dois sentidos importam: o incompleto DEPOIS do
# completo e o que mata o colapso.
caso(
    "F11 nome de destino REPETIDO, incompleto depois do completo",
    td(destinos=[dst("Atacama", dias=3), dst("Atacama")], datas_definidas=False),
    ["destinos[1].dias"],
)
caso(
    "F12 nome de destino REPETIDO, incompleto antes do completo",
    td(destinos=[dst("Atacama"), dst("Atacama", dias=3)], datas_definidas=False),
    ["destinos[0].dias"],
)


# === GRUPO G - modo datas_definidas is True (Decisao 1) ====================
# Por destino valido exige data_inicio E data_fim. `dias` e IRRELEVANTE:
# ausente, 0 ou negativo nao gera campos_faltantes neste modo.

caso(
    "G1 modo True: ambas as datas presentes",
    td(destinos=[dst("Atacama", data_inicio=INI, data_fim=FIM)], datas_definidas=True),
    [],
)
caso(
    "G2 modo True: ambas ausentes",
    td(destinos=[dst("Atacama")], datas_definidas=True),
    ["destinos[0].data_inicio", "destinos[0].data_fim"],
)
caso(
    "G3 modo True: so data_inicio",
    td(destinos=[dst("Atacama", data_inicio=INI)], datas_definidas=True),
    ["destinos[0].data_fim"],
)
caso(
    "G4 modo True: so data_fim",
    td(destinos=[dst("Atacama", data_fim=FIM)], datas_definidas=True),
    ["destinos[0].data_inicio"],
)
caso(
    "G5 modo True: dois destinos, ambos sem datas",
    td(destinos=[dst("Atacama"), dst("Uyuni")], datas_definidas=True),
    [
        "destinos[0].data_inicio", "destinos[0].data_fim",
        "destinos[1].data_inicio", "destinos[1].data_fim",
    ],
)
caso(
    "G6 modo True: incompleto no indice 0, completo no indice 1",
    td(
        destinos=[dst("Atacama"), dst("Uyuni", data_inicio=FIM, data_fim=FIM2)],
        datas_definidas=True,
    ),
    ["destinos[0].data_inicio", "destinos[0].data_fim"],
)
caso(
    "G7 modo True: tres destinos, so o do MEIO sem datas",
    td(
        destinos=[
            dst("Atacama", data_inicio=INI, data_fim=FIM),
            dst("Uyuni"),
            dst("Salar", data_inicio=FIM, data_fim=FIM2),
        ],
        datas_definidas=True,
    ),
    ["destinos[1].data_inicio", "destinos[1].data_fim"],
)

# `dias` irrelevante no modo True - as tres formas de invalidez de dias.
caso(
    "G8 modo True: datas validas com dias=None nao emite .dias",
    td(destinos=[dst("Atacama", data_inicio=INI, data_fim=FIM, dias=None)], datas_definidas=True),
    [],
)
caso(
    "G9 modo True: datas validas com dias=0 nao emite .dias",
    td(destinos=[dst("Atacama", data_inicio=INI, data_fim=FIM, dias=0)], datas_definidas=True),
    [],
)
caso(
    "G10 modo True: datas validas com dias=-100 nao emite .dias",
    td(destinos=[dst("Atacama", data_inicio=INI, data_fim=FIM, dias=-100)], datas_definidas=True),
    [],
)


# === GRUPO H - ordem cronologica (D-6) e o guard da invariante cruzada =====
# data_fim >= data_inicio. `data_inicio == data_fim` e VALIDO (passeio de um
# dia). Invalida emite AMBOS os identificadores, porque nao se sabe qual das
# duas o cliente informou errado. Com uma das datas ausente a invariante NAO
# e avaliada: para datas, "individualmente valido" se reduz a presenca.

caso(
    "H1 cronologia: inicio < fim",
    td(destinos=[dst("Atacama", data_inicio=INI, data_fim=FIM)], datas_definidas=True),
    [],
)
caso(
    "H2 cronologia: inicio == fim e VALIDO (viagem de um dia)",
    td(destinos=[dst("Atacama", data_inicio=INI, data_fim=INI)], datas_definidas=True),
    [],
)
caso(
    "H3 cronologia: fim < inicio emite AMBOS, nesta ordem",
    td(destinos=[dst("Atacama", data_inicio=FIM, data_fim=INI)], datas_definidas=True),
    ["destinos[0].data_inicio", "destinos[0].data_fim"],
)
caso(
    "H4 guard cronologico: data_fim ausente emite SO data_fim",
    td(destinos=[dst("Atacama", data_inicio=FIM)], datas_definidas=True),
    ["destinos[0].data_fim"],
)
caso(
    "H5 guard cronologico: data_inicio ausente emite SO data_inicio",
    td(destinos=[dst("Atacama", data_fim=INI)], datas_definidas=True),
    ["destinos[0].data_inicio"],
)
caso(
    "H6 cronologia: segundo destino invertido",
    td(
        destinos=[
            dst("Atacama", data_inicio=INI, data_fim=FIM),
            dst("Uyuni", data_inicio=FIM2, data_fim=FIM),
        ],
        datas_definidas=True,
    ),
    ["destinos[1].data_inicio", "destinos[1].data_fim"],
)
caso(
    "H7 cronologia: primeiro destino invertido, segundo valido",
    td(
        destinos=[
            dst("Atacama", data_inicio=FIM, data_fim=INI),
            dst("Uyuni", data_inicio=FIM, data_fim=FIM2),
        ],
        datas_definidas=True,
    ),
    ["destinos[0].data_inicio", "destinos[0].data_fim"],
)


# === GRUPO I - modo datas_definidas is False (Decisao 1) ===================
# Por destino valido exige `dias > 0`. Datas sao IRRELEVANTES: ausentes,
# incompletas ou cronologicamente invalidas nao geram campos_faltantes.

caso("I1 modo False: dias=None", td(destinos=[dst("Atacama")], datas_definidas=False), ["destinos[0].dias"])
caso("I2 modo False: dias=0", td(destinos=[dst("Atacama", dias=0)], datas_definidas=False), ["destinos[0].dias"])
caso("I3 modo False: dias=-1", td(destinos=[dst("Atacama", dias=-1)], datas_definidas=False), ["destinos[0].dias"])
caso("I4 modo False: dias=1 (fronteira valida)", td(destinos=[dst("Atacama", dias=1)], datas_definidas=False), [])
caso("I5 modo False: dias=30", td(destinos=[dst("Atacama", dias=30)], datas_definidas=False), [])

caso(
    "I6 modo False: dias valido com datas INVERTIDAS nao emite path de data",
    td(destinos=[dst("Atacama", dias=5, data_inicio=FIM, data_fim=INI)], datas_definidas=False),
    [],
)
caso(
    "I7 modo False: dias valido com so data_inicio nao emite path de data",
    td(destinos=[dst("Atacama", dias=5, data_inicio=INI)], datas_definidas=False),
    [],
)
caso(
    "I8 modo False: dias valido com so data_fim nao emite path de data",
    td(destinos=[dst("Atacama", dias=5, data_fim=FIM)], datas_definidas=False),
    [],
)
caso(
    "I9 modo False: dias=0 com datas validas emite SO .dias",
    td(destinos=[dst("Atacama", dias=0, data_inicio=INI, data_fim=FIM)], datas_definidas=False),
    ["destinos[0].dias"],
)
caso(
    "I10 modo False: dois destinos, so o segundo sem dias",
    td(destinos=[dst("Atacama", dias=3), dst("Uyuni")], datas_definidas=False),
    ["destinos[1].dias"],
)


# === GRUPO J - datas_definidas is None (Decisao 2) =========================
# Enquanto o modo global nao estiver definido, `datas_definidas` entra em
# campos_faltantes e NENHUM destinos[N].* e emitido - o motor ainda nao sabe
# qual conjunto de campos e requerido. Nao exigir datas e dias ao mesmo tempo,
# e nao adivinhar o modo pelo que ja esta preenchido.

caso("J1 modo None: destino sem nada", td(destinos=[dst("Atacama")]), ["datas_definidas"])
caso(
    "J2 modo None: destino com datas completas",
    td(destinos=[dst("Atacama", data_inicio=INI, data_fim=FIM)]),
    ["datas_definidas"],
)
caso("J3 modo None: destino com dias valido", td(destinos=[dst("Atacama", dias=5)]), ["datas_definidas"])
caso(
    "J4 modo None: destino com datas invertidas E dias=-5",
    td(destinos=[dst("Atacama", data_inicio=FIM, data_fim=INI, dias=-5)]),
    ["datas_definidas"],
)
caso(
    "J5 modo None: dois destinos sem nada",
    td(destinos=[dst("Atacama"), dst("Uyuni")]),
    ["datas_definidas"],
)
caso("J6 modo None + lista de destinos vazia", td(destinos=[]), ["destinos", "datas_definidas"])
# Intersecao Decisao 2 x Decisao 3: sem modo global nenhum `destinos[N].*` sai
# (Decisao 2), mas `destinos` e campo BASE (D-1) e continua saindo pelo item sem
# identidade (Decisao 3). As duas regras vivem em camadas diferentes e nao se
# anulam — a ordem e a de `_ORDEM_BASE`, `destinos` antes de `datas_definidas`.
caso(
    "J7 modo None + lista parcialmente valida",
    td(destinos=[dst("Atacama"), dst("  ")]),
    ["destinos", "datas_definidas"],
)


# === GRUPO K - modo misto sem pseudo-campo ================================
# Reporta-se o que o modo global exige e falta; `modo_misto` nao existe.

caso(
    "K1 misto com tres destinos no modo True: datas + dias + nada",
    td(
        destinos=[
            dst("Atacama", data_inicio=INI, data_fim=FIM),
            dst("Uyuni", dias=4),
            dst("Salar"),
        ],
        datas_definidas=True,
    ),
    [
        "destinos[1].data_inicio", "destinos[1].data_fim",
        "destinos[2].data_inicio", "destinos[2].data_fim",
    ],
)
caso(
    "K2 misto com tres destinos no modo False: datas + dias + nada",
    td(
        destinos=[
            dst("Atacama", data_inicio=INI, data_fim=FIM),
            dst("Uyuni", dias=4),
            dst("Salar"),
        ],
        datas_definidas=False,
    ),
    ["destinos[0].dias", "destinos[2].dias"],
)


# === GRUPO L - ordem determinista com muitas falhas simultaneas (D-8) ======
# So com varias falhas ao mesmo tempo a ordem vira observavel. LISTA EXATA,
# nunca `set`.

caso(
    "L1 base quase toda invalida + um destino sem dias, na ordem de D-8",
    TriageData(
        nome="  ", email=None,
        destinos=[dst("Atacama"), dst("")],
        total_pessoas=None, adultos=0, criancas=-1, datas_definidas=False,
    ),
    ["nome", "email", "destinos", "total_pessoas", "adultos", "criancas", "destinos[0].dias"],
)
caso(
    "L2 base invalida + soma incoerente + destino com datas invertidas",
    TriageData(
        nome="", email="   ",
        destinos=[dst("  "), dst("Atacama", data_inicio=FIM, data_fim=INI)],
        total_pessoas=3, adultos=1, criancas=1, datas_definidas=True,
    ),
    [
        "nome", "email", "destinos", "total_pessoas", "adultos", "criancas",
        "destinos[1].data_inicio", "destinos[1].data_fim",
    ],
)


# === GRUPO M - fixture adversarial (PII no campo `destino`) ===============
# D-2 garante que nenhum texto do cliente entra no protocolo, e hoje isso
# depende de o implementador escrever f"destinos[{i}]..." e nao
# f"destinos[{d.destino}]...". Os dois valores abaixo sao exatamente os que
# escapam de uma assercao de pertinencia a gramatica: um telefone (so digitos,
# casa com `\d+`) e o literal `email` (casa com o alternativo de campo base).
# Por isso o campo base `email` esta PRESENTE e VALIDO em todas as fixtures
# deste grupo: assim uma aparicao de "email" em campos_faltantes e, por si so,
# prova do vazamento - se ele estivesse faltando de verdade, D-8 ("identificador
# ja presente nao e duplicado") fundiria o vazamento com o faltante legitimo e
# a lista exata bateria mesmo assim.
PII_TELEFONE = "5551999999999"
PII_LITERAL_BASE = "email"
_TEXTOS_ADVERSARIAIS = (PII_TELEFONE, PII_LITERAL_BASE)

_RE_INDICE = re.compile(r"^destinos\[(\d+)\]\.")


def indices_emitidos(lista):
    """Os `N` que aparecem nos identificadores por destino, na ordem em que sairam."""
    return [int(m.group(1)) for m in (_RE_INDICE.match(item) for item in lista) if m]


def confere_adversarial(rotulo, resultado, dados, indices_reais_esperados):
    """As quatro provas do caso adversarial, alem da lista exata ja feita em caso()."""
    if resultado is None:
        check(False, f"{rotulo}: sem resultado para conferir (a chamada levantou)")
        return
    lista = list(resultado.campos_faltantes)

    # (2) todo N emitido e a posicao REAL do item na lista avaliada.
    _indices = indices_emitidos(lista)
    check(
        _indices == indices_reais_esperados,
        f"{rotulo}: os N emitidos sao as posicoes REAIS em dados.destinos "
        f"(esperado={indices_reais_esperados}, obtido={_indices}, lista={lista})",
    )
    _em_faixa = all(0 <= n < len(dados.destinos) for n in _indices)
    check(
        _em_faixa,
        f"{rotulo}: todo N emitido e indice valido de dados.destinos "
        f"(len={len(dados.destinos)}, N={_indices})",
    )

    # (3) o texto de DestinoTriagem.destino nunca aparece dentro de identificador.
    _vazou = sorted(
        {texto for texto in _TEXTOS_ADVERSARIAIS for item in lista if texto in item}
    )
    check(
        _vazou == [],
        f"{rotulo}: nenhum texto de destino vaza para dentro de um identificador "
        f"(vazaram={_vazou}, lista={lista})",
    )

    # (4) o literal `email` nao vira falso campo faltante - a base esta valida.
    check(
        PII_LITERAL_BASE not in lista,
        f"{rotulo}: '{PII_LITERAL_BASE}' NAO aparece como campo faltante "
        f"(o campo base email esta presente e valido) (lista={lista})",
    )


_dados_m1 = td(
    destinos=[dst(PII_TELEFONE), dst("Atacama", dias=4), dst(PII_LITERAL_BASE)],
    datas_definidas=False,
)
_res_m1 = caso(
    "M1 adversarial modo False: telefone (idx 0) sem dias, Atacama (idx 1) completo, 'email' (idx 2) sem dias",
    _dados_m1,
    ["destinos[0].dias", "destinos[2].dias"],
)
# O indice 2 e a prova de que o item completo do meio nao reindexa os demais.
confere_adversarial("M1", _res_m1, _dados_m1, [0, 2])

_dados_m2 = td(
    destinos=[dst("Atacama", dias=4), dst(PII_LITERAL_BASE), dst(PII_TELEFONE, dias=9)],
    datas_definidas=False,
)
_res_m2 = caso(
    "M2 adversarial modo False: so 'email' (idx 1) incompleto, entre dois completos",
    _dados_m2,
    ["destinos[1].dias"],
)
confere_adversarial("M2", _res_m2, _dados_m2, [1])

_dados_m3 = td(
    destinos=[dst(PII_LITERAL_BASE), dst(PII_TELEFONE, data_inicio=INI, data_fim=FIM)],
    datas_definidas=True,
)
_res_m3 = caso(
    "M3 adversarial modo True: 'email' (idx 0) sem datas, telefone (idx 1) completo",
    _dados_m3,
    ["destinos[0].data_inicio", "destinos[0].data_fim"],
)
confere_adversarial("M3", _res_m3, _dados_m3, [0, 0])


# === GRUPO N - nao-mutacao (D-4) ==========================================
# A nao-mutacao e disciplina da funcao, nao garantia do modelo: os contratos
# nao sao `frozen`, entao um `dados.nome = dados.nome.strip()` de conveniencia
# compilaria - e o mesmo objeto e `AIInterpretationResult.extracted`, entao a
# mutacao alcancaria as fases posteriores. Fixture com whitespace em nome,
# email e destino, e com multiplos destinos, para pegar um `.strip()` in-place.

_dados_n1 = TriageData(
    nome="  Joao  ",
    email="  joao@example.com  ",
    destinos=[
        dst("  Atacama  "),
        dst("   ", dias=0),
        dst("Uyuni", data_inicio=FIM, data_fim=INI),
    ],
    total_pessoas=2, adultos=2, criancas=0, datas_definidas=False,
    quantidade_dias_pretendida=8,
)
_antes_n1 = _dados_n1.model_dump()
caso(
    "N1 fixture de nao-mutacao avalia normalmente (whitespace nao e normalizado, so ignorado)",
    _dados_n1,
    ["destinos", "destinos[0].dias", "destinos[2].dias"],
)
_depois_n1 = _dados_n1.model_dump()
check(
    _antes_n1 == _depois_n1,
    f"N2 avaliar_triagem NAO muta o TriageData recebido "
    f"(antes={_antes_n1}, depois={_depois_n1})",
)

_dados_n3 = td(
    destinos=[dst(PII_TELEFONE), dst("  Atacama  ", dias=2)],
    datas_definidas=True,
)
_antes_n3 = _dados_n3.model_dump()
avaliar_triagem(_dados_n3)
avaliar_triagem(_dados_n3)  # duas chamadas: mutacao acumulativa tambem apareceria
_depois_n3 = _dados_n3.model_dump()
check(
    _antes_n3 == _depois_n3,
    f"N3 duas chamadas seguidas nao acumulam mutacao no mesmo objeto "
    f"(antes={_antes_n3}, depois={_depois_n3})",
)


# === GRUPO O - pureza: nenhuma dependencia de infra, nenhum IO no import ===
# Mesmo padrao dos checks 21a/21b/21c de tests/test_v2_contratos.py: AST dos
# imports DIRETOS (nao depende de runtime nem de ambiente) + DELTA de
# sys.modules medido num subprocesso limpo (pega import dinamico, que a
# analise estatica nao ve). Presenca ABSOLUTA em sys.modules nao prova
# acoplamento e nao e usada aqui.

_ARQUIVO_TRIAGEM = CONVERSAS_DIR / "app" / "v2" / "triagem.py"
_CODIGO_TRIAGEM = _ARQUIVO_TRIAGEM.read_text(encoding="utf-8")

# PROVA A - imports diretos por AST. `ast.walk` percorre a arvore INTEIRA,
# entao pega import escondido dentro de funcao, `if` ou `try`. A raiz vem de
# `alias.name`, nao de `asname`: `import sqlalchemy as sa` nao escapa.
_RAIZES_PERMITIDAS = {
    "__future__", "datetime", "typing", "collections", "dataclasses", "enum", "re",
    "app",  # so `app.v2.contratos`, restringido pelo check 03 logo abaixo
}
# Modulos-alvo da regra de pureza, escritos por extenso: infra da aplicacao,
# ORM, framework web, clientes HTTP (CRM e n8n saem por httpx/requests) e os
# eventos persistentes, que arrastam model, database e config.
_MODULOS_PROIBIDOS = (
    "sqlalchemy", "fastapi", "httpx", "requests", "aiohttp", "urllib",
    "sqlite3", "socket", "n8n",
    "app.database", "app.models", "app.services", "app.config", "app.auth",
    "app.main", "app.schemas", "app.routers", "app.v2.eventos",
)


def _modulos_importados(codigo_fonte):
    """Nome completo de todo import direto do arquivo (via AST).

    Import relativo vira `"." * level + module`, para que `from . import x` e
    `from ..models import y` sejam distinguiveis de um import absoluto.
    """
    nomes = set()
    for no in ast.walk(ast.parse(codigo_fonte)):
        if isinstance(no, ast.Import):
            for alias in no.names:
                nomes.add(alias.name)
        elif isinstance(no, ast.ImportFrom):
            nomes.add("." * no.level + (no.module or ""))
    return nomes


_NOMES_IMPORTADOS = _modulos_importados(_CODIGO_TRIAGEM)
_ABSOLUTOS = {n for n in _NOMES_IMPORTADOS if not n.startswith(".")}
_RELATIVOS = {n for n in _NOMES_IMPORTADOS if n.startswith(".")}
_RAIZES = {n.split(".")[0] for n in _ABSOLUTOS}

check(
    _RAIZES <= _RAIZES_PERMITIDAS,
    f"O1 (PROVA A/AST) imports diretos de triagem.py vem so de "
    f"{sorted(_RAIZES_PERMITIDAS)} (encontradas={sorted(_RAIZES)})",
)

# A raiz `app` sozinha seria fraca demais: ela cobre `app.database` tanto
# quanto `app.v2.contratos`. Por isso o caminho completo tambem e travado.
_APP_IMPORTS = {n for n in _ABSOLUTOS if n == "app" or n.startswith("app.")}
check(
    _APP_IMPORTS <= {"app.v2.contratos"},
    f"O2 unico import de `app` permitido e `app.v2.contratos` "
    f"(encontrados={sorted(_APP_IMPORTS)})",
)
check(
    _RELATIVOS <= {".contratos", "..v2.contratos"},
    f"O3 unico import relativo permitido aponta para contratos "
    f"(encontrados={sorted(_RELATIVOS)})",
)

# Denylist explicita por cima da allowlist: nomeia o que a regra de pureza
# proibe, para que a intencao continue legivel se a allowlist for afrouxada.
_PROIBIDOS_ENCONTRADOS = sorted(
    proibido
    for proibido in _MODULOS_PROIBIDOS
    for nome in _NOMES_IMPORTADOS
    if nome == proibido or nome.startswith(proibido + ".")
)
check(
    _PROIBIDOS_ENCONTRADOS == [],
    f"O4 triagem.py nao importa ORM, framework web, cliente HTTP, infra da app "
    f"nem eventos persistentes (encontrados={_PROIBIDOS_ENCONTRADOS})",
)

# PROVA B - DELTA de runtime, num subprocesso limpo. O baseline inclui
# `pydantic` e `app.v2.contratos`, que triagem.py legitimamente carrega e cuja
# inercia ja e provada por tests/test_v2_contratos.py. O que entrar DEPOIS
# desse ponto foi triagem.py que trouxe. O `cwd` fica fora do repositorio de
# proposito: um IO no import por caminho relativo quebraria o rc.
_SUSPEITOS_IMPORT = [
    "sqlalchemy", "fastapi", "httpx", "requests", "aiohttp", "sqlite3", "socket",
    "app.database", "app.models", "app.services", "app.services.crm",
    "app.config", "app.auth", "app.main", "app.v2.eventos",
]
_codigo_filho = (
    "import sys\n"
    f"sys.path.insert(0, {str(CONVERSAS_DIR)!r})\n"
    "import pydantic\n"
    "import app.v2.contratos  # baseline: inercia ja provada em test_v2_contratos.py\n"
    "baseline = set(sys.modules)\n"
    "import app.v2.triagem\n"
    "delta = set(sys.modules) - baseline\n"
    f"suspeitos = {_SUSPEITOS_IMPORT!r}\n"
    "novos = sorted(m for m in suspeitos if m in delta)\n"
    "print('NOVOS:' + (','.join(novos) if novos else 'NENHUM'))\n"
)
_resultado_subproc = subprocess.run(
    [sys.executable, "-c", _codigo_filho],
    capture_output=True, text=True, timeout=60, cwd=tempfile.gettempdir(),
)
check(
    _resultado_subproc.returncode == 0,
    f"O5 subprocesso limpo, fora do repo, importa app.v2.triagem sem erro - "
    f"nenhum IO por caminho relativo no import "
    f"(rc={_resultado_subproc.returncode}, stderr={_resultado_subproc.stderr!r})",
)
_saida_subproc = _resultado_subproc.stdout.strip()
check(
    _saida_subproc == "NOVOS:NENHUM",
    f"O6 (PROVA B/DELTA) nenhum suspeito NOVO alem do baseline pydantic+contratos "
    f"entra em sys.modules ao importar app.v2.triagem - mede o delta, nao a "
    f"presenca absoluta (suspeitos={_SUSPEITOS_IMPORT}, saida={_saida_subproc!r}, "
    f"stderr={_resultado_subproc.stderr!r})",
)


# === GRUPO P - regras que D-4/D-5/D-6 EXCLUEM (nao podem ter sido criadas) =
# "D-4, D-5 e D-6 sao EXAUSTIVAS. Nenhuma outra regra de validade por campo
# entra em avaliar_triagem()." Cada caso abaixo falharia se a regra proibida
# correspondente tivesse sido implementada por conta propria.

caso(
    "P1 email='banana' e VALIDO - sem validacao de formato de e-mail",
    td(email="banana", destinos=[dst("Atacama", dias=5)], datas_definidas=False),
    [],
)
caso(
    "P2 email='@' e VALIDO - string nao-vazia basta",
    td(email="@", destinos=[dst("Atacama", dias=5)], datas_definidas=False),
    [],
)
caso(
    "P3 nome='a' e destino='X' sao VALIDOS - sem comprimento minimo",
    td(nome="a", destinos=[dst("X", dias=5)], datas_definidas=False),
    [],
)
caso(
    "P4 sem teto numerico de pessoas",
    td(
        total_pessoas=1000000, adultos=999999, criancas=1,
        destinos=[dst("Atacama", dias=5)], datas_definidas=False,
    ),
    [],
)
caso(
    "P5 sem teto numerico de dias",
    td(destinos=[dst("Atacama", dias=100000)], datas_definidas=False),
    [],
)
caso(
    "P6 sem teto de quantidade de destinos",
    td(destinos=[dst(f"Destino {i}", dias=1) for i in range(50)], datas_definidas=False),
    [],
)
# Data no passado acoplaria o resultado ao relogio e quebraria a promessa de
# funcao pura - alem de invalidar fixtures conforme a data de execucao.
caso(
    "P7 data claramente no passado continua VALIDA - sem regra de data futura",
    td(
        destinos=[dst("Atacama", data_inicio=date(1990, 1, 1), data_fim=date(1990, 1, 10))],
        datas_definidas=True,
    ),
    [],
)
caso(
    "P8 passeio de um dia no passado continua VALIDO",
    td(
        destinos=[dst("Atacama", data_inicio=date(1990, 1, 1), data_fim=date(1990, 1, 1))],
        datas_definidas=True,
    ),
    [],
)


# === GRUPO Q - checks globais sobre TODOS os casos avaliados ===============
# Defesa ADICIONAL, aplicada por cima das listas exatas acima - nunca no lugar
# delas.

check(len(_AVALIACOES) > 0, f"Q0 houve casos avaliados para os checks globais (n={len(_AVALIACOES)})")

# Q1 - o retorno respeita o contrato da Fase 1.
_TIPO_ERRADO = [rot for rot, res, _ in _AVALIACOES if not isinstance(res, ResultadoTriagem)]
check(
    _TIPO_ERRADO == [],
    f"Q1 todo retorno de avaliar_triagem e um ResultadoTriagem (fora do tipo={_TIPO_ERRADO})",
)
_NAO_STR = [
    rot for rot, res, _ in _AVALIACOES
    if not all(isinstance(item, str) for item in res.campos_faltantes)
]
check(_NAO_STR == [], f"Q2 campos_faltantes so contem str em todo caso (fora do tipo={_NAO_STR})")

# Q3 - D-8: identificador ja presente nao e duplicado, em nenhum caso.
_COM_DUPLICATA = [
    (rot, list(res.campos_faltantes))
    for rot, res, _ in _AVALIACOES
    if len(res.campos_faltantes) != len(set(res.campos_faltantes))
]
check(_COM_DUPLICATA == [], f"Q3 nenhum caso repete identificador em campos_faltantes ({_COM_DUPLICATA})")

# Q4 - gramatica fechada dos identificadores (D-1 + D-2). Vocabulario, nao
# formato: asserir `^[a-z_]+$` provaria formato e deixaria passar qualquer
# nome inventado.
_RE_BASE = re.compile(r"^(nome|email|destinos|total_pessoas|adultos|criancas|datas_definidas)$")
_RE_POR_DESTINO = re.compile(r"^destinos\[\d+\]\.(data_inicio|data_fim|dias)$")
_FORA_DA_GRAMATICA = sorted(
    {
        item
        for _, res, _ in _AVALIACOES
        for item in res.campos_faltantes
        if not (_RE_BASE.match(item) or _RE_POR_DESTINO.match(item))
    }
)
check(
    _FORA_DA_GRAMATICA == [],
    f"Q4 todo identificador emitido bate a gramatica fechada de D-1/D-2 "
    f"(fora={_FORA_DA_GRAMATICA})",
)

# Q5 - o conteudo entre colchetes e sempre so digitos. Pega injecao de texto
# do cliente mesmo num destino que a gramatica de Q4 aceitaria por acaso.
_RE_COLCHETE = re.compile(r"^destinos\[([^\]]*)\]")
_COLCHETE_NAO_NUMERICO = sorted(
    {
        item
        for _, res, _ in _AVALIACOES
        for item in res.campos_faltantes
        if (_m := _RE_COLCHETE.match(item)) and not _m.group(1).isdigit()
    }
)
check(
    _COLCHETE_NAO_NUMERICO == [],
    f"Q5 o N de todo identificador por destino e so digitos, nunca texto do cliente "
    f"(fora={_COLCHETE_NAO_NUMERICO})",
)

# Q6 - todo N emitido e um indice existente na lista avaliada daquele caso.
_INDICE_FORA_DA_FAIXA = sorted(
    {
        (rot, item)
        for rot, res, dados in _AVALIACOES
        for item in res.campos_faltantes
        for n in indices_emitidos([item])
        if not 0 <= n < len(dados.destinos)
    }
)
check(
    _INDICE_FORA_DA_FAIXA == [],
    f"Q6 todo N emitido e indice existente em dados.destinos ({_INDICE_FORA_DA_FAIXA})",
)

# Q7 - pseudo-campos proibidos por D-3, e vocabulario que D-1/D-2 nao contem.
# Comparacao por PERTINENCIA exata, nao substring: "dias" e substring
# legitima de "destinos[0].dias".
_PSEUDO_CAMPOS_PROIBIDOS = [
    "soma_incoerente", "contagem_invalida", "erro_total", "modo_misto",
    "datas", "dias", "periodo", "data_inicio", "data_fim",
    "quantidade_dias_pretendida", "destino",
]
_PSEUDO_EMITIDOS = sorted(
    {
        (rot, pseudo)
        for rot, res, _ in _AVALIACOES
        for pseudo in _PSEUDO_CAMPOS_PROIBIDOS
        if pseudo in res.campos_faltantes
    }
)
check(
    _PSEUDO_EMITIDOS == [],
    f"Q7 nenhum pseudo-campo de invariante nem vocabulario fora de D-1/D-2 e emitido "
    f"({_PSEUDO_EMITIDOS})",
)

# Q8 - `destinos[N].destino` nao existe: no caso parcial quem sinaliza o item
# sem identidade e o proprio `destinos` (Decisao 3).
_COM_CAMPO_DESTINO = sorted(
    {
        item
        for _, res, _ in _AVALIACOES
        for item in res.campos_faltantes
        if item.startswith("destinos[") and item.endswith(".destino")
    }
)
check(
    _COM_CAMPO_DESTINO == [],
    f"Q8 nenhum caso emite destinos[N].destino ({_COM_CAMPO_DESTINO})",
)


print()
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
