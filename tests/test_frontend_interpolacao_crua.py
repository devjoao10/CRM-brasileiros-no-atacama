# -*- coding: utf-8 -*-
"""
SEC-XSS-05 - nenhum valor cru chega a um sink de HTML no front do CRM.

O SEC-XSS-04 achou quatro campos de lead crus em innerHTML so por varredura
MANUAL: email e whatsapp em segmentacao.html renderDetailTable, t.cor em
renderTagChips e o value="${savedVal}" de renderDiasDestino no editor de lead.
Nos quatro a linha em volta ja passava os outros campos por esc(), entao o
bloco inteiro PARECIA escapado. Este arquivo transforma a varredura em regra:
o quinto caso nao pode depender de alguem ler a tela certa.

A regra: todo valor que chega a um sink de HTML (innerHTML / outerHTML /
insertAdjacentHTML / document.write) e esc()/escapeHtml() - ou
DOMPurify.sanitize() - cobrindo o valor inteiro, numero, constante do codigo ou
uma entrada de PERMITIDAS (abaixo), que fixa o motivo e o numero de ocorrencias.
Quem julga isso e o analisador de tests/analisador_interpolacao_crua.py, e o
docstring dele e o contrato: o que conta como valor seguro, as extensoes e o que
a regra NAO pega.

Escopo: templates/**/*.html (blocos <script> inline, sem src=) e
static/js/**/*.js. O app de conversas fica fora: e follow-up.

Rodar:  python tests/test_frontend_interpolacao_crua.py
"""
import pathlib
import re
import sys

# `python tests/<arquivo>.py` poe tests/ em sys.path[0]: o analisador irmao importa direto.
from analisador_interpolacao_crua import (
    IDS_TEXTO, RE_ID_DE_ETAPA, Achado, Analise, analisa, arquivos_do_front,
    confere_permitidas, dessincronias, varre)

ROOT = pathlib.Path(__file__).resolve().parent.parent

falhas = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        falhas.append(msg)


# Piso do scan (hoje sao 23 arquivos): um glob quebrado - pasta movida, padrao
# errado - daria zero achados e a regra passaria verde e cega.
MINIMO_DE_ARQUIVOS = 20


