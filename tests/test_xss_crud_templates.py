# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-W2A — XSS por handler inline + variaveis CSS inexistentes
nas telas de CRUD (tarefas, tags, equipes, relatorios).

A CAUSA RAIZ

Cada um destes templates carregava sua propria copia de:

    function esc(str){ ... d.textContent = str;
        return d.innerHTML.replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

O helper esta CORRETO para texto HTML e para valor de atributo entre aspas.
Ele e INUTIL no contexto em que mais era usado: literal de string JS dentro de
um atributo `on*`. O parser HTML decodifica as entidades ANTES de o JavaScript
ser compilado — `&#39;` volta a ser uma apostrofe de verdade e fecha a string.
Nenhuma funcao de escape conserta esse contexto. A correcao foi PARAR de por
dado em handler inline: id no `data-*`, objeto resolvido em JS.

O caso mais grave (tarefas.html) nem apostrofe precisava: o objeto inteiro ia
por `JSON.stringify` num atributo de aspas simples e so `'` virava entidade.
O `&` nao era escapado por ninguem — nem pelo JSON.stringify — entao um titulo
contendo o TEXTO `&quot;` era decodificado pelo parser numa aspa dupla real,
fechava a string do JSON e abria contexto de expressao.

DUAS TECNICAS, cada uma onde e honesta (mesmo criterio de
tests/test_pipeline_target_clear.py):

1. EXECUCAO REAL (node): `renderTasks` e extraida do proprio template e
   RODADA com um payload hostil. O que se verifica e a MARCACAO PRODUZIDA,
   nao a grafia do codigo. O stub de DOM e fiel no unico ponto que importa:
   `textContent -> innerHTML` escapa `& < >` como o navegador faz.
   Ha ainda um teste de controle que roda o detector contra a marcacao ANTIGA
   e exige que ele ACUSE — sem isso, um detector vazio passaria calado.

2. INSPECAO ESTATICA: o contexto perigoso (`on*` + literal de string) e uma
   propriedade da FORMA do template, e o CSS nao roda sem navegador. Para
   esses, ler o arquivo e o que da para garantir sem browser.

O `node` nao e dependencia nova (o runner ja precisa dele para o
actions/checkout). Por isso este arquivo EXIGE node em vez de pular em
silencio — seis testes deste repo pulam calados e isso e, por si so, um
achado.

AUDIT-2026-08-WF2 (secao 4) — o mesmo helper, o buraco do lado de fora

A auditoria seguinte achou o simetrico do achado acima em leads.html e
pipeline.html: dado de lead indo para innerHTML sem esc() NENHUM, agora no
contexto de TEXTO e de valor de atributo. `destinos` e `idades_criancas`
vem do workflow n8n `gerenciador_leads`, que os preenche com o que o LLM
extrai da conversa de WhatsApp — terceiro nao autenticado escrevendo no
banco. Cinco sinks, dois deles achados so na varredura (o botao
'Carregar mais' de renderStage e o <option> de loadTransferStages), ambos
invisiveis para os detectores existentes porque montam o handler/atributo
por CONCATENACAO em vez de template literal.

A secao 4 usa parser de HTML, nao substring: depois de escapado, o texto
`onerror=alert(1)` continua na marcacao — e certo que continue, como
TEXTO. So o parser separa texto de markup, que e a separacao que a
vulnerabilidade apaga.

Rodar:  python tests/test_xss_crud_templates.py
"""
import html
import json
import pathlib
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser

RAIZ = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

MEUS_TEMPLATES = ["tarefas.html", "tags.html", "equipes.html", "relatorios.html"]

TAREFAS = (RAIZ / "templates" / "tarefas.html").read_text(encoding="utf-8")

# Titulo hostil: o texto literal `&quot;` sobrevive ao JSON.stringify e ao
# esc() antigo, e o parser HTML o transforma numa aspa dupla de verdade.
PAYLOAD = '&quot;});alert(1);//'
TAREFA_HOSTIL = {
    "id": 7,
    "titulo": PAYLOAD,
    "status": "pendente",
    "tipo": "manual",
    "data_vencimento": None,
    "user_id": 1,
    "resultado_ia": None,
    "google_calendar_event_id": None,
}

# on* com aspas duplas OU simples — a versao vulneravel usava simples.
RE_HANDLER = re.compile(r"""\son[a-zA-Z]+\s*=\s*("[^"]*"|'[^']*')""")


def _corpo(src, nome, fecho="\n    }"):
    """Codigo real da funcao, extraido do template: o teste exercita producao."""
    marca = "function " + nome + "("
    assert marca in src, f"funcao {nome} nao existe mais no template"
    return marca + src.split(marca, 1)[1].split(fecho, 1)[0] + fecho


