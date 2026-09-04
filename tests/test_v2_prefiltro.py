"""
BIA-V2 Fase 3 - pre-filtro deterministico de conversas/app/v2/prefiltro.py.

Trava `avaliar_prefiltro(lote: list[MensagemLote]) -> DecisaoPrefiltro` e o
contrato LOCAL `MensagemLote`: a matriz obrigatoria A-N da FASE 3 do plano e as
decisoes P-1 a P-9 do subgate de especificacao (2026-09-02). Fonte normativa
unica: docs/superpowers/plans/2026-08-29-bia-v2.md, secao "FASE 3 - Pre-filtro".
Nenhuma regra aqui vem da V1: o par `reacao_emoji_isolada`/`mensagem_com_conteudo`
do n8n e V1 e NAO vincula (P-8), e o regex `\\p{Extended_Pictographic}` do n8n nao
e portavel para a stdlib (P-6).

OS CASOS QUE REALMENTE SEPARAM IMPLEMENTACAO CERTA DE ERRADA:

1. STRING-MATCH (grupos C/D). `msg_type="text", content="Reação: \U0001F44D"` e
   `msg_type="text", content="Sticker"` CONTINUAM. Sao os textos sinteticos que a
   propria V1 escreve para reaction/sticker (conversas/app/routers/webhook.py:531
   e :537), entao qualquer `content.startswith("Reação:")` ou
   `content == "Sticker"` passa nos casos faceis e morre nestes dois. P-4: o tipo
   e a fonte, nunca o conteudo.

2. ALLOWLIST, NUNCA FAIXA DE BLOCO (grupo G). `\U0001F13D\U0001F130\U0001F13E`
   (squared latin) escreve "NAO", `\U0001F1F4\U0001F1F0` (par de regional
   indicator) renderiza "OK" e `✓` e um visto de resposta - os tres estao
   dentro dos blocos que uma leitura por faixa chamaria de emoji, e os tres sao
   palavras da regra de regressao de P-5. Descarta-los seria falso positivo:
   cliente ignorado em silencio.

3. KEYCAP E SUBSTANTIVO (decidido 2026-09-02, grupo G). `1️⃣`,
   `2️⃣0️⃣...` e keycap misturado com emoji CONTINUAM -
   keycap codifica quantidade de pessoas, ano e orcamento.

4. SCANNER DE SEQUENCIAS, NAO TESTE DE PERTENCIMENTO A CONJUNTO (grupos F/G/O).
   `\U0001F44D⃣`, `\U0001F44D‍` (ZWJ final) e `️\U0001F44D` (VS16
   inicial) tem todos os codepoints "conhecidos" e mesmo assim CONTINUAM, porque
   a sequencia nao fecha. O grupo O usa entrada longa terminada em caractere
   invalido: e a forma de ReDoS que P-6 manda evitar varrendo indice em vez de
   regex com quantificador aninhado.

5. FAIL-OPEN (P-3, grupos I/J). Tipo desconhecido, midia e `interactive`
   continuam MESMO com `content=""` - o unico ramo em que conteudo vazio
   significa ignorar e `msg_type == "text"`, exatamente.

FORMA DA ASSERCAO: `ignorar is True/False` (identidade, nao valor verdade) mais
a bicondicional de P-8 em TODO caso - `ignorar is True <=> motivo ==
"lote_sem_conteudo_substantivo"`, `ignorar is False <=> motivo is None`. A
bicondicional e obrigacao de teste porque o contrato nao a aplica:
`DecisaoPrefiltro(ignorar=False, motivo="x")` constroi sem erro hoje.

FONTE 100% ASCII, E OS ROTULOS TAMBEM. Todo pictograma entra por escape
(`"\\U0001F44D"`) e todo `content` vai ao `print` por `rot()`, que devolve
`ascii()` + codepoints. Com stdout redirecionado (pipe, arquivo, CI) o Windows
usa a codepage local: um unico emoji cru no rotulo levantaria UnicodeEncodeError
dentro do `check` e o arquivo morreria antes de reportar a falha real.

Roda standalone:  python tests/test_v2_prefiltro.py
"""
import ast
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Conversas - o literal abaixo tambem e o discriminador de job do CI
# (.github/workflows/test.yml separa as suites com grep -l CONVERSAS_DIR).
CONVERSAS_DIR = ROOT / "conversas"

sys.path.insert(0, str(CONVERSAS_DIR))

failures = []
executados = []


def check(cond, msg):
    executados.append(msg)
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


from pydantic import BaseModel, ValidationError  # noqa: E402

# `contratos` ANTES de `prefiltro`, de proposito: enquanto prefiltro.py nao
# existir (TDD), o vermelho precisa ser ModuleNotFoundError de app.v2.prefiltro
# com os contratos ja carregados - prova de que o ambiente, o sys.path e o
# pacote `app` de conversas/ estao certos, e que falta so a implementacao.
from app.v2.contratos import DecisaoPrefiltro  # noqa: E402

from app.v2.prefiltro import MensagemLote, avaliar_prefiltro  # noqa: E402


# --- vocabulario e codepoints ---------------------------------------------
# P-8: o UNICO valor nao-nulo que a Fase 3 produz.
MOTIVO_CANONICO = "lote_sem_conteudo_substantivo"

# Allowlist inicial de P-6, semeada pelas fixtures aprovadas e so por elas.
POLEGAR = "\U0001F44D"          # U+1F44D THUMBS UP SIGN
CORACAO = "❤"              # U+2764 HEAVY BLACK HEART
ORACAO = "\U0001F64F"           # U+1F64F PERSON WITH FOLDED HANDS
CHECK_BRANCO = "✅"         # U+2705 WHITE HEAVY CHECK MARK
HOMEM = "\U0001F468"            # U+1F468 MAN
MULHER = "\U0001F469"           # U+1F469 WOMAN
MENINA = "\U0001F467"           # U+1F467 GIRL
MENINO = "\U0001F466"           # U+1F466 BOY
ESCULAPIO = "⚕"            # U+2695 STAFF OF AESCULAPIUS
ALLOWLIST_BASES = [
    ("polegar", POLEGAR), ("coracao", CORACAO), ("maos em oracao", ORACAO),
    ("check branco", CHECK_BRANCO), ("homem", HOMEM), ("mulher", MULHER),
    ("menina", MENINA), ("menino", MENINO), ("bastao de esculapio", ESCULAPIO),
]

