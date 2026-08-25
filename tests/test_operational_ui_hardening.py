# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-W2C — endurecimento das telas operacionais.

`templates/operational/kanban.html` era o ponto fora da curva de todo o front:
a UNICA pagina que falava com a API por `fetch` cru, com o JWT tirado do
localStorage uma vez no load, e por consequencia a unica sem NENHUM tratamento
de 401. Somava-se a isso um contrato errado (`GET /api/users`), tres sinks de
XSS (um deles ARMAZENADO), quatro `response.ok` sem `else`, um `catch {}` vazio
e tres formularios sem guarda de duplo-submit.

DUAS TECNICAS, cada uma onde ela e honesta:

1. EXECUCAO REAL (node): as funcoes de render que interpolam dado de usuario
   (`fetchUsers`, `fetchAssignees`, `fetchCustomFields`, `fetchHistory`) sao
   EXTRAIDAS do template e RODADAS contra payloads hostis. O `esc()` exercitado
   e o do proprio arquivo. O stub de DOM so reproduz a serializacao de um text
   node (`&`, `<`, `>`) — a parte que o navegador faz e que nao esta em disputa;
   o que o teste mede e se o template CHAMA esc() nos lugares certos e se o HTML
   resultante e inerte. Uma regressao que tire um `esc()` produz `<img ...>` de
   verdade na saida e o teste quebra.

2. INSPECAO ESTATICA: "todo `if (response.ok)` tem `else`", "nao sobrou `fetch`
   cru", "nao sobrou `catch {}` vazio" e as regras de CSS/layout sao
   propriedades do ARQUIVO, nao de uma execucao. Nao ha browser no repo, entao
   media query e padding duplicado se verificam lendo o CSS.

LIMITACAO ASSUMIDA: sem Playwright/Selenium no repo (e este PR nao justifica
instalar um), nao ha teste de drag&drop real, de clique real nem de pixel.

`node` nao e dependencia nova: o runner ubuntu-latest ja precisa dele para o
actions/checkout (action node20) — por isso o teste EXIGE em vez de pular.

Rodar:  python tests/test_operational_ui_hardening.py
   ou:  python -m pytest tests/test_operational_ui_hardening.py
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

KANBAN = (ROOT / "templates" / "operational" / "kanban.html").read_text(encoding="utf-8")
BOARDS = (ROOT / "templates" / "operational" / "boards.html").read_text(encoding="utf-8")
PENDING = (ROOT / "templates" / "operational" / "pending.html").read_text(encoding="utf-8")

# Payloads hostis. A: injeta uma TAG nova. B: quebra um atributo de aspas duplas
# para pendurar um handler no MESMO elemento (o caso que `esc()` so cobre porque
# tambem escapa aspas — SEC-XSS-02).
XSS_TAG = "<img src=x onerror=alert(1)>"
XSS_ATTR = 'x" onmouseover="alert(1)'


def _sem_comentarios(src: str) -> str:
    """
    Tira comentarios `//` (preservando `http://`) e `/* */`.

    Sem isto, comentar uma chamada — `// esc(...)` — deixaria os greps passando
    porque a string continua no arquivo. E os comentarios de auditoria CITAM os
    anti-padroes que este teste proibe (`catch (err) {}`, `innerHTML +=`), entao
    sem remove-los o teste acusaria a propria documentacao da correcao.
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?<!:)//.*", "", src)


def _corpo(nome: str, assinc: bool = True) -> str:
    """Codigo REAL da funcao, extraido do template: o teste exercita producao."""
    marca = ("async function " if assinc else "function ") + nome + "("
    assert marca in KANBAN, f"funcao {nome} nao existe mais em kanban.html"
    corpo = KANBAN.split(marca, 1)[1].split("\n        }", 1)[0]
    return marca + corpo + "\n        }"


# ─────────────────────────────────────────────────────────────────────────
# 1. EXECUCAO REAL — os tres sinks de XSS (F3) e o historico (F9)
# ─────────────────────────────────────────────────────────────────────────

_HARNESS = r"""
'use strict';