# ---------------------------------------------------------------------------
# Allowlist: (arquivo, expressao normalizada) -> (ocorrencias, motivo).
# Cada entrada aceita EXATAMENTE `ocorrencias` achados crus com aquela chave:
# o que foi auditado e a contagem, nao a grafia. O motivo diz de onde o valor
# vem e por que ele nao carrega markup. Numero NAO vai para esc(): esc(0)
# devolve '' e apaga o zero da tela.
# ---------------------------------------------------------------------------
PERMITIDAS = {
    # -- status HTTP: Auth.apiRequest (static/js/auth.js) devolve o Response do
    #    fetch (ou null no 401); Response.status e inteiro.
    ("templates/segmentacao.html", "r.status"): (
        1, "r = await Auth.apiRequest('/api/segments'): Response.status, inteiro"),
    ("templates/tags.html", "resp.status"): (
        1, "resp = await Auth.apiRequest('/api/tags'): Response.status, inteiro"),
    ("templates/tarefas.html", "resp.status"): (
        1, "resp = await Auth.apiRequest(url) em fetchTasks: Response.status, inteiro"),
    ("templates/pipeline.html", "'<div style=\"padding:2rem;color:red\">Erro ao carregar o funil (' "
                                "+ resp.status + ')</div>'"): (
        1, "valor do innerHTML de erro em loadBoard: literal + resp.status, com resp = "
           "await Auth.apiRequest('/api/pipeline/board/' + currentFunnelId + '/meta'): "
           "Response.status, inteiro"),
    ("templates/equipes.html", "loadError"): (
        2, "loadError so recebe null (loadAllData) ou resp.status de Auth.apiRequest "
           "(fetchUsers, fetchTeams): Response.status, inteiro"),
    ("templates/leads.html", "status ? ' (' + status + ')' : ''"): (
        1, "status e o parametro de mostrarErro; os dois chamadores (loadLeads) "
           "passam nada ou resp.status de Auth.apiRequest, inteiro"),
    # -- numero do servidor que a regra numerica nao reconhece pelo nome
    ("templates/leads.html", "data.importados"): (
        1, "ImportResponse.importados: int, o contador `imported` de POST "
           "/api/leads/import (app/routers/leads.py); pode ser 0"),
    ("templates/leads.html", "data.erros"): (
        1, "ImportResponse.erros: int = len(errors) em POST /api/leads/import "
           "(app/routers/leads.py); pode ser 0"),
    ("templates/relatorios.html", "vol"): (
        1, "vol = dataObj[k] de data.breakdown.*: get_detailed_reports "
           "(app/routers/analytics.py) conta nome -> int; o texto e a chave k, "
           "que ja passa por esc(k)"),
    ("templates/pipeline.html", "diasLimite"): (
        1, "parametro de addStageRow: os chamadores passam literal (2, 3, 5, 7) ou "
           "s.dias_limite || 7 de funnels.etapas, gravado so por create_funnel e "
           "update_funnel via StageSchema.dias_limite: int ge=1"),
    # -- valor que o proprio codigo ou o navegador restringe
    ("templates/partials/_lead_edit_modal.html", "dest"): (
        4, "dest vem de getSelectedDestinos(): o data-value das opcoes ESTATICAS "
           "de #destinosDropdown neste parcial (Atacama, Uyuni, Santiago); "
           "setDestinosSelection so marca opcao existente, nunca cria"),
    ("templates/partials/_lead_edit_modal.html", "chegadaGeral"): (
        2, "value de #leadChegada, <input type=\"date\">: o navegador sanitiza o "
           ".value para data valida (aaaa-mm-dd) ou ''"),
    ("templates/partials/_lead_edit_modal.html", "partidaGeral"): (
        2, "value de #leadPartida, <input type=\"date\">: o navegador sanitiza o "
           ".value para data valida (aaaa-mm-dd) ou ''"),
    ("templates/relatorios.html", "emptyMsg"): (
        1, "parametro de renderTableGeneric: os tres chamadores (loadReports) "
           "passam literal - 'Sem origens cadastradas', 'Sem tags vinculadas', "
           "'Nenhum lead em funis'"),
    ("templates/gestao/pendencias.html", "count"): (
        1, "parametro de mk em renderTabs: os dois chamadores passam openCount(...), "
           "que devolve list.filter(...).length, inteiro; pode ser 0"),
}


# ---------------------------------------------------------------------------
# 1. A REGRA no front do CRM
# ---------------------------------------------------------------------------
print("1) todo valor que chega a um sink de HTML no CRM e esc(), numero, constante ou allowlist")

arquivos = arquivos_do_front(ROOT)
achados, dessincronizados = varre(ROOT, arquivos)
check(len(arquivos) >= MINIMO_DE_ARQUIVOS, f"arquivos de front analisados: {len(arquivos)}")
check(not dessincronizados, f"lexer em sincronia com a fonte real: {dessincronizados!a}")
# ...e o invariante reprova lexer dessincronizado (senao ele mesmo seria verde e cego).
for rel, codigo, esperado in (
    ("x.js", "el.innerHTML = `<b>${a", ["x.js: 2 template(s) ou ${...} sem fechamento"]),
    ("x.js", "f(a;", ["x.js:1: 1 ( [ { sem par no codigo"]),
    ("x.html", "<script>a()</script>\n<script>b()",
     ["x.html: 2 <script> inline, 1 regiao(oes) analisada(s)"]),
):
    obtido = dessincronias(Analise(rel, codigo))
    check(obtido == esperado, f"o invariante de sincronia reprova {codigo!a}: {obtido!a}")
problemas = confere_permitidas(achados, PERMITIDAS)
for linha in problemas:
    print(f"    {linha!a}")
check(not problemas, f"achados crus x allowlist: {len(problemas)} problema(s)")