def _handlers(markup):
    """Valores dos atributos on*, ja DECODIFICADOS como o parser HTML faria.

    Decodificar e o ponto todo: e exatamente o passo que o esc() antigo
    ignorava. Comparar a marcacao crua nao provaria nada.
    """
    return [html.unescape(m.group(1)[1:-1]) for m in RE_HANDLER.finditer(markup)]


# ─────────────────────────────────────────────────────────────────────────
# 1. EXECUCAO REAL — renderTasks com titulo hostil
# ─────────────────────────────────────────────────────────────────────────

_HARNESS = """
'use strict';
const MAPA = { '&': '&amp;', '<': '&lt;', '>': '&gt;' };

// Fiel no unico ponto que importa: o navegador serializa textContent
// escapando & < > . E o `&` que o codigo antigo deixava passar.
function criaDiv() {
    let html = '';
    return {
        set textContent(v) { html = String(v).replace(/[&<>]/g, c => MAPA[c]); },
        get innerHTML() { return html; },
    };
}

const botoes = [];
const list = {
    innerHTML: '',
    querySelectorAll() {
        botoes.length = 0;
        for (const m of this.innerHTML.matchAll(/data-task-id="([^"]*)"/g)) {
            botoes.push({
                dataset: { taskId: m[1] },
                _fn: null,
                addEventListener(ev, fn) { this._fn = fn; },
            });
        }
        return botoes;
    },
};

const document = {
    getElementById: id => (id === 'tasksList' ? list : null),
    createElement: () => criaDiv(),
};

let dateFilter = 'all';
let allTasks = %s;
let editado = null;
function editTask(t) { editado = t; }

%s

%s

renderTasks();
// Clique real no botao Editar, pela fiacao que o proprio template montou.
if (botoes.length && botoes[0]._fn) botoes[0]._fn();

console.log(JSON.stringify({
    markup: list.innerHTML,
    nBotoes: botoes.length,
    editadoId: editado ? editado.id : null,
    editadoTitulo: editado ? editado.titulo : null,
}));
"""

_cache = {}


def _roda():
    if "r" in _cache:
        return _cache["r"]
    node = shutil.which("node")
    assert node, (
        "node e necessario para renderizar renderTasks de verdade. Este teste "
        "NAO pula em silencio: sem execucao real ele nao prova nada sobre a "
        "marcacao produzida, e o achado F1 e um account takeover."
    )
    script = _HARNESS % (
        json.dumps([TAREFA_HOSTIL]),
        _corpo(TAREFAS, "esc", "\n"),
        _corpo(TAREFAS, "renderTasks"),
    )
    p = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert p.returncode == 0, f"node falhou: {p.stderr.strip()}"
    _cache["r"] = json.loads(p.stdout)
    return _cache["r"]


def test_titulo_hostil_nao_escapa_de_nenhum_handler_inline():
    """F1 — a propriedade central, medida na marcacao que o codigo produziu."""
    markup = _roda()["markup"]
    for valor in _handlers(markup):
        for veneno in ("alert", "});", "titulo"):
            assert veneno not in valor, (
                f"handler inline carrega dado do lead: {valor!r} (achou {veneno!r})"
            )


def test_handlers_inline_so_recebem_numeros():
    """Se so numero atravessa o atributo, nao ha o que quebrar."""
    for valor in _handlers(_roda()["markup"]):
        assert re.fullmatch(r"[A-Za-z_$][\w$]*\(\s*\d*\s*(,\s*'[a-z_]+'\s*)?\)", valor), (
            f"handler inline com forma inesperada: {valor!r}"
        )


def test_o_objeto_da_tarefa_nao_atravessa_mais_o_html():
    markup = _roda()["markup"]
    assert '"titulo"' not in markup and "'titulo'" not in markup, (
        "o objeto serializado voltou para dentro da marcacao"
    )
    assert "JSON.stringify" not in _corpo(TAREFAS, "renderTasks"), (
        "renderTasks voltou a serializar a tarefa para dentro do HTML"
    )


def test_o_botao_editar_carrega_o_id_e_a_fiacao_funciona():
    """Corrigir sem quebrar: o clique tem que continuar abrindo a tarefa certa."""
    r = _roda()
    assert 'data-task-id="7"' in r["markup"], "o botao Editar perdeu o id"
    assert r["nBotoes"] == 1, f"esperado 1 botao Editar, veio {r['nBotoes']}"
    assert r["editadoId"] == 7, (
        f"o clique nao resolveu a tarefa em allTasks: {r['editadoId']!r}"
    )
    assert r["editadoTitulo"] == PAYLOAD, (
        "editTask precisa receber o objeto ORIGINAL, nao uma versao escapada"
    )