# Componentes de sequencia (nunca base isolada, por P-6).
VS16 = "️"                 # U+FE0F VARIATION SELECTOR-16
ZWJ = "‍"                  # U+200D ZERO WIDTH JOINER
MOD_TOM_4 = "\U0001F3FD"        # U+1F3FD EMOJI MODIFIER FITZPATRICK TYPE-4
KEYCAP = "⃣"               # U+20E3 COMBINING ENCLOSING KEYCAP
ZWSP = "​"                 # U+200B ZERO WIDTH SPACE (nao e isspace())

# Regional indicators: so o PAR allowlistado e bandeira.
RI_B = "\U0001F1E7"
RI_R = "\U0001F1F7"
RI_N = "\U0001F1F3"
RI_O = "\U0001F1F4"
RI_K = "\U0001F1F0"
BANDEIRA_BR = RI_B + RI_R       # par allowlistado

# Sequencias compostas nomeadas pela matriz F.
FAMILIA = HOMEM + ZWJ + MULHER + ZWJ + MENINA + ZWJ + MENINO
PROFISSAO = MULHER + ZWJ + ESCULAPIO + VS16

# Pictogramas FORA da allowlist - custo operacional aceito por P-6.
ESTRELA = "⭐"              # U+2B50 fixture-ancora do grupo G
VISTO = "✓"                # U+2713 CHECK MARK
BALLOT_X = "✗"             # U+2717 BALLOT X
NAO_QUADRADO = "\U0001F13D\U0001F130\U0001F13E"   # squared latin = "NAO"
RISOS = "\U0001F602"
MAOS_ERGUIDAS = "\U0001F64C"
PALMAS = "\U0001F44F"
APAIXONADO = "\U0001F60D"

# Keycaps - SUBSTANTIVOS pela decisao de 2026-09-02.
KC_0 = "0" + VS16 + KEYCAP
KC_1 = "1" + VS16 + KEYCAP
KC_2 = "2" + VS16 + KEYCAP

# PII canonica das fixtures adversariais (mesma de test_v2_eventos_validacao.py).
PII_TELEFONE = "5551999999999"
PII_EMAIL = "joao@example.com"

# Textos sinteticos que a propria V1 escreve (webhook.py:518-544).
V1_REACAO = "Reação: " + POLEGAR
V1_STICKER = "Sticker"
V1_LOCALIZACAO = "Localização: -22, -43"
V1_CONTATO = "Contato compartilhado"
V1_BOTAO = "[Botão]"


# --- infraestrutura dos casos ---------------------------------------------
def _cps(texto):
    """Codepoints por extenso, no formato da matriz F/G do plano."""
    return " ".join("U+%04X" % ord(c) for c in texto)


def rot(texto):
    """Rotulo ASCII-SEGURO de um `content`. Ver o cabecalho do arquivo."""
    if len(texto) > 24:
        return "<%d chars: %s ... %s>" % (len(texto), _cps(texto[:3]), _cps(texto[-3:]))
    return "%s [%s]" % (ascii(texto), _cps(texto))


def m(msg_type, content):
    """`MensagemLote` NOVA a cada chamada.

    Nunca reaproveitar a MESMA instancia entre casos: o Pydantic v2 nao
    revalida instancia de modelo (`revalidate_instances` e `never` por
    default), entao o objeto seria compartilhado de verdade e uma mutacao
    indevida em `avaliar_prefiltro` vazaria de um caso para o outro,
    mascarando ou espalhando a falha (mesma razao de `dst()` em
    tests/test_v2_triagem.py).
    """
    return MensagemLote(msg_type=msg_type, content=content)


_DECISOES = []  # (rotulo, decisao, lote) - reusado pelos checks globais


def caso(rotulo, lote, ignorar_esperado):
    """Avalia `lote` e faz DUAS afirmacoes: a decisao esperada e a bicondicional
    de P-8. A bicondicional e comparada contra o `ignorar` REAL, nao contra o
    esperado: se a decisao ja estiver errada, o segundo check ainda reporta se o
    motivo esta coerente com o que a funcao devolveu.
    """
    try:
        decisao = avaliar_prefiltro(lote)
    except Exception as exc:  # noqa: BLE001 - relatar o tipo errado, nao mascarar
        check(False, f"{rotulo}: avaliar_prefiltro levantou {type(exc).__name__}: {ascii(exc)}")
        check(False, f"{rotulo}: P-8 nao pode ser avaliada (a chamada levantou)")
        return None
    _DECISOES.append((rotulo, decisao, lote))
    check(
        decisao.ignorar is ignorar_esperado,
        f"{rotulo}: ignorar is {ignorar_esperado} (obtido={ascii(decisao.ignorar)})",
    )
    if decisao.ignorar is True:
        bicond = decisao.motivo == MOTIVO_CANONICO
        esperado = ascii(MOTIVO_CANONICO)
    elif decisao.ignorar is False:
        bicond = decisao.motivo is None
        esperado = "None"
    else:
        bicond = False
        esperado = "<ignorar nao e bool>"
    check(
        bicond,
        f"{rotulo}: P-8 bicondicional - com ignorar={ascii(decisao.ignorar)} o motivo "
        f"tem de ser {esperado} (obtido={ascii(decisao.motivo)})",
    )
    return decisao


def levanta(fn, label):
    """Chama fn() e confirma que levanta ValidationError - nunca Exception generica."""
    try:
        fn()
    except ValidationError:
        check(True, label)
    except Exception as exc:  # noqa: BLE001 - relatar o tipo errado, nao mascarar
        check(False, f"{label} (levantou {type(exc).__name__}, nao ValidationError: {ascii(exc)})")
    else:
        check(False, f"{label} (nao levantou)")