# ---------------------------------------------------------------------------
# 2. Controle de mutacao no arquivo REAL: reverter um esc() auditado tem que acusar
# ---------------------------------------------------------------------------
print("\n2) reverter cada correcao (SEC-XSS-04, id de etapa) no arquivo real acusa o campo")

TRAVESSAO = chr(0x2014)
PLACEHOLDER = "'<span style=\"color:#aaa\">" + TRAVESSAO + "</span>'"
SEG = "templates/segmentacao.html"
MODAL = "templates/partials/_lead_edit_modal.html"
PIPE = "templates/pipeline.html"
MUTACOES = [
    (SEG, "<td>${esc(l.email)||" + PLACEHOLDER + "}</td>",
     "<td>${l.email||" + PLACEHOLDER + "}</td>", "l.email||" + PLACEHOLDER),
    (SEG, "<td>${esc(l.whatsapp)||" + PLACEHOLDER + "}</td>",
     "<td>${l.whatsapp||" + PLACEHOLDER + "}</td>", "l.whatsapp||" + PLACEHOLDER),
    (SEG, "${s?esc(t.cor):'#eee'}", "${s?t.cor:'#eee'}", "s?t.cor:'#eee'"),
    (MODAL, 'value="${esc(savedVal)}"', 'value="${savedVal}"', "savedVal"),
    # O id de etapa (StageSchema.id, texto livre): o <option> de loadTransferStages
    # (`s` de funnel.etapas.map) e o parametro `id` de addStageRow (recebe s.id).
    (PIPE, '<option value="${esc(s.id)}">', '<option value="${s.id}">', "s.id"),
    (PIPE, 'value="${esc(id)}"', 'value="${id}"', "id"),
]
for rel, antes, depois, esperado in MUTACOES:
    fonte = (ROOT / rel).read_text(encoding="utf-8")
    vezes = fonte.count(antes)
    # Troca que nao acontece exatamente uma vez deixa o controle vacuo.
    check(vezes == 1, f"{rel}: a correcao existe 1 vez na fonte ({vezes}x) {antes!a}")
    if vezes != 1:
        continue
    original = {a.expr for a in analisa(rel, fonte)}
    mutado = {a.expr for a in analisa(rel, fonte.replace(antes, depois))}
    check(esperado not in original and esperado in mutado,
          f"{rel}: sem o esc() o analisador acusa {esperado!a}")

# ---------------------------------------------------------------------------
# 3. Trechos sinteticos pelo MESMO ponto de entrada (uma afirmacao cada)
#    Os nomes do analisador citados nos comentarios abaixo (RE_*, METODOS_*,
#    metodos de Analise) estao em tests/analisador_interpolacao_crua.py.
# ---------------------------------------------------------------------------
print("\n3) trechos sinteticos: o que a regra acusa e o que ela aceita")


def crus(codigo):
    return [(a.expr, a.culpado) for a in analisa("sintetico.js", codigo)]


ESC_COM_REGEX = ("function esc(s) { return String(s).replace(/'/g, '&#39;')"
                 ".replace(/\"/g, '&quot;'); } el.innerHTML = `<i>${l.nome}</i>`;")
# O valor do innerHTML de loadStage (pipeline.html:704-707), com o stageId cru.
RHS_LOADSTAGE = ("st.items.length === 0\n"
                 "        ? '<div class=\"empty-column\">Nenhum lead</div>'\n"
                 "        : st.items.map(l => `<div>${esc(l.nome)}</div>`).join('') +\n"
                 "          (st.hasMore ? '<button data-stage=\"' + stageId + '\">Carregar mais</button>'"
                 " : '')")