def test_o_titulo_hostil_continua_visivel_e_inerte_no_texto():
    """Escapar nao pode virar censura: o usuario ainda le o titulo que gravou."""
    markup = _roda()["markup"]
    assert "&amp;quot;});alert(1);//" in markup, (
        "o titulo sumiu da tela ou o `&` deixou de ser escapado no texto"
    )


def test_status_e_tipo_saem_escapados():
    """F7 — atributos que os seletores CSS de :52-57 leem."""
    corpo = _corpo(TAREFAS, "renderTasks")
    for campo in ("t.status", "t.tipo"):
        assert f'data-{campo.split(".")[1]}="${{esc({campo})}}"' in corpo, (
            f"{campo} voltou a ser interpolado cru no atributo"
        )


def test_o_detector_acusa_a_marcacao_ANTIGA():
    """
    Controle de mutacao. Sem isto, um detector que nunca dispara passaria
    calado e os testes acima seriam decorativos. Aqui se reconstroi o
    `onclick='editTask(${JSON.stringify(t).replace(/'/g,"&#39;")})'` original
    e se exige que o detector ACUSE a fuga.
    """
    antigo = (
        "<button title=\"Editar\" onclick='editTask("
        + json.dumps(TAREFA_HOSTIL).replace("'", "&#39;")
        + ")'>Editar</button>"
    )
    valores = _handlers(antigo)
    assert valores, "o detector nao enxergou nem o handler antigo"
    assert any("});alert(1);//" in v for v in valores), (
        "o detector nao acusa a versao vulneravel — ele nao prova nada"
    )


# ─────────────────────────────────────────────────────────────────────────
# 2. ESTATICO — o contexto perigoso nao pode reaparecer
# ─────────────────────────────────────────────────────────────────────────

# `onclick="foo('${x}')"` — dado interpolado como LITERAL DE STRING JS dentro
# de um atributo de evento. Nenhum escape salva este contexto.
RE_LITERAL_EM_HANDLER = re.compile(r"""on\w+="[^"]*'\$\{""")


def test_nenhum_template_meu_interpola_string_js_em_handler_inline():
    for nome in MEUS_TEMPLATES:
        src = (RAIZ / "templates" / nome).read_text(encoding="utf-8")
        achados = RE_LITERAL_EM_HANDLER.findall(src)
        assert not achados, (
            f"{nome}: dado interpolado como literal de string JS dentro de on* "
            f"— o parser HTML decodifica as entidades antes do JS compilar, "
            f"esc() nao protege ali. Achados: {achados}"
        )


def test_nenhum_template_meu_usa_atributo_de_evento_com_aspas_simples():
    """Aspas simples no atributo foi o que permitiu enfiar JSON inteiro nele."""
    for nome in MEUS_TEMPLATES:
        src = (RAIZ / "templates" / nome).read_text(encoding="utf-8")
        assert not re.search(r"\son\w+='", src), (
            f"{nome}: atributo on* com aspas simples voltou"
        )


def test_relatorios_define_e_usa_um_helper_de_escape():
    """F4 — a pagina nao tinha helper nenhum e concatenava chave crua."""
    src = (RAIZ / "templates" / "relatorios.html").read_text(encoding="utf-8")
    assert "function esc(" in src, "relatorios.html continua sem helper de escape"
    corpo = _corpo(src, "renderTableGeneric", "\n        }")
    assert "${esc(k)}" in corpo, (
        "as chaves de data.breakdown.* (valores de campo do lead e nomes de tag, "
        "escritos por usuario comum) voltaram a entrar cruas no innerHTML de "
        "uma pagina que so admin abre"
    )
    assert "${k}" not in corpo.replace("${esc(k)}", ""), (
        "sobrou uma interpolacao crua da chave em renderTableGeneric"
    )


def test_equipes_usa_textContent_no_erro_do_servidor():
    """F6 — todas as paginas irmas ja faziam assim."""
    src = (RAIZ / "templates" / "equipes.html").read_text(encoding="utf-8")
    corpo = _corpo(src, "showAlert", "\n        }")
    assert "innerHTML" not in corpo, (
        "showAlert voltou a escrever o `detail` do servidor por innerHTML"
    )
    assert "textContent" in corpo