def constroi(fn, label):
    """Confirma que fn() constroi sem ValidationError."""
    try:
        fn()
    except ValidationError as exc:
        check(False, f"{label} (levantou ValidationError: {ascii(exc)})")
    except Exception as exc:  # noqa: BLE001
        check(False, f"{label} (levantou {type(exc).__name__}: {ascii(exc)})")
    else:
        check(True, label)


# === GRUPO A - contrato MensagemLote (P-1, P-2) ===========================
# "Dois campos, exatamente." Nada de message_id, whatsapp_msg_id,
# conversation_id, telefone, nome, timestamp, created_at, media_url, alvo de
# reacao, caption separado ou metadados da Meta. A funcao precisa saber so duas
# coisas: QUE TIPO de mensagem e, e QUAL CONTEUDO textual ja esta disponivel.

check(
    isinstance(MensagemLote, type) and issubclass(MensagemLote, BaseModel),
    "A01 MensagemLote e um BaseModel do Pydantic",
)

_campos = set(MensagemLote.model_fields.keys())
check(
    _campos == {"msg_type", "content"},
    f"A02 MensagemLote tem EXATAMENTE os campos msg_type e content "
    f"(encontrados={sorted(_campos)})",
)

for _campo in ("msg_type", "content"):
    _f = MensagemLote.model_fields.get(_campo)
    check(
        _f is not None and _f.is_required(),
        f"A03 '{_campo}' e obrigatorio e nao tem default",
    )

levanta(
    lambda: MensagemLote(content="oi"),
    "A04 MensagemLote sem 'msg_type' levanta ValidationError",
)
levanta(
    lambda: MensagemLote(msg_type="text"),
    "A05 MensagemLote sem 'content' levanta ValidationError",
)
levanta(
    lambda: MensagemLote(),
    "A06 MensagemLote sem nenhum campo levanta ValidationError",
)

# extra="forbid" por comportamento, campo a campo dos que P-1 nomeia.
for _extra, _valor in [
    ("message_id", "wamid.HBgM"),
    ("whatsapp_msg_id", "wamid.HBgM"),
    ("conversation_id", 7),
    ("media_url", "https://example.com/x.jpg"),
    ("timestamp", "1756800000"),
    ("created_at", "2026-09-02T00:00:00"),
    ("campo_desconhecido_qualquer", "x"),
]:
    levanta(
        lambda _e=_extra, _v=_valor: MensagemLote(msg_type="text", content="oi", **{_e: _v}),
        f"A07 MensagemLote rejeita o campo extra '{_extra}' (extra='forbid')",
    )

check(
    MensagemLote.model_config.get("extra") == "forbid",
    f"A08 model_config declara extra='forbid' "
    f"(obtido={ascii(MensagemLote.model_config.get('extra'))})",
)

# StrictStr EFETIVO: sem coercao de int, bool, bytes; None nao e aceito em
# nenhum dos dois (diferente de `motivo` em DecisaoPrefiltro, que e anulavel).
for _campo, _outro in (("msg_type", "content"), ("content", "msg_type")):
    for _rotulo_v, _valor in [
        ("int", 1), ("bool", True), ("None", None), ("bytes", b"oi"), ("float", 1.5),
    ]:
        levanta(
            lambda _c=_campo, _o=_outro, _v=_valor: MensagemLote(**{_c: _v, _o: "text"}),
            f"A09 StrictStr: '{_campo}' = {_rotulo_v} levanta ValidationError",
        )

constroi(
    lambda: MensagemLote(msg_type="text", content=""),
    "A10 content='' constroi (conteudo vazio e fato normal de midia e de texto)",
)
constroi(
    lambda: MensagemLote(msg_type="future_meta_type", content=""),
    "A11 msg_type='future_meta_type' constroi (P-1: nao e enum nem Literal)",
)
constroi(
    lambda: MensagemLote(msg_type="", content=""),
    "A12 msg_type='' constroi (P-1 nao impoe min_length; tipo vazio e tipo desconhecido)",
)

check(
    MensagemLote.__module__ == "app.v2.prefiltro",
    f"A13 (P-2) MensagemLote e definida em app.v2.prefiltro, nao em contratos.py "
    f"(obtido={ascii(MensagemLote.__module__)})",
)

_BASES_CONTRATOS = [b.__name__ for b in MensagemLote.__mro__ if b.__module__ == "app.v2.contratos"]
check(
    _BASES_CONTRATOS == [],
    f"A14 (P-1) MensagemLote herda BaseModel direto, nao _ContratoV2 - nenhuma "
    f"base vem de app.v2.contratos (encontradas={_BASES_CONTRATOS})",
)

_dump_a = MensagemLote(msg_type="text", content="oi").model_dump()
check(
    _dump_a == {"msg_type": "text", "content": "oi"},
    f"A15 model_dump devolve exatamente as duas chaves (obtido={ascii(_dump_a)})",
)


# === GRUPO B - lote vazio (P-7.1) =========================================
_d_b = caso("B01 lote vazio []", [], True)
check(
    _d_b is not None and _d_b.motivo == "lote_sem_conteudo_substantivo",
    f"B02 o motivo do lote vazio e o literal canonico "
    f"(obtido={ascii(_d_b.motivo) if _d_b is not None else '<sem decisao>'})",
)


# === GRUPO C - reacao (P-4) ===============================================
caso("C01 reacao isolada, com o content sintetico da V1", [m("reaction", V1_REACAO)], True)
caso("C02 reacao isolada com content=''", [m("reaction", "")], True)
caso("C03 duas reacoes", [m("reaction", V1_REACAO), m("reaction", "Reação: " + CHECK_BRANCO)], True)
caso(
    "C04 reacao + texto substantivo",
    [m("reaction", V1_REACAO), m("text", "Somos 3 pessoas")],
    False,
)
# *** killer de string-match: o cliente escreveu o texto que a V1 usa para reacao
caso(
    f"C05 *** msg_type='text' com content={rot(V1_REACAO)} (o cliente digitou)",
    [m("text", V1_REACAO)],
    False,
)
caso(
    "C06 msg_type='text' com content='Rea\\xe7\\xe3o:' (prefixo puro)",
    [m("text", "Reação:")],
    False,
)
caso(
    "C07 reacao com content substantivo continua sendo IGNORAVEL (o tipo e a fonte)",
    [m("reaction", "Quero viajar em janeiro")],
    True,
)