ACUSA = [
    ("campo cru no <td>", "el.innerHTML = `<td>${l.nome}</td>`;", [("l.nome", "l.nome")]),
    ("esc(a) + b: o b e julgado", "el.innerHTML = `<td>${esc(a) + b}</td>`;",
     [("esc(a) + b", "b")]),
    ("foo(esc(a)): o foo e julgado", "el.innerHTML = `<td>${foo(esc(a))}</td>`;",
     [("foo(esc(a))", "foo(esc(a))")]),
    ("stage.id e texto livre", "el.innerHTML = `<i>${stage.id}</i>`;",
     [("stage.id", "stage.id")]),
    ("etapa_id e coluna String", "el.innerHTML = `<i>${t.etapa_id}</i>`;",
     [("t.etapa_id", "t.etapa_id")]),
    ("stageId (extensao: o mesmo id de etapa)", "el.innerHTML = `<i>${stageId}</i>`;",
     [("stageId", "stageId")]),
    # `.id` do parametro de callback sobre `.etapas`, seja qual for o nome dele
    ("s.id de funnel.etapas.map(s => ...) e id de etapa",
     "el.innerHTML = funnel.etapas.map(s => `<i>${s.id}</i>`).join('');", [("s.id", "s.id")]),
    # nome numerico LIGADO no arquivo: decide o valor, nao o nome
    ("const count = l.nome: o nome numerico nao salva o valor",
     "const count = l.nome;\nel.innerHTML = `<b>${count}</b>`;", [("count", "l.nome")]),
    ("const total = `${l.nome}`: o template vira markup pela resolucao",
     "const total = `${l.nome}`;\nel.innerHTML = `<b>${total}</b>`;", [("l.nome", "l.nome")]),
    ("aninhado: acusa o ponto de dentro, nao o de fora",
     "el.innerHTML = `<td>${ok ? `<b>${raw}</b>` : ''}</td>`;", [("raw", "raw")]),
    ("template sem tag aninhado em argumento tambem vira markup (aninhado em MARKUP)",
     "el.innerHTML = `<td>${f(`${l.nome}`)}</td>`;",
     [("f(`${l.nome}`)", "f(`${l.nome}`)"), ("l.nome", "l.nome")]),
    ("corpo de .map().join()",
     "el.innerHTML = `<ul>${items.map(x => `<i>${x.nome}</i>`).join('')}</ul>`;",
     [("x.nome", "x.nome")]),
    ("template sem tag que chega ao markup pela resolucao",
     "const label = `${l.nome}`;\nel.innerHTML = `<b>${label}</b>`;", [("l.nome", "l.nome")]),
    ("raiz por sink sem tag", "el.innerHTML = `${l.nome}`;", [("l.nome", "l.nome")]),
    ("toLocaleString de texto devolve o texto",
     "el.innerHTML = `<i>${x.nome.toLocaleString()}</i>`;",
     [("x.nome.toLocaleString()", "x.nome.toLocaleString()")]),
    ("aspa dentro de regex nao descarrila o lexer", ESC_COM_REGEX, [("l.nome", "l.nome")]),
    # -- um caso por sink, com template SEM tag: so a raiz do sink o torna markup.
    # RE_SINK_ATRIB, o `\+?` de `.innerHTML +=`
    ("sink innerHTML +=", "el.innerHTML += `${l.nome}`;", [("l.nome", "l.nome")]),
    # RE_SINK_ATRIB, a alternativa `outerHTML`
    ("sink outerHTML =", "el.outerHTML = `${l.nome}`;", [("l.nome", "l.nome")]),
    # RE_SINK_CHAMADA, `insertAdjacentHTML(pos, html)`: o HTML e o 2o argumento
    ("sink insertAdjacentHTML",
     "el.insertAdjacentHTML('beforeend', `${l.nome}`);", [("l.nome", "l.nome")]),
    # RE_SINK_CHAMADA, `document.write(`: o `writeln?` antigo nao casava `write(`
    ("sink document.write", "document.write(`${l.nome}`);", [("l.nome", "l.nome")]),
    # RE_SINK_CHAMADA, o `(?:ln)?` do mesmo token
    ("sink document.writeln", "document.writeln(`${l.nome}`);", [("l.nome", "l.nome")]),
    # -- um caso por ramo permissivo do classificador: o que e impresso tem que ser julgado.
    # `a && b`: so o b pode carregar markup (o falsy de a imprime false/0/''), entao julga o b
    ("a && b: o b e julgado", "el.innerHTML = `<td>${c && y.nome}</td>`;",
     [("c && y.nome", "y.nome")]),
    # `a || b`: qualquer operando pode ser impresso, o de depois do || tambem
    ("esc(a) || b: o b e julgado", "el.innerHTML = `<td>${esc(a) || y.nome}</td>`;",
     [("esc(a) || y.nome", "y.nome")]),
    # `a ?? b`: idem; esc(a) na frente isola o b (um `a` solto ja seria cru por si so)
    ("esc(a) ?? b: o b e julgado", "el.innerHTML = `<td>${esc(a) ?? y.nome}</td>`;",
     [("esc(a) ?? y.nome", "y.nome")]),
    # ternario `c ? a : b`: o 2o ramo e julgado
    ("ternario: o 2o ramo e julgado", "el.innerHTML = `<td>${c ? 'a' : y.nome}</td>`;",
     [("c ? 'a' : y.nome", "y.nome")]),
    # ternario `c ? a : b`: o 1o ramo e julgado
    ("ternario: o 1o ramo e julgado", "el.innerHTML = `<td>${c ? y.nome : 'a'}</td>`;",
     [("c ? y.nome : 'a'", "y.nome")]),
    # `X.map(cb)`: o veredito vem do que o callback devolve (aqui o campo cru, sem template)
    (".map(cb): o retorno do callback e julgado",
     "el.innerHTML = `<td>${rows.map(r => r.nome).join(', ')}</td>`;",
     [("rows.map(r => r.nome).join(', ')", "r.nome")]),
    # `X.join(sep)`: o receptor (o array) e julgado, nao so o separador
    (".join(sep): o receptor e julgado", "el.innerHTML = `<td>${y.tags.join(', ')}</td>`;",
     [("y.tags.join(', ')", "y.tags")]),
    # METODOS_PRESERVAM: charAt/toUpperCase devolvem o texto do receptor (equipes.html:745)
    ("charAt/toUpperCase: o receptor e julgado",
     "el.innerHTML = `<td>${m.nome.charAt(0).toUpperCase()}</td>`;",
     [("m.nome.charAt(0).toUpperCase()", "m.nome")]),
    # `NOME(...)` do arquivo: o veredito vem do `return` da funcao (aqui, o parametro)
    ("funcao do arquivo: o return e julgado",
     "function fmt(x) { return x; }\nel.innerHTML = `<td>${fmt(y.nome)}</td>`;",
     [("fmt(y.nome)", "x")]),
    # _escritas_no_conteudo: `T[k] = ...` tira o status de constante da tabela
    ("tabela const escrita (T[k] = ...) nao e constante",
     "const T = { a: 'x' };\nT[k] = y.nome;\nel.innerHTML = `<td>${T[k]}</td>`;",
     [("T[k]", "T[k]")]),
    # _escritas_no_conteudo: `T.push(...)` (outro caminho) tambem
    ("tabela const escrita (T.push) nao e constante",
     "const T = ['a'];\nT.push(y.nome);\nel.innerHTML = `<td>${T[0]}</td>`;",
     [("T[0]", "T[0]")]),
    # _escritas_no_conteudo: o mesmo vocabulario de escrita vale para tabela e para local
    ("tabela const escrita (T.x = ...) nao e constante",
     "const T = { x: 'a' };\nT.x = y.nome;\nel.innerHTML = `<td>${T.x}</td>`;", [("T.x", "T.x")]),
    ("tabela const escrita (Object.assign) nao e constante",
     "const T = { a: 'x' };\nObject.assign(T, {a: y.nome});\nel.innerHTML = `<td>${T.a}</td>`;",
     [("T.a", "T.a")]),
    ("tabela const escrita (T.unshift) nao e constante",
     "const T = ['a'];\nT.unshift(y.nome);\nel.innerHTML = `<td>${T[0]}</td>`;", [("T[0]", "T[0]")]),
    ("local escrito por splice",
     "const parts=[]; parts.splice(0,0,y.nome); el.innerHTML=`<ul>${parts.join('')}</ul>`",
     [("parts.join('')", "y.nome")]),
    ("local escrito por fill",
     "const parts = [''];\nparts.fill(y.nome);\nel.innerHTML = `<i>${parts.join('')}</i>`;",
     [("parts.join('')", "y.nome")]),
    # numerico(): o receptor de .toLocaleString() segue a regra do nome ligado
    ("toLocaleString de nome numerico ligado a texto",
     "const total = l.nome;\nel.innerHTML = `<b>${total.toLocaleString()}</b>`;",
     [("total.toLocaleString()", "total.toLocaleString()")]),
    # _literal_puro: tabela com valor nao literal nao e constante do codigo
    ("tabela const com valor nao literal nao e constante",
     "const T = { a: y.nome };\nel.innerHTML = `<td>${T.a}</td>`;", [("T.a", "T.a")]),
    # esc(...) so cobre o valor quando e a expressao INTEIRA: esc(a).concat(b) deixa o b
    ("esc(a).concat(b): esc(...) so vale sozinho",
     "el.innerHTML = `<td>${esc(a).concat(b)}</td>`;",
     [("esc(a).concat(b)", "esc(a).concat(b)")]),
    # -- o valor do sink FORA de template tambem e julgado (e acusado no proprio sink)
    ("sink: concatenacao", "el.innerHTML = '<td>' + l.nome + '</td>';",
     [("'<td>' + l.nome + '</td>'", "l.nome")]),
    ("sink: valor cru direto", "el.innerHTML = l.nome;", [("l.nome", "l.nome")]),
    ("sink: += cru direto", "el.innerHTML += l.nome;", [("l.nome", "l.nome")]),
    ("sink: insertAdjacentHTML com concatenacao",
     "el.insertAdjacentHTML('beforeend', '<b>' + x);", [("'<b>' + x", "x")]),
    ("sink: [...].join('')", "el.innerHTML = ['<b>', x, '</b>'].join('');",
     [("['<b>', x, '</b>'].join('')", "x")]),
    ("sink: variavel local montada com +=",
     "let h = '';\nh += '<b>' + y.nome;\nel.innerHTML = h;", [("h", "y.nome")]),
    ("sink: .map que devolve concatenacao",
     "el.innerHTML = rows.map(r => '<i>' + r.nome + '</i>').join('');",
     [("rows.map(r => '<i>' + r.nome + '</i>').join('')", "r.nome")]),
    ("sink: o formato de pipeline.html:704-707 sem o esc(stageId)",
     "function loadStage(stageId, st) {\n    body.innerHTML = " + RHS_LOADSTAGE + ";\n}",
     [(" ".join(RHS_LOADSTAGE.split()), "stageId")]),
    # DOMPurify.sanitize(...) so cobre o valor quando e a expressao INTEIRA
    ("DOMPurify.sanitize(a) + b: o b e julgado",
     "el.innerHTML = DOMPurify.sanitize(a) + l.nome;", [("DOMPurify.sanitize(a) + l.nome", "l.nome")]),
    # -- cantos que falhavam abertos: `{{` nao e objeto literal; funcao sem nome no indice
    ("{{ ... }} do Jinja num <script> e valor do servidor",
     "const nome = {{ nome|tojson }};\nel.innerHTML = `<b>${nome}</b>`;", [("nome", "{{ nome|tojson }}")]),
    ("funcao declarada junto com outra variavel (o indice nao a nomeia)",
     "const a = 1, fmt = (x) => x;\nel.innerHTML = `<td>${fmt(y.nome)}</td>`;",
     [("fmt(y.nome)", "fmt(y.nome)")]),
    # -- um caso por ramo do classificador que ainda nao tinha controle
    # `.split(...)[N]`: o pedaco e texto do receptor (o primeiro nome de m.nome)
    ("split(...)[N]: o receptor e julgado",
     "el.innerHTML = `<td>${m.nome.split(' ')[0]}</td>`;", [("m.nome.split(' ')[0]", "m.nome")]),
    # array literal: todo elemento e julgado (esc(a) na frente isola o 2o elemento)
    ("array literal: todo elemento e julgado",
     "el.innerHTML = `<td>${[esc(a), y.nome].join('')}</td>`;",
     [("[esc(a), y.nome].join('')", "y.nome")]),
    # `await X`: vale o X
    ("await X: o X e julgado", "el.innerHTML = `<td>${await f(y.nome)}</td>`;",
     [("await f(y.nome)", "f(y.nome)")]),
    # `new X(...)`: so `new Date(...)` e seguro
    ("new X(...) que nao e Date", "el.innerHTML = `<td>${new X(y.nome)}</td>`;",
     [("new X(y.nome)", "new X(y.nome)")]),
    # operador virgula: vale o ultimo operando
    ("virgula: o ultimo operando e julgado", "el.innerHTML = `<td>${(a, y.nome)}</td>`;",
     [("(a, y.nome)", "y.nome")]),
    # atribuicao como expressao: vale o lado direito
    ("atribuicao como expressao: o lado direito e julgado",
     "el.innerHTML = `<td>${(z = y.nome)}</td>`;", [("(z = y.nome)", "y.nome")]),
]
for nome, codigo, esperado in ACUSA:
    obtido = crus(codigo)
    check(obtido == esperado, f"acusa {nome}: {obtido!a}")