def test_leitura_de_lista_checa_resp_ok():
    """F11 — `if (resp)` sozinho faz 403/500 virar 'nenhum registro'."""
    alvos = [
        ("tarefas.html", "fetchTasks", "\n    }"),
        ("tags.html", "loadTags", "\n    }"),
        ("equipes.html", "fetchUsers", "\n        }"),
        ("equipes.html", "fetchTeams", "\n        }"),
    ]
    for nome, fn, fecho in alvos:
        src = (RAIZ / "templates" / nome).read_text(encoding="utf-8")
        corpo = _corpo(src, fn, fecho)
        assert re.search(r"!resp\s*\|\|\s*!resp\.ok", corpo), (
            f"{nome}:{fn} nao checa resp.ok — falha do servidor fica "
            f"indistinguivel de sistema vazio"
        )


def test_salvar_equipe_confere_a_gravacao_dos_membros():
    """F10 — o POST de membros era disparado sem olhar a resposta."""
    src = (RAIZ / "templates" / "equipes.html").read_text(encoding="utf-8")
    corpo = _corpo(src, "saveTeam", "\n        }")
    assert "/members" in corpo, "o POST de membros sumiu"
    assert re.search(r"!\w+\.ok", corpo), (
        "saveTeam fecha o modal anunciando sucesso sem conferir o POST de membros"
    )
    for fn in ("saveTeam", "saveUser"):
        c = _corpo(src, fn, "\n        }")
        assert "disabled = true" in c and "finally" in c, (
            f"{fn} nao desabilita o botao em volta do await (duplo clique "
            f"cria registro duplicado) ou nao reabilita em finally"
        )


# ─────────────────────────────────────────────────────────────────────────
# 3. ESTATICO — variaveis CSS que nao existem
# ─────────────────────────────────────────────────────────────────────────

RE_DEF = re.compile(r"(--[A-Za-z0-9_-]+)\s*:")
RE_USO = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)")

# Nomes que NUNCA existiram em variables.css. Uma custom property indefinida
# invalida a declaracao no computed-value time: `border-left-color` cai para
# currentColor (texto quase preto) e `font-weight`, por ser herdada, cai para
# o valor herdado — deixando titulos MAIS LEVES que o h1 padrao do base.css.
PROIBIDOS = ("--color-danger", "--font-weight-", "--shadow-xl", "--space-16")


def _definidas():
    css = "".join(
        (RAIZ / "static" / "css" / f).read_text(encoding="utf-8")
        for f in ("variables.css", "base.css")
    )
    return set(RE_DEF.findall(css))


def test_nenhum_nome_de_variavel_inexistente_nos_meus_templates():
    for nome in MEUS_TEMPLATES:
        src = (RAIZ / "templates" / nome).read_text(encoding="utf-8")
        for proibido in PROIBIDOS:
            assert proibido not in src, (
                f"{nome} referencia {proibido}, que nao existe em variables.css "
                f"(os nomes reais sao --color-error, --fw-*, --shadow-lg, --space-12)"
            )


def test_toda_variavel_css_usada_nos_meus_templates_existe():
    definidas = _definidas()
    assert "--fw-semibold" in definidas and "--color-error" in definidas, (
        "variables.css mudou de forma: o teste esta lendo o arquivo errado"
    )
    for nome in MEUS_TEMPLATES:
        src = (RAIZ / "templates" / nome).read_text(encoding="utf-8")
        faltando = sorted(set(RE_USO.findall(src)) - definidas)
        assert not faltando, (
            f"{nome} usa var() de nomes indefinidos: {faltando}"
        )


def test_a_cor_de_atraso_e_cancelamento_continua_forte():
    """F8 — era o unico marcador visual de tarefa atrasada/cancelada."""
    for regra in ('.task-card[data-status="cancelado"]', ".task-card.is-overdue"):
        i = TAREFAS.index(regra)
        trecho = TAREFAS[i:i + 200].split("}", 1)[0]
        assert "var(--color-error)" in trecho, (
            f"{regra} perdeu a cor de erro valida: {trecho}"
        )


def test_a_restricao_de_admin_do_relatorio_esta_marcada_como_cosmetica():
    """F12 — o guard vive no localStorage; quem manda e o router."""
    src = (RAIZ / "templates" / "relatorios.html").read_text(encoding="utf-8")
    i = src.index("user.role !== 'admin'")
    antes = src[max(0, i - 700):i].lower()
    assert "cosmetica" in antes or "cosmetico" in antes, (
        "a checagem de admin no navegador precisa de comentario dizendo que "
        "nao e controle de acesso — senao alguem confia nela"
    )