# === GRUPO D - sticker (P-4) ==============================================
caso("D01 sticker isolado, com o content sintetico da V1", [m("sticker", V1_STICKER)], True)
caso("D02 sticker isolado com content=''", [m("sticker", "")], True)
caso("D03 sticker + reacao", [m("sticker", V1_STICKER), m("reaction", V1_REACAO)], True)
caso(
    "D04 sticker + texto substantivo",
    [m("sticker", V1_STICKER), m("text", "Quero viajar em janeiro")],
    False,
)
# *** killer de string-match
caso("D05 *** msg_type='text' com content='Sticker' (o cliente digitou)", [m("text", V1_STICKER)], False)
caso("D06 msg_type='text' com content='sticker' minusculo", [m("text", "sticker")], False)
caso(
    "D07 sticker com content substantivo continua sendo IGNORAVEL (o tipo e a fonte)",
    [m("sticker", "Somos 3 pessoas")],
    True,
)


# === GRUPO E - texto vazio (P-5) ==========================================
# `content.strip() == ""` e ignoravel SO para msg_type == "text".
_VAZIOS = ["", " ", "   ", "\t", "\n", "\t \n", "\r\n", "\n\n \t "]
for _i, _c in enumerate(_VAZIOS, start=1):
    caso(f"E{_i:02d} text vazio {rot(_c)}", [m("text", _c)], True)


# === GRUPO F - emoji-only que IGNORA (P-6) ================================
# So para msg_type == "text". Cada codepoint da allowlist tem caso que o
# exercita sozinho (o plano exige isso explicitamente para U+2705), e cada
# producao da gramatica tem caso: base sozinha, base+VS16, base+modifier,
# cadeia por ZWJ com slot proprio de VS16, par de bandeira allowlistado,
# multiplas sequencias e whitespace ao redor.
_FIXTURES_F = []
for _nome, _base in ALLOWLIST_BASES:
    _FIXTURES_F.append((f"base allowlistada sozinha ({_nome})", _base))
_FIXTURES_F += [
    ("coracao+VS16+oracao (fixture aprovada)", CORACAO + VS16 + ORACAO),
    ("polegar+VS16 (producao base + VS16)", POLEGAR + VS16),
    ("polegar+modifier de tom de pele", POLEGAR + MOD_TOM_4),
    ("familia por ZWJ", FAMILIA),
    ("profissao por ZWJ, com VS16 no slot da 2a base", PROFISSAO),
    ("bandeira BR (par allowlistado)", BANDEIRA_BR),
    ("bandeira BR duas vezes (duas sequencias)", BANDEIRA_BR + BANDEIRA_BR),
    ("polegar com whitespace ao redor", "  " + POLEGAR + "  "),
    ("polegar e oracao separados por espaco", POLEGAR + " " + ORACAO),
    ("polegar e oracao adjacentes, sem espaco", POLEGAR + ORACAO),
    ("polegar entre tab e newline", "\t" + POLEGAR + "\n"),
    ("bandeira BR com whitespace ao redor", " " + BANDEIRA_BR + " "),
    ("familia + espaco + polegar", FAMILIA + " " + POLEGAR),
    ("polegar com tom + espaco + check branco", POLEGAR + MOD_TOM_4 + " " + CHECK_BRANCO),
]
for _i, (_desc, _c) in enumerate(_FIXTURES_F, start=1):
    caso(f"F{_i:02d} {_desc} {rot(_c)}", [m("text", _c)], True)


# === GRUPO G - NAO emoji-only: CONTINUA (P-3, P-5, P-6) ===================
# "Codepoint fora da allowlist ou sequencia nao reconhecida => SUBSTANTIVO."
_FIXTURES_G = [
    ("digito", "1"),
    ("letra", "a"),
    ("polegar + exclamacao", POLEGAR + "!"),
    ("texto + polegar", "oi " + POLEGAR),
    ("check branco + texto", CHECK_BRANCO + " ok"),
    ("estrela U+2B50 - fixture-ancora fora da allowlist", ESTRELA),
    ("squared latin = NAO (o caso que mata a leitura por faixa)", NAO_QUADRADO),
    ("visto U+2713 como resposta sim", VISTO),
    ("ballot X U+2717 como resposta nao", BALLOT_X),
    ("par de RI nao allowlistado = OK", RI_O + RI_K),
    ("par de RI nao allowlistado = NO", RI_N + RI_O),
    ("RI impar: um regional indicator sozinho", RI_B),
    ("RI impar: tres regional indicators", RI_B + RI_R + RI_B),
    ("polegar + keycap solto U+20E3", POLEGAR + KEYCAP),
    ("polegar + ZWJ final (sequencia nao fecha)", POLEGAR + ZWJ),
    ("VS16 inicial + polegar", VS16 + POLEGAR),
    ("ZWJ inicial + polegar", ZWJ + POLEGAR),
    ("ZWJ sozinho", ZWJ),
    ("VS16 sozinho", VS16),
    ("modifier de tom sozinho (so vale como componente)", MOD_TOM_4),
    ("dois modifiers na mesma base (o slot e de UM)", POLEGAR + MOD_TOM_4 + MOD_TOM_4),
    ("U+200B nao e whitespace por str.isspace()", ZWSP),
    ("polegar + U+200B", POLEGAR + ZWSP),
    ("keycap isolado 1", KC_1),
    ("keycap sem VS16 (U+0031 U+20E3)", "1" + KEYCAP),
    ("multi-keycap 2000", KC_2 + KC_0 + KC_0 + KC_0),
    ("multi-keycap 2027", KC_2 + KC_0 + KC_2 + "7" + VS16 + KEYCAP),
    ("keycap de hash", "#" + VS16 + KEYCAP),
    ("keycap de asterisco", "*" + VS16 + KEYCAP),
    ("polegar + keycap", POLEGAR + " " + KC_2),
    ("keycap + polegar", KC_2 + " " + POLEGAR),
    ("risos U+1F602 (custo operacional aceito)", RISOS),
    ("maos erguidas U+1F64C (custo operacional aceito)", MAOS_ERGUIDAS),
    ("palmas U+1F44F (custo operacional aceito)", PALMAS),
    ("apaixonado U+1F60D (custo operacional aceito)", APAIXONADO),
    ("polegar allowlistado + risos fora da allowlist", POLEGAR + RISOS),
]
for _i, (_desc, _c) in enumerate(_FIXTURES_G, start=1):
    caso(f"G{_i:02d} {_desc} {rot(_c)}", [m("text", _c)], False)