# Mesma ordem de segmentacao.html: a `pills` de renderFilterPills (texto CRU,
# escapado no fim) vem DEPOIS da que guarda o retorno dela.
PILLS = """
function loadSegments(d) {
    grid.innerHTML = d.segments.map(s => {
        const pills = renderFilterPills(s.filtros);
        return `<div>${pills ? `<div class="segment-filters-pills">${pills}</div>` : ''}</div>`;
    }).join('');
}
function renderFilterPills(f) {
    if (!f) return '';
    const pills = [];
    if (f.search) pills.push(`Texto: "${f.search}"`);
    if (f.destino) pills.push(`Destino: ${f.destino}`);
    return pills.map(p => `<span class="filter-pill">${esc(p)}</span>`).join('');
}
"""
ACEITA = [
    ("id inteiro", "el.innerHTML = `<i>${l.id}</i>`;"),
    ("esc() cobrindo o valor", "el.innerHTML = `<i>${esc(l.nome)}</i>`;"),
    ("ternario de literais", "el.innerHTML = `<i>${x ? 'a' : 'b'}</i>`;"),
    ("tamanho de lista", "el.innerHTML = `<i>${list.length}</i>`;"),
    ("consulta a objeto literal const",
     "const CONST = { a: 'x', b: 'y' };\nel.innerHTML = `<i>${CONST[k]}</i>`;"),
    ("data formatada", "el.innerHTML = `<i>${new Date(x).toLocaleDateString('pt-BR')}</i>`;"),
    ("DOMPurify.sanitize() cobrindo o valor do sink", "el.innerHTML = DOMPurify.sanitize(l.nome);"),
    ("as duas `pills` de segmentacao.html (escopo, nao nome)", PILLS),
]
for nome, codigo in ACEITA:
    obtido = crus(codigo)
    check(not obtido, f"aceita {nome} (achados: {obtido!a})")