# ─────────────────────────────────────────────────────────────────────────
# 4. AUDIT-2026-08-WF2 — XSS ARMAZENADO em leads.html e pipeline.html
#
# Sinks diferentes dos da secao 1: aqui o dado nao vai para dentro de um
# handler `on*`, vai para o TEXTO / para o valor de atributo, e cai em
# innerHTML sem passar por esc() nenhum. `destinos` e `idades_criancas` sao
# gravados pelo workflow n8n `gerenciador_leads` a partir do que o LLM extrai
# da conversa de WhatsApp — ou seja, por um terceiro NAO autenticado. Com
# `script-src 'self' 'unsafe-inline'` na CSP do CRM, um `onerror=` inline roda.
#
# A tecnica e a da secao 1, com um reforco: em vez de procurar substring na
# marcacao, a marcacao produzida e PARSEADA (html.parser, convert_charrefs) e
# o que se afirma e sobre o DOM resultante — nenhuma tag intrusa nasceu,
# nenhum atributo `on*` carrega o payload. Substring nao serviria: depois de
# escapado, o texto `onerror=alert(1)` continua aparecendo — e corretamente,
# como TEXTO. So o parser separa texto de markup, que e exatamente a separacao
# que a vulnerabilidade apaga.
# ─────────────────────────────────────────────────────────────────────────

LEADS_SRC = (RAIZ / "templates" / "leads.html").read_text(encoding="utf-8")
PIPE_SRC = (RAIZ / "templates" / "pipeline.html").read_text(encoding="utf-8")

TEMPLATES_WF2 = ["leads.html", "pipeline.html"]

# Dois payloads, um por contexto de fuga:
#   ASPAS     — fecha o atributo e abre tag nova (destinos, idades, option).
#   APOSTROFE — fecha o literal de string JS dentro do handler inline (o botao
#               "Carregar mais", que montava o onclick por concatenacao).
PAY_ASPAS = 'Atacama"><img src=/x onerror=alert(1)>'
PAY_IDADES = '7"><img src=/y onerror=alert(2)>'
PAY_APOSTROFE = "novo'); alert(1);//"

TAGS_INTRUSAS = {"img", "script", "svg", "iframe", "object", "embed", "link", "style"}


class Pulado(Exception):
    """Parte executavel indisponivel. NUNCA silenciosa: o runner grita SKIP."""


def _sem_comentarios_wf2(txt):
    """Remove // e /* */ para que uma correcao COMENTADA nao passe no detector."""
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", txt)


