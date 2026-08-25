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

Rodar:  python tests/test_xss_crud_templates.py
"""
import html
import json
import pathlib
import re
import shutil
import subprocess
import sys

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
    p = subprocess.run([node, "-e", script], capture_output=True, text=True)
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