# Comentario nao conta - e o mesmo trecho FORA do comentario acusa, senao o
# "nenhum achado" nao provaria nada. O `a` antes do /* faz a barra ser divisao:
# sem isso um lexer que nao trata comentario leria `/* ... </` como regex e
# engoliria o template, passando por acaso.
for comentado, ativo in (
    ("// el.innerHTML = `<td>${l.nome}</td>`;", "el.innerHTML = `<td>${l.nome}</td>`;"),
    ("x = a /* `<td>${l.nome}</td>` */;", "x = a + `<td>${l.nome}</td>`;"),
):
    check(not crus(comentado) and crus(ativo) == [("l.nome", "l.nome")],
          f"comentario nao conta: {comentado!a}")

# ---------------------------------------------------------------------------
# 4. Higiene da allowlist: cada verificacao reprova o caso que existe para pegar
# ---------------------------------------------------------------------------
print("\n4) higiene da allowlist")

A1 = Achado("x.html", 10, "foo", "foo")
BOM_MOTIVO = "motivo concreto: de onde vem e por que nao carrega markup"
for nome, achs, perm, esperado in (
    ("achado sem entrada", [A1], {}, "nao classificado"),
    ("entrada sem achado (allowlist nao apodrece)", [], {("x.html", "foo"): (1, BOM_MOTIVO)},
     "entrada morta"),
    ("contagem auditada diferente da achada", [A1], {("x.html", "foo"): (2, BOM_MOTIVO)},
     "contagem"),
    ("ocorrencia nova da mesma expressao", [A1, A1._replace(linha=20)],
     {("x.html", "foo"): (1, BOM_MOTIVO)}, "contagem"),
    ("motivo curto", [A1], {("x.html", "foo"): (1, "porque sim")}, "motivo curto"),
):
    probs = confere_permitidas(achs, perm)
    check(any(p.startswith(esperado) for p in probs), f"reprova {nome} ({probs!a})")