// ── stub de DOM ──────────────────────────────────────────────────────────
// Reproduz so o que esta em jogo: um text node serializado por innerHTML
// escapa &, < e > (e NAO escapa aspas — dai o esc() do arquivo precisar dos
// dois replaces extras). Tudo o mais e registro passivo.
function makeEl() {
    let text = null, html = '';
    return {
        style: {}, options: [{ textContent: '' }], children: [],
        set textContent(v) { text = String(v); html = null; },
        get textContent() { return text; },
        set innerHTML(v) { html = String(v); text = null; },
        get innerHTML() {
            if (html !== null) return html;
            return String(text)
                .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        },
        appendChild(c) { this.children.push(c); },
        querySelector() { return null; },
    };
}
const REGISTRO = {};
function elemento(id) {
    if (!REGISTRO[id]) REGISTRO[id] = makeEl();
    return REGISTRO[id];
}
function coletar(el) {
    return el.innerHTML + el.children.map(coletar).join('');
}
const document = { createElement: () => makeEl(), getElementById: elemento };

// ── stub de rede ─────────────────────────────────────────────────────────
const XSS_TAG = %(tag)s;
const XSS_ATTR = %(attr)s;
const ROTAS = [
    ['/users/for-select', { users: [{ id: 5, nome: XSS_TAG }] }],
    ['/activity-logs', [{ created_at: '2026-01-01T12:00:00', action: XSS_TAG, user_id: 7 }]],
    ['/assignees', [{ user_id: 5 }]],
    ['/field-values', [{ definition_id: 1, value_text: XSS_ATTR },
                       { definition_id: 2, value_text: XSS_TAG }]],
];
const Auth = {
    apiRequest: async (url) => {
        const hit = ROTAS.find(([k]) => url.indexOf(k) !== -1);
        if (!hit) throw new Error('rota nao stubada: ' + url);
        return { ok: true, status: 200, json: async () => hit[1] };
    },
};

// ── estado do modulo ─────────────────────────────────────────────────────
let activeCardId = 99;
let usersList = [];
let fieldSavedValues = {};
let fieldDefsError = "";
let fieldDefinitions = [
    { id: 1, name: XSS_TAG, field_type: 'text' },
    { id: 2, name: 'Opcoes', field_type: 'select', select_options: [XSS_TAG, XSS_ATTR] },
];
async function failMsg(response, contexto) { return contexto; }

// ── codigo de producao, extraido do template ─────────────────────────────
%(esc)s
%(fetchUsers)s
%(fetchAssignees)s
%(fetchCustomFields)s
%(fetchHistory)s