# === GRUPO H - as palavras que NUNCA podem ser auto-ignoradas (P-5) =======
# Regra de regressao da aprovacao original: nenhuma delas vira descarte
# silencioso. Nao existe heuristica de "texto curto" nem whitelist operacional.
_PALAVRAS_H = [
    ("oito palavras: ok", "ok"),
    ("oito palavras: valeu", "valeu"),
    ("oito palavras: obrigado", "obrigado"),
    ("oito palavras: sim", "sim"),
    ("oito palavras: nao", "não"),
    ("oito palavras: beleza", "beleza"),
    ("oito palavras: perfeito", "perfeito"),
    ("oito palavras: entendi", "entendi"),
    ("variante OK maiusculo", "OK"),
    ("variante Ok capitalizado", "Ok"),
    ("obrigado com exclamacao", "obrigado!"),
    ("nao com ponto", "não."),
    ("valeu + polegar", "valeu " + POLEGAR),
    ("ok + polegar", "ok " + POLEGAR),
    ("sim + check branco", "sim " + CHECK_BRANCO),
]
for _i, (_desc, _c) in enumerate(_PALAVRAS_H, start=1):
    caso(f"H{_i:02d} {_desc} {rot(_c)}", [m("text", _c)], False)

caso(
    "H16 'ok' junto de reacao e sticker (o lote inteiro continua)",
    [m("reaction", V1_REACAO), m("text", "ok"), m("sticker", V1_STICKER)],
    False,
)


# === GRUPO I - tipos conhecidos: sempre CONTINUAM (P-3, P-4) ==============
_TIPOS_I = ["image", "video", "audio", "document", "location", "contacts", "interactive"]
for _i, _t in enumerate(_TIPOS_I, start=1):
    caso(f"I{_i:02d} msg_type='{_t}' com content='' (conteudo vazio NAO ignora fora de text)",
         [m(_t, "")], False)

# Conteudo sintetico REAL da V1 (webhook.py:518-544): fecha a heuristica sobre
# conteudo que os grupos C/D nao alcancam - nem o conteudo tipico do tipo faz o
# ramo mudar.
_CONTEUDO_I = [
    ("image", "[IMAGE]"),
    ("video", "[VIDEO]"),
    ("audio", "[AUDIO]"),
    ("document", "[DOCUMENT]"),
    ("location", V1_LOCALIZACAO),
    ("contacts", V1_CONTATO),
    ("interactive", V1_BOTAO),
]
for _i, (_t, _c) in enumerate(_CONTEUDO_I, start=8):
    caso(f"I{_i:02d} msg_type='{_t}' com o content sintetico da V1 {rot(_c)}",
         [m(_t, _c)], False)

caso("I15 image com content so de whitespace (P-5 vale so para text)", [m("image", "   ")], False)
caso("I16 image com content emoji-only (P-6 vale so para text)", [m("image", POLEGAR)], False)
caso("I17 document com content emoji-only", [m("document", CORACAO + VS16 + ORACAO)], False)
caso(
    "I18 os sete tipos conhecidos no mesmo lote, todos com content=''",
    [m(_t, "") for _t in _TIPOS_I],
    False,
)


# === GRUPO J - tipo desconhecido e comparacao EXATA de msg_type (P-4) =====
# Sem strip(), sem lower(), sem normalizacao Unicode: qualquer variante cai em
# "qualquer outro" e CONTINUA, que e o lado que P-3 exige.
_TIPOS_J = [
    ("tipo futuro da Meta com content=''", "future_meta_type", ""),
    ("tipo futuro da Meta com emoji", "future_meta_type", POLEGAR),
    ("'TEXT' maiusculo nao e 'text'", "TEXT", ""),
    ("'TEXT' maiusculo com emoji-only", "TEXT", POLEGAR),
    ("'Text' capitalizado com whitespace", "Text", "   "),
    ("'text ' com espaco no fim (sem strip)", "text ", ""),
    ("' reaction ' com espacos nao e 'reaction'", " reaction ", V1_REACAO),
    ("'Sticker' capitalizado nao e 'sticker'", "Sticker", V1_STICKER),
    ("'REACTION' maiusculo", "REACTION", ""),
    ("msg_type vazio", "", ""),
    ("msg_type so de whitespace", " ", ""),
]
for _i, (_desc, _t, _c) in enumerate(_TIPOS_J, start=1):
    caso(f"J{_i:02d} {_desc}: msg_type={ascii(_t)} content={rot(_c)}", [m(_t, _c)], False)