check(not confere_permitidas([A1], {("x.html", "foo"): (1, BOM_MOTIVO)}),
      "aceita entrada com contagem certa e motivo concreto")

# ---------------------------------------------------------------------------
# 5. IDS_TEXTO x app/models e app/schemas: `*_id` texto novo reprova ate ser classificado
# ---------------------------------------------------------------------------
print("\n5) excecoes de id texto conferidas contra app/models e app/schemas")

RE_COLUNA_ID_TEXTO = re.compile(r"(\w+_id)\s*=\s*(?:Column|mapped_column)\(\s*(?:String|Text)")
RE_CAMPO_ID_TEXTO = re.compile(r"(\w+_id)\s*:\s*(?:Optional\[)?str")


def fora_da_regra(nomes):
    """`*_id` texto que a regra numerica ainda aceitaria pelo nome (nem IDS_TEXTO, nem id de etapa)."""
    return sorted(n for n in nomes if n not in IDS_TEXTO and not RE_ID_DE_ETAPA.search(n))


colunas, campos = set(), set()
for p in sorted((ROOT / "app" / "models").rglob("*.py")):
    colunas |= set(RE_COLUNA_ID_TEXTO.findall(p.read_text(encoding="utf-8")))
for p in sorted((ROOT / "app" / "schemas").rglob("*.py")):
    campos |= set(RE_CAMPO_ID_TEXTO.findall(p.read_text(encoding="utf-8")))