(async () => {
    await fetchUsers();
    await fetchAssignees();
    await fetchCustomFields();
    await fetchHistory();
    console.log(JSON.stringify({
        select: coletar(elemento('addAssigneeSelect')),
        assignees: coletar(elemento('assigneesList')),
        campos: coletar(elemento('customFieldsContainer')),
        historico: coletar(elemento('cardHistoryContainer')),
        usersList: usersList,
    }));
})().catch(e => { console.error(e); process.exit(3); });
"""


def _roda_render():
    node = shutil.which("node")
    assert node, (
        "node e necessario para exercitar os sinks de XSS de verdade; "
        "sem ele este teste FALHA em vez de passar em silencio"
    )
    script = _HARNESS % {
        "tag": json.dumps(XSS_TAG),
        "attr": json.dumps(XSS_ATTR),
        "esc": _sem_comentarios(_corpo("esc", assinc=False)),
        "fetchUsers": _sem_comentarios(_corpo("fetchUsers")),
        "fetchAssignees": _sem_comentarios(_corpo("fetchAssignees")),
        "fetchCustomFields": _sem_comentarios(_corpo("fetchCustomFields")),
        "fetchHistory": _sem_comentarios(_corpo("fetchHistory")),
    }
    p = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert p.returncode == 0, f"node falhou: {p.stderr.strip()}"
    return json.loads(p.stdout)


_CACHE = {}


def render_js():
    if "r" not in _CACHE:
        _CACHE["r"] = _roda_render()
    return _CACHE["r"]


def _inerte(html, onde):
    assert "<img" not in html.lower(), (
        f"{onde}: o payload virou TAG de verdade — sink sem esc(). HTML: {html[:400]}"
    )
    assert 'onmouseover="' not in html, (
        f"{onde}: aspas nao escapadas fecharam o atributo e penduraram um "
        f"handler no elemento. HTML: {html[:400]}"
    )
    assert "<script" not in html.lower(), f"{onde}: <script> injetado. HTML: {html[:400]}"


def test_xss_nome_de_usuario_no_select():
    """F3 — `<option value="${u.id}">${u.nome}</option>` ia cru."""
    _inerte(render_js()["select"], "addAssigneeSelect")


def test_xss_nome_do_responsavel():
    """F3 — `<span>👤 ${name}</span>` em fetchAssignees ia cru."""
    r = render_js()
    assert r["assignees"], "fetchAssignees nao renderizou nada (stub quebrado?)"
    _inerte(r["assignees"], "assigneesList")


def test_xss_armazenado_nos_campos_personalizados():
    """
    F3 — o pior dos tres: `currentVal` vem de `value_text`, texto livre que
    QUALQUER usuario grava via saveCustomField. Persistido por card e executado
    para todo operador que abrisse o card, com o JWT no localStorage e sem CSP.
    """
    r = render_js()
    assert r["campos"], "fetchCustomFields nao renderizou nada (stub quebrado?)"
    _inerte(r["campos"], "customFieldsContainer")


def test_xss_no_historico_do_card():
    """F9 — `<strong>${l.action}</strong>` ia cru."""
    r = render_js()
    assert r["historico"], "fetchHistory nao renderizou nada (stub quebrado?)"
    _inerte(r["historico"], "cardHistoryContainer")


def test_historico_monta_de_uma_vez_so():
    """
    F9 — o `innerHTML +=` dentro do loop reparseava o container a cada volta
    (destroi listeners, O(n^2)). A montagem tem que ser unica.
    """
    corpo = _sem_comentarios(_corpo("fetchHistory"))
    assert "innerHTML +=" not in corpo, "fetchHistory ainda concatena innerHTML no loop"
    assert "container.innerHTML = " in corpo, "fetchHistory deve atribuir de uma vez"


def test_lista_de_usuarios_le_o_campo_certo_da_resposta():
    """
    F2 — `/api/users` devolve {total, skip, limit, users} e e gated por
    require_admin; `usersList.forEach` estourava TypeError e abortava o init()
    antes de fetchFieldDefinitions(). Agora e `/api/users/for-select`.
    """
    users = render_js()["usersList"]
    assert isinstance(users, list) and users, (
        f"usersList precisa sair de `data.users`, veio {users!r}"
    )
    assert users[0].get("id") == 5, f"contrato de for-select nao respeitado: {users!r}"


# ─────────────────────────────────────────────────────────────────────────
# 2. INSPECAO ESTATICA — sessao, contrato e ramos de erro
# ─────────────────────────────────────────────────────────────────────────

def _script(html):
    return _sem_comentarios(html.split("{% block scripts %}", 1)[1])


def test_kanban_nao_tem_mais_fetch_cru():
    """
    F1 — todo request tem que passar por Auth.apiRequest, que le o token a CADA
    chamada e, no 401, limpa a sessao e redireciona. `fetch` cru nao faz nada
    disso e era a raiz do 'quadro morto sem aviso'.
    """
    crus = re.findall(r"\bfetch\s*\(", _script(KANBAN))
    assert not crus, f"sobraram {len(crus)} chamadas a fetch() cru em kanban.html"
    assert "Auth.apiRequest(" in KANBAN, "kanban.html nao usa Auth.apiRequest"


def test_kanban_nao_guarda_o_token_em_variavel():
    """F1 — o snapshot `let token = Auth.getToken()` congelava a sessao no load."""
    assert "Auth.getToken()" not in _script(KANBAN), (
        "kanban.html voltou a fotografar o token; Auth.apiRequest ja o le por request"
    )
    assert not re.search(r"Bearer \$\{token\}", KANBAN), "header Authorization montado a mao"


def test_kanban_exige_sessao_como_as_paginas_irmas():
    """F1 — boards.html e pending.html sempre chamaram; o kanban, nunca."""
    assert "Auth.requireAuth()" in _script(KANBAN), "kanban.html sem Auth.requireAuth()"


def test_kanban_usa_o_endpoint_aberto_de_usuarios():
    """F2 — /api/users e require_admin; /api/users/for-select existe para isto."""
    script = _script(KANBAN)
    assert "/api/users/for-select" in script, "kanban.html nao usa /api/users/for-select"
    assert '"/api/users"' not in script and "'/api/users'" not in script, (
        "kanban.html ainda chama /api/users (admin-only, e com outro contrato)"
    )


def test_todo_response_ok_tem_ramo_de_falha():
    """
    F1/F4/F5/F7/F8 — o padrao do arquivo era `if (response.ok) { ... }` SEM
    `else`: falha de rede, 403, 404 e 500 viravam no-op silencioso.
    """
    script = _script(KANBAN)
    positivos = list(re.finditer(r"if \((?:response && )?response\.ok\)\s*\{", script))
    assert len(positivos) >= 20, (
        f"so {len(positivos)} checagens de response.ok — o arquivo tem mais de 20 "
        "chamadas de API; alguma perdeu a checagem por completo"
    )
    sem_else = []
    for m in positivos:
        fim = _fecha_bloco(script, m.end() - 1)
        if not re.match(r"\s*else\b", script[fim + 1:]):
            linha = script[:m.start()].count("\n") + 1
            sem_else.append(linha)
    assert not sem_else, (
        f"response.ok sem else (linha relativa ao bloco de scripts): {sem_else}"
    )


def _fecha_bloco(src, abre):
    """Indice do `}` que fecha a `{` em `abre`. Suficiente para blocos de codigo."""
    assert src[abre] == "{"
    nivel = 0
    for i in range(abre, len(src)):
        if src[i] == "{":
            nivel += 1
        elif src[i] == "}":
            nivel -= 1
            if nivel == 0:
                return i
    raise AssertionError("bloco nao fechado")


def test_nenhum_catch_vazio():
    """F5 — `catch (err) {}` engolia inclusive a queda de rede no move do card."""
    for nome, html in (("kanban", KANBAN), ("boards", BOARDS), ("pending", PENDING)):
        vazios = re.findall(r"catch\s*(?:\([^)]*\))?\s*\{\s*\}", _sem_comentarios(html))
        assert not vazios, f"{nome}.html: {len(vazios)} catch vazio(s) — House rule"


def test_move_do_card_le_o_corpo_e_trata_no_change():
    """
    F5 — app/routers/operational_flow.py:38-45 responde 200 com um corpo
    COMPLETAMENTE diferente ({"status": "no_change"}) quando a coluna de destino
    e a mesma. Sem ler o corpo era impossivel distinguir de um move real.
    """
    corpo = _sem_comentarios(_corpo("drop"))
    assert '"no_change"' in corpo or "'no_change'" in corpo, (
        "drop() nao distingue o 200 de no_change de uma movimentacao real"
    )
    assert "await response.json()" in corpo, "drop() nao le o corpo da resposta"
    assert "fetchCards()" in corpo, "drop() precisa re-sincronizar com o servidor"


def test_toggle_de_checklist_reverte_na_falha():
    """
    F7 — o browser marca o checkbox ANTES do request; sem reverter, uma tarefa
    aparecia como feita enquanto o servidor a mantinha pendente.
    """
    corpo = _sem_comentarios(_corpo("toggleChecklistItem"))
    assert "input.checked = !input.checked" in corpo, (
        "toggleChecklistItem nao desfaz a marcacao otimista do browser"
    )
    assert "toggleChecklistItem(${i.id}, this)" in KANBAN, (
        "o onclick precisa passar o proprio input para poder reverter"
    )


def test_campo_personalizado_restaura_valor_na_falha():
    """F4 — o POST era aguardado e o resultado JOGADO FORA."""
    corpo = _sem_comentarios(_corpo("saveCustomField"))
    assert "response.ok" in corpo, "saveCustomField ainda descarta o resultado do POST"
    assert "fieldSavedValues" in corpo, (
        "sem o ultimo valor confirmado nao ha como restaurar a edicao perdida"
    )


def test_abrir_card_nao_confia_no_id_compartilhado():
    """F8 — cliques rapidos misturavam titulo de um card com checklists de outro."""
    corpo = _sem_comentarios(_corpo("openCardDetails"))
    for sub in ("fetchChecklists(cardId)", "fetchComments(cardId)",
                "fetchAssignees(cardId)", "fetchCustomFields(cardId)",
                "fetchHistory(cardId)"):
        assert sub in corpo, f"openCardDetails ainda depende do activeCardId em {sub}"
    assert "cardDetailSeq" in corpo, "sem sequencia, o clique superado ainda abre o modal"


def test_formularios_tem_guarda_de_duplo_submit():
    """F6/F11 — nao ha chave de idempotencia no servidor: 2 cliques = 2 registros."""
    script = _script(KANBAN)
    assert script.count("setSubmitting(") >= 6, (
        "os 3 formularios do kanban precisam desabilitar/reabilitar o submit "
        "(2 chamadas cada: antes do await e no finally)"
    )
    assert "finally" in script, "a reabilitacao tem que estar num finally"
    assert "submitBtn.disabled = true" in BOARDS, (
        "boards.html: createBoardForm sem guarda de duplo-submit"
    )


def test_boards_nao_inventa_a_causa_do_erro():
    """
    F11 — o `else` unico culpava a permissao para 500, 422 e ate para o caminho
    do 401, mandando o usuario para o suporte errado.
    """
    assert "response.status === 403" in BOARDS, (
        "boards.html: a mensagem de permissao tem que ser condicionada ao 403"
    )
    permissao = "Apenas administradores podem criar novos quadros operacionais!"
    trecho = BOARDS.split("createBoardForm\").onsubmit", 1)[1]
    i = trecho.index(permissao)
    assert "403" in trecho[max(0, i - 200):i], (
        "a mensagem de permissao continua num else generico"
    )


def test_check_de_admin_esta_marcado_como_cosmetico():
    """F10 — `Auth.getUser().role` sai do localStorage, que o usuario controla."""
    trecho = KANBAN.split("Auth.getUser()", 1)[0][-900:]
    assert "COSMETICO" in trecho.upper() or "cosmetic" in trecho.lower(), (
        "falta o comentario dizendo que o servidor e a fronteira real"
    )


# ─────────────────────────────────────────────────────────────────────────
# 3. LAYOUT — pendencias no mobile e .content-area duplicada
# ─────────────────────────────────────────────────────────────────────────

def test_pending_tem_media_query_de_coluna_unica():
    """
    F12 — a pagina nao tinha NENHUMA media query, e o @media (max-width: 640px)
    do layout.css so ajusta padding: nunca reseta colunas. Num telefone de 375px
    cada coluna ficava com ~160px menos 40px de padding.
    """
    blocos = re.findall(r"@media[^{]*\(max-width[^{]*\)\s*\{(.*?)\n        \}",
                        PENDING, re.S)
    assert blocos, "pending.html continua sem nenhuma media query"
    assert any("grid-template-columns: 1fr;" in b for b in blocos), (
        "nenhuma media query de pending.html reseta grid-template-columns"
    )


def test_pending_nao_sequestra_a_classe_global():
    """F12 — .content-area e a classe de layout de TODAS as paginas."""
    css = PENDING.split("{% block content %}", 1)[0]
    assert not re.search(r"^\s*\.content-area\s*\{", css, re.M), (
        "pending.html volta a redefinir a .content-area global"
    )
    assert ".pending-grid" in css, "o grid da pagina precisa de classe propria"


def test_nenhuma_content_area_duplicada():
    """
    F13 — base.html:50 ja envolve o bloco de conteudo numa .content-area; o
    wrapper interno aplicava o padding global duas vezes (96px no desktop) e,
    em pending.html, aninhava um grid dentro de outro.
    """
    for nome, html in (("boards", BOARDS), ("pending", PENDING)):
        corpo = html.split("{% block content %}", 1)[1].split("{% endblock %}", 1)[0]
        corpo = re.sub(r"\{#.*?#\}", "", corpo, flags=re.S)  # comentario nao e wrapper
        assert 'class="content-area"' not in corpo, (
            f"{nome}.html abre uma segunda .content-area dentro da do base.html"
        )


def test_divs_do_bloco_content_continuam_balanceadas():
    """Guarda da remocao do wrapper: sobrar/faltar </div> quebra o layout inteiro."""
    for nome, html in (("boards", BOARDS), ("pending", PENDING)):
        corpo = html.split("{% block content %}", 1)[1].split("{% endblock %}", 1)[0]
        corpo = re.sub(r"\{#.*?#\}", "", corpo, flags=re.S)
        abre = len(re.findall(r"<div\b", corpo))
        fecha = len(re.findall(r"</div>", corpo))
        assert abre == fecha, f"{nome}.html: {abre} <div> para {fecha} </div>"


# ─────────────────────────────────────────────────────────────────────────
# 4. SMOKE DE RENDER — as tres paginas continuam compilando em Jinja
# ─────────────────────────────────────────────────────────────────────────

def test_smoke_render_das_tres_paginas():
    import jinja2
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(ROOT / "templates")), autoescape=True
    )
    ctx = {"board_id": 1, "page_title": "T", "active_nav": "operational-boards",
           "user": {"nome": "Teste", "role": "admin"}}
    for name in ("operational/kanban.html", "operational/boards.html",
                 "operational/pending.html"):
        html = env.get_template(name).render(**ctx)
        assert "<!DOCTYPE html>" in html, f"{name}: base.html nao resolveu"
        assert "{% " not in html, f"{name}: tag Jinja nao resolvida"
        assert "{#" not in html, f"{name}: comentario Jinja vazou para o HTML"


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