# === GRUPO K - semantica do lote (P-7) ====================================
# A funcao decide o LOTE INTEIRO: um unico item substantivo faz tudo continuar,
# em qualquer posicao.
caso("K01 reacao + sticker", [m("reaction", V1_REACAO), m("sticker", V1_STICKER)], True)
caso("K02 emoji + vazio", [m("text", POLEGAR), m("text", "  ")], True)
caso(
    "K03 reacao + sticker + emoji + vazio",
    [m("reaction", V1_REACAO), m("sticker", V1_STICKER), m("text", CORACAO + VS16 + ORACAO), m("text", "\t")],
    True,
)
caso(
    "K04 tres reacoes, dois stickers, tres vazios e dois emojis",
    [
        m("reaction", V1_REACAO), m("reaction", ""), m("reaction", "Reação: " + CHECK_BRANCO),
        m("sticker", V1_STICKER), m("sticker", ""),
        m("text", ""), m("text", " "), m("text", "\n"),
        m("text", POLEGAR), m("text", BANDEIRA_BR),
    ],
    True,
)
caso("K05 emoji + texto substantivo", [m("text", POLEGAR), m("text", "Quero viajar em janeiro")], False)
caso("K06 reacao + 'Somos 3 pessoas'", [m("reaction", V1_REACAO), m("text", "Somos 3 pessoas")], False)
caso(
    "K07 dois substantivos",
    [m("text", "Quero viajar em janeiro"), m("text", "Somos 3 pessoas")],
    False,
)
caso("K08 tipo desconhecido + emoji", [m("future_meta_type", ""), m("text", POLEGAR)], False)
caso("K09 image com content='' + emoji", [m("image", ""), m("text", POLEGAR)], False)
caso(
    "K10 caso aprovado original: polegar + 'Quero viajar em janeiro'",
    [m("text", POLEGAR), m("text", "Quero viajar em janeiro")],
    False,
)
caso(
    "K11 substantivo PRIMEIRO, ignoraveis depois",
    [m("text", "Quero viajar em janeiro"), m("reaction", V1_REACAO), m("text", POLEGAR)],
    False,
)
caso(
    "K12 substantivo NO MEIO",
    [m("text", POLEGAR), m("text", "Quero viajar em janeiro"), m("sticker", V1_STICKER)],
    False,
)
caso(
    "K13 substantivo POR ULTIMO",
    [m("reaction", V1_REACAO), m("text", "  "), m("sticker", ""), m("text", "Quero viajar em janeiro")],
    False,
)
caso(
    "K14 sticker de verdade + texto 'Sticker' digitado",
    [m("sticker", V1_STICKER), m("text", V1_STICKER)],
    False,
)
caso(
    "K15 reacao de verdade + texto 'Rea\\xe7\\xe3o: ...' digitado",
    [m("reaction", V1_REACAO), m("text", V1_REACAO)],
    False,
)
caso(
    "K16 keycap no meio de ignoraveis (keycap e substantivo)",
    [m("reaction", V1_REACAO), m("text", POLEGAR), m("text", KC_2)],
    False,
)
caso(
    "K17 estrela fora da allowlist no meio de ignoraveis",
    [m("sticker", V1_STICKER), m("text", ESTRELA)],
    False,
)


# === GRUPO L - motivo: um unico valor, sem interpolacao (P-8) =============
# Fixture adversarial: conteudo com cara de PII nos itens IGNORAVEIS. Se a
# implementacao montar o motivo a partir do lote (f-string de conveniencia), o
# telefone ou o e-mail do cliente vaza para o campo que a Fase 4 persiste.
_lote_l1 = [
    m("reaction", "Reação: " + PII_TELEFONE),
    m("sticker", PII_EMAIL),
    m("text", POLEGAR),
    m("text", "   "),
]
_d_l1 = caso("L01 lote ignoravel com PII no content dos itens", _lote_l1, True)
if _d_l1 is not None:
    check(
        _d_l1.motivo == MOTIVO_CANONICO,
        f"L02 o motivo e o literal canonico, byte a byte (obtido={ascii(_d_l1.motivo)})",
    )
    _vazamentos = sorted(
        agulha for agulha in (PII_TELEFONE, PII_EMAIL, "reaction", "sticker", "text", POLEGAR)
        if agulha in (_d_l1.motivo or "")
    )
    check(
        _vazamentos == [],
        f"L03 nem content nem msg_type sao interpolados no motivo "
        f"(vazamentos={ascii(_vazamentos)})",
    )

_d_l4 = caso(
    "L04 lote substantivo com PII: motivo is None (nada a interpolar)",
    [m("text", f"meu telefone e {PII_TELEFONE} e o email {PII_EMAIL}")],
    False,
)

# Checks globais sobre TODOS os casos ja avaliados.
_MOTIVOS = [d.motivo for _, d, _ in _DECISOES]
_NAO_NULOS = sorted({x for x in _MOTIVOS if x is not None})
check(
    _NAO_NULOS in ([], [MOTIVO_CANONICO]),
    f"L05 o unico motivo nao-nulo produzido em TODO o arquivo e o literal canonico "
    f"(encontrados={ascii(_NAO_NULOS)})",
)
check(
    all(x is None or isinstance(x, str) for x in _MOTIVOS),
    "L06 todo motivo e None ou str (nunca outro tipo)",
)
check(
    "" not in _MOTIVOS,
    "L07 motivo nunca e '' (o contrato aceita string vazia, a funcao nao a produz)",
)
_COM_DIGITO_OU_ARROBA = sorted(
    {x for x in _MOTIVOS if x is not None and (any(c.isdigit() for c in x) or "@" in x)}
)
check(
    _COM_DIGITO_OU_ARROBA == [],
    f"L08 nenhum motivo contem digito ou '@' - o literal canonico nao tem nenhum "
    f"dos dois, entao qualquer ocorrencia e conteudo de cliente vazando "
    f"(encontrados={ascii(_COM_DIGITO_OU_ARROBA)})",
)
_TIPO_ERRADO = [r for r, d, _ in _DECISOES if not isinstance(d, DecisaoPrefiltro)]
check(
    _TIPO_ERRADO == [],
    f"L09 avaliar_prefiltro devolve DecisaoPrefiltro em todo caso "
    f"(fora do tipo={_TIPO_ERRADO[:5]})",
)
_IGNORAR_NAO_BOOL = [r for r, d, _ in _DECISOES if d.ignorar is not True and d.ignorar is not False]
check(
    _IGNORAR_NAO_BOOL == [],
    f"L10 `ignorar` e sempre o bool True ou False, nunca um valor truthy "
    f"(fora do tipo={_IGNORAR_NAO_BOOL[:5]})",
)
_ROTULOS = [r for r, _, _ in _DECISOES]
check(
    len(set(_ROTULOS)) == len(_ROTULOS),
    f"L11 nenhum rotulo de caso repetido (um duplicado esconderia um caso nao "
    f"escrito): {len(_ROTULOS)} casos, {len(set(_ROTULOS))} rotulos distintos",
)