check(bool(colunas), f"o scan acha as colunas String/Text *_id dos models ({sorted(colunas)})")
check(bool(campos), f"o scan acha os campos str *_id dos schemas ({sorted(campos)})")
check(not fora_da_regra(colunas | campos),
      f"todo *_id texto de models e schemas esta em IDS_TEXTO ou e id de etapa "
      f"(fora: {fora_da_regra(colunas | campos)})")
novo = RE_COLUNA_ID_TEXTO.findall("    codigo_id = Column(String(40), nullable=True)\n"
                                  "    nota_id = Column(Text)\n    ref_id = mapped_column(String(20))\n")
check(fora_da_regra(novo) == ["codigo_id", "nota_id", "ref_id"],
      f"controle: coluna String/Text *_id nova (Column ou mapped_column) fica fora ({novo})")
novo = RE_CAMPO_ID_TEXTO.findall("    codigo_id: Optional[str] = None\n    ref_id: str\n")
check(fora_da_regra(novo) == ["codigo_id", "ref_id"],
      f"controle: campo str *_id novo de schema fica fora ({novo})")
check(crus("el.innerHTML = `<i>${t.google_calendar_event_id}</i>`;")
      == [("t.google_calendar_event_id", "t.google_calendar_event_id")],
      "a regra numerica respeita IDS_TEXTO")


print()
if falhas:
    print(f"{len(falhas)} FALHA(S)")
    sys.exit(1)
print("OK: nenhum valor cru em sink de HTML do CRM fora da allowlist")