class _Coletor(HTMLParser):
    """Le a marcacao como o navegador leria: tags reais, atributos decodificados.

    `convert_charrefs=True` reproduz o passo que a vulnerabilidade explora — o
    parser desfaz `&#39;` / `&quot;` ANTES de qualquer outra coisa. Se o escape
    esta certo, o payload chega aqui como DATA; se esta errado, chega como
    STARTTAG ou como valor de `on*`.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.handlers = []
        self.atributos = []
        self.texto = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for chave, valor in attrs:
            self.atributos.append((tag, chave.lower(), valor or ""))
            if chave.lower().startswith("on"):
                self.handlers.append((tag, chave.lower(), valor or ""))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        self.texto.append(data)


def _le(markup):
    c = _Coletor()
    c.feed(markup)
    c.close()
    return c


def _acusacoes(markup):
    """O DETECTOR: motivos pelos quais esta marcacao e XSS. Vazio = inerte.

    Roda contra a marcacao de producao (tem que vir vazio) E contra a marcacao
    ANTIGA no teste de controle (tem que vir cheio) — detector que so roda no
    caso feliz nao prova nada.
    """
    c = _le(markup)
    motivos = []
    intrusas = sorted(set(c.tags) & TAGS_INTRUSAS)
    if intrusas:
        motivos.append(f"o payload virou tag real: {intrusas}")
    for tag, attr, valor in c.handlers:
        if "alert" in valor:
            motivos.append(f"<{tag} {attr}> carrega o payload decodificado: {valor!r}")
    return motivos


def _exige_node():
    node = shutil.which("node")
    if node is None:
        raise Pulado(
            "node ausente do PATH — a verificacao COMPORTAMENTAL (renderizar a "
            "funcao real do template com payload hostil) NAO rodou. As checagens "
            "estaticas da secao 4b rodaram e continuam valendo, mas elas leem a "
            "grafia do codigo, nao a marcacao produzida. Instale node para a "
            "cobertura completa."
        )
    return node


def _node(script):
    p = subprocess.run(
        [_exige_node(), "-e", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert p.returncode == 0, f"node falhou: {p.stderr.strip()}"
    return json.loads(p.stdout)


# Stub de DOM. Fiel no unico ponto que importa: textContent -> innerHTML escapa
# & < > como o navegador faz. E o `&` que os helpers caseiros esquecem.
_STUB_WF2 = """
'use strict';
const MAPA = { '&': '&amp;', '<': '&lt;', '>': '&gt;' };
function criaDiv() {
    let h = '';
    return {
        set textContent(v) { h = String(v).replace(/[&<>]/g, c => MAPA[c]); },
        get innerHTML() { return h; },
    };
}
const _els = {};
function _el(id) {
    if (!_els[id]) _els[id] = {
        id, value: '', textContent: '', innerHTML: '',
        classList: { add() {}, remove() {}, contains() { return false; } },
        querySelector() { return null; },
    };
    return _els[id];
}
const document = { getElementById: _el, createElement: () => criaDiv() };
"""

_cache_wf2 = {}


def _render_leads():
    """getDestinoTags, extraida do proprio leads.html, com destino hostil."""
    if "leads" not in _cache_wf2:
        script = (
            _STUB_WF2
            + _corpo(LEADS_SRC, "esc", "\n    }") + "\n"
            + _corpo(LEADS_SRC, "getDestinoTags") + "\n"
            + "console.log(JSON.stringify({ markup: getDestinoTags("
            + json.dumps([PAY_ASPAS]) + ") }));"
        )
        _cache_wf2["leads"] = _node(script)["markup"]
    return _cache_wf2["leads"]


def _render_pipeline():
    """preencherDrawer + renderStage + loadTransferStages, do template real."""
    if "pipe" not in _cache_wf2:
        lead = {
            "nome": "Fulano", "email": None, "whatsapp": None,
            "destinos": [PAY_ASPAS],
            "data_chegada": None, "data_partida": None, "total_dias": None,
            "datas_destinos": {}, "num_viajantes": 2, "num_criancas": 1,
            "idades_criancas": PAY_IDADES,
            "tags": [], "campos_personalizados": {},
            "status_venda": "em_negociacao", "responsavel_id": 0,
        }
        card = {
            "entry_id": 11, "lead_id": 22, "nome": "Fulano", "email": None,
            "whatsapp": None, "destinos": [PAY_ASPAS], "tags": [],
            "num_viajantes": None, "num_criancas": None,
            "data_chegada": None, "responsavel_nome": None,
        }
        etapa = json.dumps(PAY_APOSTROFE)
        script = (
            _STUB_WF2
            + _corpo(PIPE_SRC, "esc", "\n") + "\n"
            + _corpo(PIPE_SRC, "fmtDate", "\n") + "\n"
            + _corpo(PIPE_SRC, "getDestinoClass") + "\n"
            + _corpo(PIPE_SRC, "getDestinoIcon", "\n") + "\n"
            + _corpo(PIPE_SRC, "renderLeadCard") + "\n"
            + _corpo(PIPE_SRC, "preencherDrawer") + "\n"
            + _corpo(PIPE_SRC, "renderStage") + "\n"
            + _corpo(PIPE_SRC, "loadTransferStages") + "\n"
            + "const highlightLeadId = null;\n"
            + "const boardMeta = { stages: [{ id: " + etapa + ", nome: 'x', total: 1 }] };\n"
            + "const stageState = { [" + etapa + "]: { items: " + json.dumps([card])
            + ", hasMore: true, filtrado: 1 } };\n"
            + "const funnels = [{ id: 1, nome: 'F', etapas: [{ id: "
            + json.dumps(PAY_ASPAS) + ", nome: 'Etapa' }] }];\n"
            + "preencherDrawer(" + json.dumps(lead) + ");\n"
            + "_el('body-' + " + etapa + ");\n"
            + "renderStage(" + etapa + ");\n"
            + "_el('transferFunnel').value = '1';\n"
            + "loadTransferStages();\n"
            + "console.log(JSON.stringify({\n"
            + "  drawer: _el('detailInfo').innerHTML,\n"
            + "  coluna: _el('body-' + " + etapa + ").innerHTML,\n"
            + "  transfer: _el('transferStage').innerHTML,\n"
            + "}));"
        )
        _cache_wf2["pipe"] = _node(script)
    return _cache_wf2["pipe"]


# ── 4a. COMPORTAMENTO: a marcacao que o template produziu e inerte ────────

def test_wf2_destino_hostil_nao_vira_markup_na_tabela_de_leads():
    """Sink 1 — getDestinoTags -> linhaDoLead -> tbody.innerHTML."""
    markup = _render_leads()
    assert not _acusacoes(markup), (
        f"leads.html getDestinoTags: {_acusacoes(markup)} — markup: {markup!r}"
    )


def test_wf2_destino_hostil_nao_vira_markup_no_drawer_do_pipeline():
    """Sink 2 — preencherDrawer, campo Destinos -> detailInfo.innerHTML."""
    markup = _render_pipeline()["drawer"]
    assert not _acusacoes(markup), (
        f"pipeline.html preencherDrawer/destinos: {_acusacoes(markup)} — {markup!r}"
    )


def test_wf2_idades_criancas_hostil_nao_vira_markup_no_drawer():
    """Sink 3 — viajantesText, interpolado cru no MESMO detailInfo.innerHTML."""
    markup = _render_pipeline()["drawer"]
    assert "img src=/y" in html.unescape(markup), (
        "o payload de idades_criancas nao chegou na marcacao: o teste esta "
        "CEGO, nao aprovado (o campo Viajantes so sai com num_viajantes/num_criancas)"
    )
    assert not _acusacoes(markup), (
        f"pipeline.html preencherDrawer/idades: {_acusacoes(markup)} — {markup!r}"
    )


def test_wf2_stage_id_hostil_nao_escapa_do_handler_da_coluna():
    """Sink 4 (achado NOVO desta varredura) — botao 'Carregar mais'.

    O caso que nem a secao 1 nem test_frontend_injection_contract.py pegam: o
    onclick era montado por CONCATENACAO, e os dois detectores so procuram a
    forma template literal.
    """
    markup = _render_pipeline()["coluna"]
    assert "Carregar mais" in markup, (
        "o botao sumiu — sem ele este teste nao exercita o sink (hasMore=true)"
    )
    assert not _acusacoes(markup), (
        f"pipeline.html renderStage: {_acusacoes(markup)} — markup: {markup!r}"
    )


def test_wf2_stage_id_hostil_nao_escapa_do_option_de_transferencia():
    """Sink 5 (achado NOVO) — loadTransferStages, atributo value cru.

    FunnelResponse.etapas e `list[dict]`: passthrough do JSON do banco, sem
    validacao na leitura. O pattern de StageSchema.id so roda na ESCRITA, entao
    etapa legada (ou gravada antes do pattern) chega aqui com o que tiver.
    """
    markup = _render_pipeline()["transfer"]
    assert not _acusacoes(markup), (
        f"pipeline.html loadTransferStages: {_acusacoes(markup)} — {markup!r}"
    )


def test_wf2_o_botao_carregar_mais_continua_sabendo_a_etapa():
    """Corrigir sem quebrar: o clique tem que recarregar a etapa CERTA.

    data-* + this.dataset.stage: o parser devolve o id ORIGINAL, com apostrofe
    e tudo. Se alguem "consertar" trocando esc() por uma sanitizacao que apaga
    caracteres, este teste cai.
    """
    c = _le(_render_pipeline()["coluna"])
    stages = [v for _, k, v in c.atributos if k == "data-stage"]
    assert stages, f"o botao perdeu o data-stage: {_render_pipeline()['coluna']!r}"
    assert stages[0] == PAY_APOSTROFE, (
        f"data-stage nao volta ao id original depois de decodificado: "
        f"{stages[0]!r} != {PAY_APOSTROFE!r} — loadStage recarregaria outra etapa"
    )
    assert any("this.dataset.stage" in v for _, _, v in c.handlers), (
        "o onclick nao le mais a etapa do dataset"
    )


def test_wf2_o_destino_hostil_continua_visivel_como_texto():
    """Escapar nao pode virar censura: o operador ainda le o que foi gravado."""
    texto = "".join(_le(_render_leads()).texto)
    assert PAY_ASPAS in texto, (
        f"o destino sumiu da tela em vez de ser neutralizado: {texto!r}"
    )


def test_wf2_o_detector_acusa_a_marcacao_ANTIGA():
    """Controle de mutacao — sem isto a secao 4a inteira seria decorativa.

    Reconstroi a marcacao que cada sink produzia ANTES da correcao e exige que
    `_acusacoes` ACUSE as duas formas de fuga (tag nova e handler sequestrado).
    """
    antigos = {
        "getDestinoTags": f'<span class="destino-tag outro">{PAY_ASPAS}</span>',
        "preencherDrawer/destinos": f'<span class="detail-field-value">{PAY_ASPAS}</span>',
        "preencherDrawer/idades": f'<span class="detail-field-value">2 adultos + 1 crianca ({PAY_IDADES} anos)</span>',
        "loadTransferStages": f'<option value="{PAY_ASPAS}">Etapa</option>',
        "renderStage": (
            '<button class="btn btn-sm" onclick="loadStage(&#39;'
            + PAY_APOSTROFE.replace("'", "&#39;")
            + '&#39;, false)">Carregar mais</button>'
        ),
    }
    for nome, markup in antigos.items():
        assert _acusacoes(markup), (
            f"o detector NAO acusa a marcacao vulneravel de {nome} — logo ele "
            f"nao prova nada sobre a corrigida. Markup: {markup!r}"
        )


# ── 4b. ESTATICO — a forma perigosa nao pode reaparecer ───────────────────

# A secao 1 e o test_frontend_injection_contract.py cobrem `on*="...'${x}'"`.
# ESTA e a forma irma que nenhum dos dois enxergava: o mesmo literal de string
# JS, so que aberto por CONCATENACAO — `onclick="f(\'' + x + '\')"`. Foi assim
# que o botao "Carregar mais" sobreviveu a varredura AUDIT-2026-08-W2B-orq.
RE_CONCAT_EM_HANDLER = re.compile(r"""on\w+\s*=\s*"[^"]*\\'""")