# === GRUPO M - nao-mutacao (P-7) ==========================================
# "Nao filtra itens, nao devolve lote transformado, nao remove, nao reordena,
# nao muta." Nao-mutacao e disciplina da funcao, nao garantia do modelo:
# MensagemLote nao e frozen, entao um `item.content = item.content.strip()` de
# conveniencia compilaria - e o lote pertence ao chamador.
_lote_m = [
    m("text", "Quero viajar em janeiro"),
    m("reaction", V1_REACAO),
    m("sticker", V1_STICKER),
    m("future_meta_type", ""),
    m("text", "  \t "),
    m("text", "  " + POLEGAR + "  "),
]
_antes_m = [i.model_dump() for i in _lote_m]
_ids_m = [id(i) for i in _lote_m]
_len_m = len(_lote_m)
caso("M01 lote variado (text, reaction, sticker, tipo desconhecido, whitespace, emoji)", _lote_m, False)
_depois_m = [i.model_dump() for i in _lote_m]
check(
    _antes_m == _depois_m,
    f"M02 avaliar_prefiltro NAO muta nenhum item do lote "
    f"(antes={ascii(_antes_m)}, depois={ascii(_depois_m)})",
)
check(len(_lote_m) == _len_m, f"M03 a lista nao foi encurtada nem estendida (len={len(_lote_m)})")
check(
    [id(i) for i in _lote_m] == _ids_m,
    "M04 nenhum item foi substituido nem reordenado (identidade e posicao preservadas)",
)

avaliar_prefiltro(_lote_m)
avaliar_prefiltro(_lote_m)  # duas chamadas: mutacao acumulativa tambem apareceria
check(
    [i.model_dump() for i in _lote_m] == _antes_m,
    "M05 duas chamadas seguidas no mesmo lote nao acumulam mutacao",
)

# Caminho IGNORAR tambem: e nele que um strip() de conveniencia mora.
_lote_m6 = [m("text", "  " + POLEGAR + "  "), m("text", "\t\n"), m("reaction", V1_REACAO)]
_antes_m6 = [i.model_dump() for i in _lote_m6]
_ids_m6 = [id(i) for i in _lote_m6]
caso("M06 lote todo ignoravel, com whitespace ao redor do emoji", _lote_m6, True)
check(
    [i.model_dump() for i in _lote_m6] == _antes_m6,
    f"M07 o caminho IGNORAR tambem nao muta o lote "
    f"(antes={ascii(_antes_m6)}, depois={ascii([i.model_dump() for i in _lote_m6])})",
)
check([id(i) for i in _lote_m6] == _ids_m6, "M08 o caminho IGNORAR nao reordena nem substitui itens")


# === GRUPO N - pureza (P-9) ===============================================
# Mesmo padrao do GRUPO O de tests/test_v2_triagem.py: AST dos imports DIRETOS
# (nao depende de runtime nem de ambiente) + DELTA de sys.modules medido num
# subprocesso limpo (pega import dinamico, que a analise estatica nao ve).
# Presenca ABSOLUTA em sys.modules nao prova acoplamento e nao e usada aqui.
# Tudo estrutural: nenhum check olha texto de docstring ou comentario, entao
# escrever "sqlalchemy" numa nota explicativa nao derruba o arquivo.
_ARQUIVO_PREFILTRO = CONVERSAS_DIR / "app" / "v2" / "prefiltro.py"
_CODIGO_PREFILTRO = _ARQUIVO_PREFILTRO.read_text(encoding="utf-8")
_ARVORE = ast.parse(_CODIGO_PREFILTRO)

# PROVA A - imports diretos por AST. `ast.walk` percorre a arvore INTEIRA,
# entao pega import escondido dentro de funcao, `if` ou `try`. A raiz vem de
# `alias.name`, nao de `asname`: `import sqlalchemy as sa` nao escapa.
_RAIZES_PERMITIDAS = {
    "__future__", "typing", "collections", "dataclasses", "enum", "re",
    "unicodedata", "string", "itertools", "functools",
    # `pydantic` e permitido AQUI e nao era no teste da Fase 2: P-1 manda o
    # proprio `prefiltro.py` DEFINIR `MensagemLote`, entao ele precisa de
    # `BaseModel`/`ConfigDict`/`StrictStr` diretamente. `triagem.py` nao define
    # modelo nenhum e por isso nunca importou pydantic. A alternativa — puxar
    # esses nomes via `app.v2.contratos` — acoplaria a um detalhe de import
    # daquele modulo, que nao os exporta como API publica.
    "pydantic",
    "app",  # so `app.v2.contratos`, restringido pelo check N02 logo abaixo
}
# Modulos-alvo da regra de pureza, escritos por extenso: infra da aplicacao,
# ORM, framework web, clientes HTTP (CRM e n8n saem por httpx/requests), os
# eventos persistentes (que arrastam model, database e config), o filesystem,
# o ambiente e o relogio.
_MODULOS_PROIBIDOS = (
    "sqlalchemy", "fastapi", "starlette",
    "httpx", "requests", "aiohttp", "urllib", "http", "socket",
    "sqlite3", "psycopg2", "asyncpg", "n8n",
    "os", "pathlib", "shutil", "tempfile", "glob", "subprocess",
    "datetime", "time",
    "app.database", "app.models", "app.services", "app.config", "app.auth",
    "app.main", "app.schemas", "app.routers", "app.v2.eventos",
)


def _modulos_importados(arvore):
    """Nome completo de todo import direto do arquivo (via AST).

    Import relativo vira `"." * level + module`, para que `from . import x` e
    `from ..models import y` sejam distinguiveis de um import absoluto.
    """
    nomes = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            for alias in no.names:
                nomes.add(alias.name)
        elif isinstance(no, ast.ImportFrom):
            nomes.add("." * no.level + (no.module or ""))
    return nomes


_NOMES_IMPORTADOS = _modulos_importados(_ARVORE)
_ABSOLUTOS = {n for n in _NOMES_IMPORTADOS if not n.startswith(".")}
_RELATIVOS = {n for n in _NOMES_IMPORTADOS if n.startswith(".")}
_RAIZES = {n.split(".")[0] for n in _ABSOLUTOS}

check(
    _RAIZES <= _RAIZES_PERMITIDAS,
    f"N01 (PROVA A/AST) imports diretos de prefiltro.py vem so de "
    f"{sorted(_RAIZES_PERMITIDAS)} (encontradas={sorted(_RAIZES)})",
)

# A raiz `app` sozinha seria fraca demais: cobre `app.database` tanto quanto
# `app.v2.contratos`. Por isso o caminho completo tambem e travado.
_APP_IMPORTS = {n for n in _ABSOLUTOS if n == "app" or n.startswith("app.")}
check(
    _APP_IMPORTS <= {"app.v2.contratos"},
    f"N02 unico import de `app` permitido e `app.v2.contratos` "
    f"(encontrados={sorted(_APP_IMPORTS)})",
)
check(
    _RELATIVOS <= {".contratos", "..v2.contratos"},
    f"N03 unico import relativo permitido aponta para contratos "
    f"(encontrados={sorted(_RELATIVOS)})",
)

_PROIBIDOS_ENCONTRADOS = sorted(
    proibido
    for proibido in _MODULOS_PROIBIDOS
    for nome in _NOMES_IMPORTADOS
    if nome == proibido or nome.startswith(proibido + ".")
)
check(
    _PROIBIDOS_ENCONTRADOS == [],
    f"N04 prefiltro.py nao importa ORM, framework web, cliente HTTP, infra da app, "
    f"eventos persistentes, filesystem, ambiente nem relogio "
    f"(encontrados={_PROIBIDOS_ENCONTRADOS})",
)

# `open()` e `__import__()` sao chamadas, nao imports: escapariam da analise
# acima. Estrutural via ast.Call - a palavra em comentario nao conta.
_CHAMADAS_SUSPEITAS = sorted(
    {
        no.func.id
        for no in ast.walk(_ARVORE)
        if isinstance(no, ast.Call)
        and isinstance(no.func, ast.Name)
        and no.func.id in {"open", "__import__"}
    }
)
check(
    _CHAMADAS_SUSPEITAS == [],
    f"N05 nenhuma chamada direta a open() (filesystem) ou __import__() "
    f"(import dinamico, que fugiria da PROVA A) (encontradas={_CHAMADAS_SUSPEITAS})",
)

# PROVA B - DELTA de runtime, num subprocesso limpo. O baseline inclui
# `pydantic` e `app.v2.contratos`, que prefiltro.py legitimamente carrega e cuja
# inercia ja e provada por tests/test_v2_contratos.py. O que entrar DEPOIS desse
# ponto foi prefiltro.py que trouxe. O `cwd` fica fora do repositorio de
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
    "import app.v2.prefiltro\n"
    "delta = set(sys.modules) - baseline\n"
    f"suspeitos = {_SUSPEITOS_IMPORT!r}\n"
    "novos = sorted(m for m in suspeitos if m in delta)\n"
    "print('NOVOS:' + (','.join(novos) if novos else 'NENHUM'))\n"
)
_resultado_subproc = subprocess.run(
    [sys.executable, "-c", _codigo_filho],
    capture_output=True, text=True, timeout=60, cwd=tempfile.gettempdir(),
    encoding="utf-8", errors="replace",
)
check(
    _resultado_subproc.returncode == 0,
    f"N06 subprocesso limpo, fora do repo, importa app.v2.prefiltro sem erro - "
    f"nenhum IO por caminho relativo no import "
    f"(rc={_resultado_subproc.returncode}, stderr={ascii(_resultado_subproc.stderr)})",
)
_saida_subproc = _resultado_subproc.stdout.strip()
check(
    _saida_subproc == "NOVOS:NENHUM",
    f"N07 (PROVA B/DELTA) nenhum suspeito NOVO alem do baseline pydantic+contratos "
    f"entra em sys.modules ao importar app.v2.prefiltro - mede o delta, nao a "
    f"presenca absoluta (suspeitos={_SUSPEITOS_IMPORT}, saida={ascii(_saida_subproc)}, "
    f"stderr={ascii(_resultado_subproc.stderr)})",
)


# === GRUPO O - escala: o scanner avanca indice e TERMINA (P-6) ============
# Sem assercao de tempo - so a exigencia de terminar com a decisao certa. A
# entrada longa terminada em caractere invalido e a forma canonica de ReDoS
# contra `(base (mod|VS16)? (ZWJ base (mod|VS16)?)*)+`: com regex, o prefixo
# valido enorme e a falha no ultimo caractere disparam o backtracking
# catastrofico. Varrendo indice, e linear.
_MUITOS_POLEGARES = POLEGAR * 5000
caso(f"O01 5000 polegares {rot(_MUITOS_POLEGARES)}", [m("text", _MUITOS_POLEGARES)], True)
caso(
    f"O02 5000 polegares terminados em '!' {rot(_MUITOS_POLEGARES + '!')}",
    [m("text", _MUITOS_POLEGARES + "!")],
    False,
)
_CADEIA_ZWJ = (POLEGAR + ZWJ) * 2000 + POLEGAR
caso(f"O03 cadeia de 2001 bases ligadas por ZWJ {rot(_CADEIA_ZWJ)}", [m("text", _CADEIA_ZWJ)], True)
caso(
    f"O04 a mesma cadeia terminada em caractere invalido {rot(_CADEIA_ZWJ + 'x')}",
    [m("text", _CADEIA_ZWJ + "x")],
    False,
)
caso("O05 texto longo comum (5000 letras)", [m("text", "a" * 5000)], False)
caso(
    "O06 lote com 1500 itens, todos ignoraveis",
    [m("text", POLEGAR) for _ in range(750)] + [m("reaction", V1_REACAO) for _ in range(750)],
    True,
)
caso(
    "O07 lote com 1500 ignoraveis e UM substantivo na ultima posicao",
    [m("text", POLEGAR) for _ in range(1500)] + [m("text", "Quero viajar em janeiro")],
    False,
)


print()
print(f"{len(executados)} verificacoes executadas em {len(_DECISOES)} casos avaliados.")
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