def test_wf2_nenhum_handler_inline_montado_por_concatenacao():
    for nome in TEMPLATES_WF2:
        src = _sem_comentarios_wf2((RAIZ / "templates" / nome).read_text(encoding="utf-8"))
        achados = RE_CONCAT_EM_HANDLER.findall(src)
        assert not achados, (
            f"{nome}: handler inline abrindo literal de string JS por "
            f"concatenacao. esc() NAO protege ali (o parser decodifica antes de "
            f"o JS compilar) — use data-* + this.dataset.*. Achados: {achados}"
        )


def test_wf2_nenhum_handler_inline_com_template_literal():
    for nome in TEMPLATES_WF2:
        src = _sem_comentarios_wf2((RAIZ / "templates" / nome).read_text(encoding="utf-8"))
        achados = RE_LITERAL_EM_HANDLER.findall(src)
        assert not achados, f"{nome}: dado como literal de string JS em on*: {achados}"


def test_wf2_os_detectores_estaticos_acusam_a_forma_ANTIGA():
    """Controle: os dois regex acima precisam disparar na grafia vulneravel."""
    antigo_concat = """'<button onclick="loadStage(\\'' + stageId + '\\', false)">x</button>'"""
    assert RE_CONCAT_EM_HANDLER.search(antigo_concat), (
        "o detector de concatenacao nao acusa a linha que ele existe para pegar"
    )
    assert RE_LITERAL_EM_HANDLER.search("""onclick="del('${esc(x)}')" """), (
        "o detector de template literal nao acusa a forma antiga"
    )


def test_wf2_os_cinco_sinks_continuam_escapados_na_fonte():
    """Grafia, como rede. O que PROVA e a secao 4a; isto localiza a regressao."""
    corpo = _corpo(LEADS_SRC, "getDestinoTags")
    assert "${esc(d)}" in corpo, "leads.html getDestinoTags: destino voltou a ser cru"
    assert "${d}</span>" not in corpo, "leads.html getDestinoTags: sobrou interpolacao crua"

    drawer = _corpo(PIPE_SRC, "preencherDrawer")
    assert "esc((lead.destinos || []).join(', '))" in drawer, (
        "pipeline.html preencherDrawer: destinos voltou a ser cru"
    )
    assert "esc(lead.idades_criancas)" in drawer, (
        "pipeline.html preencherDrawer: idades_criancas voltou a ser cru"
    )

    stage = _corpo(PIPE_SRC, "renderStage")
    assert "this.dataset.stage" in stage, (
        "pipeline.html renderStage: o handler voltou a receber o stageId interpolado"
    )
    assert "esc(stageId)" in stage, "pipeline.html renderStage: data-stage voltou cru"

    transfer = _corpo(PIPE_SRC, "loadTransferStages")
    assert "${esc(s.id)}" in transfer, (
        "pipeline.html loadTransferStages: s.id voltou cru para o atributo value"
    )


ALL_TESTS = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    falhas = 0
    pulados = 0
    for fn in ALL_TESTS:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Pulado as exc:
            pulados += 1
            print(f"SKIP  {fn.__name__}: {exc}")
        except AssertionError as exc:
            falhas += 1
            print(f"FAIL  {fn.__name__}: {exc}")
        except Exception as exc:
            falhas += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(ALL_TESTS) - falhas - pulados}/{len(ALL_TESTS)} testes OK")
    if pulados:
        # AUDIT-2026-08-WF2: skip entra no RESUMO, nao so numa linha perdida
        # no meio do log. Verde com verificacao faltando e o defeito que esta
        # auditoria mais encontrou.
        print(f"ATENCAO: {pulados} teste(s) PULADOS — verificacao NAO executada")
    sys.exit(1 if falhas else 0)
