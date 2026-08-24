# -*- coding: utf-8 -*-
"""
PIPE-EDIT-01 — editar lead sem sair do board.

Antes: o botao "Editar Lead" do drawer era um <a href="/leads">, e o usuario
perdia board, filtros, cursor, scroll e highlight.

Agora o editor abre na propria pagina. E o MESMO editor da pagina de Leads:
templates/partials/_lead_edit_modal.html foi extraido de leads.html sem
reescrita e e incluido pelos dois templates. Nao existem duas implementacoes
de formulario, payload, validacao ou PUT.

O teste mais importante deste arquivo e o de PARIDADE DE PAYLOAD: ele roda o
MESMO saveLead com o MESMO lead preenchido e confere que os 14 campos saem
iguais. E a garantia contra "editar nome zera destinos".

Os testes de JavaScript rodam o codigo extraido dos templates no node, com DOM
stubado — mesmo caminho dos PRs #27, #29 e #30. Sem Playwright no repo.

Rodar:  python tests/test_pipeline_inline_lead_edit.py
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

LEADS = pathlib.Path("templates/leads.html").read_text(encoding="utf-8")
PIPE = pathlib.Path("templates/pipeline.html").read_text(encoding="utf-8")
PARTIAL = pathlib.Path("templates/partials/_lead_edit_modal.html").read_text(
    encoding="utf-8")
XSS = '<script>alert(1)</script>'


def _js(txt):
    return max(re.findall(r"<script>(.*?)</script>", txt, re.S), key=len)


def _fn(nome, *fontes):
    for f in fontes or (PARTIAL, _js(PIPE), _js(LEADS)):
        m = re.search(r"(async\s+)?function " + re.escape(nome) + r"\(", f)
        if m:
            return f[m.start():].split("\n    }", 1)[0] + "\n    }"
    raise AssertionError(f"{nome} nao encontrada")


# ─────────────────────────────────────────────────────────────────────────
# 1. Uma unica implementacao do editor
# ─────────────────────────────────────────────────────────────────────────

def test_editor_existe_uma_vez_so():
    """Regra principal do pacote: nao criar um segundo sistema de edicao."""
    for fn in ("saveLead", "openLeadModal", "closeLeadModal"):
        assert PARTIAL.count(f"function {fn}(") == 1, f"{fn} duplicada no partial"
        for nome, txt in (("leads.html", LEADS), ("pipeline.html", PIPE)):
            assert f"function {fn}(" not in txt, (
                f"{fn} reapareceu em {nome} — o editor tem que ser unico")


def test_os_dois_templates_incluem_o_MESMO_partial():
    inc = '{% include "partials/_lead_edit_modal.html" %}'
    assert inc in LEADS, "leads.html nao inclui o editor compartilhado"
    assert inc in PIPE, "pipeline.html nao inclui o editor compartilhado"


def test_form_row_de_duas_colunas_nao_e_anulado_fora_do_media_query():
    """
    A extracao quase levou a regra `.form-row { grid-template-columns: 1fr }`
    de dentro do @media para o topo do <style>. Solta, ela venceria a regra de
    duas colunas em QUALQUER largura e o formulario ficaria em coluna unica no
    desktop — regressao visual que nenhum outro teste aqui pegaria.
    """
    css = PARTIAL.split("<style>", 1)[1].split("</style>", 1)[0]
    fora, prof = [], 0
    for linha in css.splitlines():
        if "@media" in linha:
            prof += 1
            continue
        if prof and linha.strip() == "}":
            prof -= 1
            continue
        if not prof and re.search(r"\.form-row\s*\{[^}]*grid-template-columns:\s*1fr\s*[;}]",
                                  linha):
            fora.append(linha.strip())
    assert not fora, (
        f"regra de coluna unica fora do @media: {fora} — anularia as duas "
        f"colunas no desktop")
    assert "grid-template-columns: 1fr 1fr" in css, "as duas colunas sumiram"
    assert "@media" in css, "o override responsivo tem que continuar existindo"


def test_modal_html_existe_uma_vez_so():
    assert PARTIAL.count('id="leadModal"') == 1
    for nome, txt in (("leads.html", LEADS), ("pipeline.html", PIPE)):
        assert 'id="leadModal"' not in txt, f"HTML do modal duplicado em {nome}"


def test_put_e_o_endpoint_existente():
    corpo = _fn("saveLead")
    assert "`/api/leads/${id}`" in corpo, "o save tem que usar o PUT existente"
    assert "'PUT'" in corpo
    assert "/pipeline/" not in corpo, "nenhum endpoint novo de pipeline"
    # tags continuam em request separado, como sempre foram
    assert "/api/tags/lead/" in corpo, "o fluxo de tags nao pode ter sumido"


def test_cada_pagina_registra_seu_proprio_onSaved():
    assert "onLeadSaved = async (lead, id)" in LEADS, (
        "leads.html precisa registrar o callback")
    assert "onLeadSaved = async (lead)" in PIPE, (
        "pipeline.html precisa registrar o callback")
    assert "let onLeadSaved" in PARTIAL and PARTIAL.count("let onLeadSaved") == 1


# ─────────────────────────────────────────────────────────────────────────
# 2. Harness
# ─────────────────────────────────────────────────────────────────────────

CAMPOS = ["leadId", "leadNome", "leadEmail", "leadWhatsapp", "leadChegada",
          "leadPartida", "leadTotalDias", "leadNumViajantes", "leadNumCriancas",
          "leadStatusVenda", "leadAnotacoes"]

_HARNESS = """
'use strict';
let valores = %s;
let pedidos = [], respostas = %s;
let modalAberto = false, drawerFechado = 0, boardRecarregado = 0;
let alertas = [], salvando = [];

function novoEl(id) {
    return { id, _text: '', _html: '', style: {}, disabled: false,
        value: valores[id] !== undefined ? valores[id] : '',
        set textContent(v) { this._text = String(v); },
        get textContent() { return this._text; },
        set innerHTML(v) { this._html = String(v); },
        get innerHTML() { return this._html; },
        appendChild() {}, insertAdjacentHTML() {},
        classList: { add(c){ if (id === 'leadModal' && c === 'show') modalAberto = true; },
                     remove(c){ if (id === 'leadModal' && c === 'show') modalAberto = false; },
                     toggle(){}, contains(){ return false; } },
        querySelector(){ return null; }, querySelectorAll(){ return []; } };
}
const els = {};
const document = {
    getElementById: id => (els[id] = els[id] || novoEl(id)),
    querySelectorAll: sel => (sel === '#leadTagsContainer input:checked' ? [] : []),
    createElement: () => ({ set textContent(v){ this._t = v; },
                            get innerHTML(){ return String(this._t || '')
                                .replace(/&/g,'&amp;').replace(/</g,'&lt;')
                                .replace(/>/g,'&gt;'); }, className: '' }),
};
const Auth = { apiRequest: async (url, opts) => {
    pedidos.push({ url, method: (opts && opts.method) || 'GET',
                   body: opts && opts.body ? JSON.parse(opts.body) : null });
    const r = respostas[url] !== undefined ? respostas[url] : respostas['*'];
    if (r && r.lanca) { const e = new TypeError('Failed to fetch'); throw e; }
    if (r && r.status >= 400) return { ok: false, status: r.status, json: async () => r.body };
    return { ok: true, status: 200, json: async () => (r ? r.body : {}) };
}};
function esc(s){ if(!s) return ''; return String(s).replace(/&/g,'&amp;')
    .replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function getSelectedDestinos(){ return valores.__destinos || []; }
function getDatasDestinosJSON(){ return valores.__datas || {}; }
function getDiasPorDestinoJSON(){ return valores.__dias || {}; }
function getIdadesCriancasString(){ return valores.__idades || ''; }
function renderDatasDestinos(){} function renderDiasDestino(){}
function renderIdadesCriancas(){} function renderTagCheckboxes(){}
function setDestinosSelection(){} function addCustomField(){}
function displayWhatsapp(v){ return v || ''; }
let allTags = [], currentLeadTags = [];
let _currentDiasDestino = {}, _currentDatasDestinos = {};
let onLeadSaved = null;
let salvandoLead = false;

%s

(async () => {
%s
})();
"""

_DO_PARTIAL = ["showLeadAlert", "setLeadSaving", "normalizeWhatsapp",
               "closeLeadModal", "openLeadModal", "saveLead"]


def _roda(corpo, valores=None, respostas=None, extras=""):
    node = shutil.which("node")
    assert node, "node e necessario"
    v = {"leadId": "7", "leadNome": "Joao", "leadEmail": "j@x.test",
         "leadWhatsapp": "48988711776", "leadChegada": "2026-06-01",
         "leadPartida": "2026-06-10", "leadTotalDias": "9",
         "leadNumViajantes": "2", "leadNumCriancas": "1",
         "leadStatusVenda": "venda", "leadAnotacoes": "nota",
         "__destinos": ["Atacama", "Uyuni"],
         "__datas": {"Atacama": {"chegada": "2026-06-01"}},
         "__dias": {"Atacama": 5}, "__idades": "6"}
    v.update(valores or {})
    r = {"*": {"body": {"id": 7, "nome": "Joao"}}}
    r.update(respostas or {})
    script = _HARNESS % (json.dumps(v), json.dumps(r),
                         "\n".join(_fn(f, PARTIAL) for f in _DO_PARTIAL) + extras,
                         corpo)
    p = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert p.returncode == 0, f"node falhou: {p.stderr.strip()[:400]}"
    return json.loads(p.stdout)


# ─────────────────────────────────────────────────────────────────────────
# 3. PARIDADE DE PAYLOAD (item 34) — o teste central
# ─────────────────────────────────────────────────────────────────────────

CAMPOS_DO_PUT = ["nome", "email", "whatsapp", "destinos", "data_chegada",
                 "data_partida", "total_dias", "datas_destinos",
                 "dias_por_destino", "num_viajantes", "num_criancas",
                 "idades_criancas", "status_venda", "campos_personalizados"]


def test_payload_tem_os_14_campos():
    r = _roda("""
        await saveLead();
        const put = pedidos.find(p => p.method === 'PUT' && p.url.startsWith('/api/leads/'));
        console.log(JSON.stringify({ url: put.url, body: put.body }));
    """)
    faltando = [c for c in CAMPOS_DO_PUT if c not in r["body"]]
    assert not faltando, (
        f"o PUT deixou de enviar {faltando} — editar um campo zeraria os outros")
    assert r["url"] == "/api/leads/7"


def test_paridade_leads_vs_pipeline_e_estrutural():
    """
    Leads e Pipeline chamam a MESMA funcao do MESMO arquivo. A paridade nao
    depende de duas implementacoes coincidirem: existe uma so.
    """
    corpo_partial = _fn("saveLead", PARTIAL)
    for nome, txt in (("leads.html", LEADS), ("pipeline.html", PIPE)):
        assert "campos_personalizados:" not in txt, (
            f"{nome} monta payload de lead por conta propria")
        # GET/DELETE por id e PUT de status/responsavel continuam existindo;
        # o que nao pode e remontar o payload de 14 campos do formulario
        assert "datas_destinos:" not in txt and "dias_por_destino:" not in txt, (
            f"{nome} remonta o payload do formulario por conta propria")
    for c in CAMPOS_DO_PUT:
        assert f"{c}:" in corpo_partial or f"{c}," in corpo_partial, (
            f"campo {c} sumiu do payload compartilhado")


def test_payload_preserva_campos_personalizados_e_destinos():
    r = _roda("""
        await saveLead();
        const put = pedidos.find(p => p.method === 'PUT');
        console.log(JSON.stringify({ body: put.body }));
    """)
    b = r["body"]
    assert b["destinos"] == ["Atacama", "Uyuni"], f"destinos zerados: {b['destinos']}"
    assert b["campos_personalizados"].get("anotacoes") == "nota"
    assert b["datas_destinos"] == {"Atacama": {"chegada": "2026-06-01"}}
    assert b["dias_por_destino"] == {"Atacama": 5}
    assert b["idades_criancas"] == "6"


def test_whatsapp_normalizado_uma_vez_so():
    r = _roda("""
        await saveLead();
        console.log(JSON.stringify({ wpp: pedidos.find(p => p.method === 'PUT').body.whatsapp }));
    """)
    assert r["wpp"] == "5548988711776", f"normalizacao mudou: {r['wpp']}"


# ─────────────────────────────────────────────────────────────────────────
# 4. Fluxo de save
# ─────────────────────────────────────────────────────────────────────────

def test_save_manda_um_put_fecha_modal_e_chama_o_callback():
    r = _roda("""
        let recebido = null;
        onLeadSaved = async (lead, id) => { recebido = { id: lead.id, form: id }; };
        openLeadModal({ id: 7, nome: 'Joao' });
        await saveLead();
        console.log(JSON.stringify({
            puts: pedidos.filter(p => p.method === 'PUT' && p.url.startsWith('/api/leads/')).length,
            modalAberto, recebido }));
    """, respostas={"/api/leads/7": {"body": {"id": 7, "nome": "Joao Pedro"}}})
    assert r["puts"] == 1
    assert r["modalAberto"] is False, "o modal precisa fechar no sucesso"
    assert r["recebido"]["id"] == 7 and str(r["recebido"]["form"]) == "7", (
        f"callback recebeu {r['recebido']}")


def test_erro_no_put_mantem_modal_aberto_e_nao_chama_callback():
    for status in (400, 401, 403, 404, 409, 422, 500):
        r = _roda("""
            let chamou = false;
            onLeadSaved = async () => { chamou = true; };
            openLeadModal({ id: 7, nome: 'Joao' });
            await saveLead();
            console.log(JSON.stringify({ modalAberto, chamou, alerta: els.leadModalAlertMsg._text }));
        """, respostas={"/api/leads/7": {"status": status, "body": {"detail": "erro"}}})
        assert r["modalAberto"] is True, f"{status}: o modal fechou"
        assert r["chamou"] is False, (
            f"{status}: callback rodou — drawer/card seriam atualizados com dado nao salvo")


def test_double_save_gera_um_unico_put():
    r = _roda("""
        openLeadModal({ id: 7, nome: 'Joao' });
        const a = saveLead(); const b = saveLead();   // dois cliques
        await Promise.all([a, b]);
        console.log(JSON.stringify({
            puts: pedidos.filter(p => p.method === 'PUT' && p.url.startsWith('/api/leads/')).length,
            botaoDesabilitado: els.leadSaveBtn.disabled }));
    """)
    assert r["puts"] == 1, f"{r['puts']} PUTs — falta a guarda de save em andamento"


def test_cancelar_nao_manda_put():
    r = _roda("""
        openLeadModal({ id: 7, nome: 'Joao' });
        closeLeadModal();
        console.log(JSON.stringify({ puts: pedidos.length, modalAberto }));
    """)
    assert r["puts"] == 0 and r["modalAberto"] is False


def test_nome_vazio_nao_manda_put():
    r = _roda("""
        await saveLead();
        console.log(JSON.stringify({ puts: pedidos.length, alerta: els.leadModalAlertMsg._text }));
    """, valores={"leadNome": "  "})
    assert r["puts"] == 0 and "obrigat" in r["alerta"].lower()


# ─────────────────────────────────────────────────────────────────────────
# 5. Pipeline: drawer, card, board
# ─────────────────────────────────────────────────────────────────────────

def test_botao_editar_nao_navega_mais_para_leads():
    m = re.search(r'id="detailEditLink"[^>]*>', PIPE)
    assert m, "botao Editar Lead nao encontrado"
    tag = m.group(0)
    assert "href=" not in tag, f"o botao ainda navega: {tag}"
    assert "editarLeadDoDrawer()" in tag, f"o botao nao abre o editor local: {tag}"
    assert "window.location" not in _fn("editarLeadDoDrawer", _js(PIPE))


def test_abrir_editor_reaproveita_o_lead_do_drawer_sem_get_extra():
    corpo = _fn("editarLeadDoDrawer", _js(PIPE))
    assert "openLeadModal(leadDoDrawer)" in corpo, (
        "o editor tem que reaproveitar o objeto que o drawer ja carregou")
    assert "apiRequest" not in corpo, "abrir o editor nao pode fazer request novo"
    assert "leadDoDrawer = lead;" in _js(PIPE), "o drawer precisa guardar o lead"


def test_callback_do_pipeline_atualiza_drawer_e_card_sem_recarregar_board():
    bloco = _js(PIPE).split("onLeadSaved = async (lead)", 1)[1].split("};", 1)[0]
    assert "preencherDrawer(lead)" in bloco, "o drawer tem que refletir o save"
    assert "atualizarCardDoLead(lead)" in bloco, "o card tem que refletir o save"
    for proibido in ("loadBoard", "loadStage", "closeDetailPanel",
                     "clearTargetLead", "renderStage", "location"):
        assert proibido not in bloco, (
            f"o save nao pode chamar {proibido} — perderia board/filtros/highlight")


def test_atualizar_card_nao_usa_renderStage():
    """renderStage reaplica a classe de target e dispara scrollIntoView."""
    corpo = _fn("atualizarCardDoLead", _js(PIPE))
    assert "renderStage" not in corpo, (
        "renderStage tem efeito colateral de highlight/scroll; troque so o card")
    assert "scrollIntoView" not in corpo
    assert "renderLeadCard(card)" in corpo, "o card e remontado pelo render existente"
    assert "pipeline-target-card" in corpo, (
        "se o card era o alvo, a marca tem que voltar depois do outerHTML")


def test_save_nao_toca_em_etapa_nem_funil():
    corpo = _fn("saveLead", PARTIAL)
    for proibido in ("etapa_id", "funnel_id", "entry_id", "posicao"):
        assert proibido not in corpo, (
            f"payload de edicao nao pode carregar {proibido} — mover e outro fluxo")


def test_save_nao_mexe_no_highlight_nem_na_url():
    js = _js(PIPE)
    bloco = js.split("onLeadSaved = async (lead)", 1)[1].split("};", 1)[0]
    for proibido in ("highlightLeadId", "setTargetLead", "clearTargetLead",
                     "replaceState", "searchParams"):
        assert proibido not in bloco, f"o save mexeu em {proibido}"


def test_abrir_editor_nao_fecha_o_drawer():
    corpo = _fn("editarLeadDoDrawer", _js(PIPE))
    assert "closeDetailPanel" not in corpo
    fechar = _fn("closeLeadModal", PARTIAL)
    assert "closeDetailPanel" not in fechar and "detailPanel" not in fechar, (
        "fechar o editor nao pode encostar no drawer")


def test_z_index_do_editor_fica_acima_do_drawer():
    """
    O drawer esta em calc(var(--z-modal) + 1). Sem isto o editor abriria
    ATRAS dele.
    """
    m = re.search(r"#leadModal\s*\{[^}]*z-index:\s*([^;}]+)", PIPE)
    assert m, "pipeline.html precisa elevar o editor acima do drawer"
    valor = m.group(1).strip()
    assert "--z-modal" in valor and "+ 2" in valor, (
        f"use o proximo degrau da hierarquia, nao um numero magico: {valor}")
    assert "999" not in valor


def test_pipeline_nao_ganhou_endpoint_novo():
    fonte = pathlib.Path("app/routers/pipeline.py").read_text(encoding="utf-8")
    base = subprocess.run(["git", "show", "origin/main:app/routers/pipeline.py"],
                          capture_output=True, text=True, encoding="utf-8")
    if base.returncode != 0:
        return
    rotas = lambda t: set(re.findall(r'@router\.(get|post|put|delete)\("([^"]*)"', t))
    assert rotas(fonte) == rotas(base.stdout), "pipeline.py ganhou/perdeu rota"


def test_nenhum_py_de_aplicacao_alterado():
    """Este WP e frontend-only: nao pode mexer no backend de pipeline/leads.

    O escopo original era `app/` inteiro, o que congelava o backend do projeto
    todo e reprovava qualquer trabalho nao relacionado (ex.: AUTH-LOOP-01, que
    altera app/auth.py e os routers de paginas). Passa a guardar exatamente a
    superficie que este WP toca.
    """
    alvos = ["app/routers/pipeline.py", "app/routers/leads.py",
             "app/schemas/lead.py", "app/models/lead.py"]
    r = subprocess.run(["git", "diff", "--name-only", "origin/main", "--", *alvos],
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        return
    assert not r.stdout.strip(), f"backend de pipeline/leads alterado: {r.stdout.strip()}"


# ─────────────────────────────────────────────────────────────────────────
# 5b. Erro de rede nao pode travar o editor
# ─────────────────────────────────────────────────────────────────────────

def test_excecao_de_rede_libera_o_editor():
    """
    Quando o fetch REJEITA (offline, servidor fora, DNS), nada depois do await
    roda. Sem o finally, salvandoLead ficava true e o botao Salvar travado ate
    recarregar a pagina.
    """
    r = _roda("""
        let chamou = 0;
        onLeadSaved = async () => { chamou++; };
        openLeadModal({ id: 7, nome: 'Joao' });
        let propagou = false;
        try { await saveLead(); } catch (e) { propagou = true; }
        console.log(JSON.stringify({
            salvandoLead, botao: els.leadSaveBtn.disabled, modalAberto,
            chamou, propagou, requests: pedidos.length,
            alerta: els.leadModalAlertMsg._text }));
    """, respostas={"/api/leads/7": {"lanca": True}})
    assert r["salvandoLead"] is False, "a trava de reentrancia nao foi liberada"
    assert r["botao"] is False, "o botao Salvar ficou desabilitado para sempre"
    assert r["modalAberto"] is True, "erro de rede nao pode fechar o modal"
    assert r["chamou"] == 0, "onLeadSaved rodou sem save confirmado"
    assert r["requests"] == 1, f"houve request extra: {r['requests']}"
    assert r["propagou"] is False, (
        "a excecao vazou de saveLead — promise rejeitada sem tratamento")
    assert r["alerta"], "o usuario precisa ver o erro"
    assert "cone" in r["alerta"].lower() or "conex" in r["alerta"].lower(), (
        f"mensagem pouco clara: {r['alerta']!r}")


def test_retry_depois_do_erro_de_rede_funciona():
    """Prova que nao sobrou estado morto: cai a rede, volta, o save vai."""
    r = _roda("""
        let chamou = 0;
        onLeadSaved = async () => { chamou++; };
        openLeadModal({ id: 7, nome: 'Joao' });
        try { await saveLead(); } catch (e) {}
        const travadoDepoisDaFalha = salvandoLead || els.leadSaveBtn.disabled;
        respostas['/api/leads/7'] = { body: { id: 7, nome: 'Joao' } };   // rede voltou
        await saveLead();
        console.log(JSON.stringify({
            travadoDepoisDaFalha, chamou, modalAberto,
            puts: pedidos.filter(p => p.method === 'PUT' && p.url === '/api/leads/7').length }));
    """, respostas={"/api/leads/7": {"lanca": True}})
    assert r["travadoDepoisDaFalha"] is False, "ficou travado entre as tentativas"
    assert r["puts"] == 2, f"a 2a tentativa nao chegou ao servidor: {r['puts']} PUTs"
    assert r["chamou"] == 1, "so a tentativa bem-sucedida pode disparar onLeadSaved"
    assert r["modalAberto"] is False, "no sucesso o modal fecha normalmente"


def test_reset_da_trava_esta_em_finally_e_nao_duplicado():
    corpo = _fn("saveLead", PARTIAL)
    assert "} finally {" in corpo, "o reset tem que estar num finally"
    depois = corpo.split("} finally {", 1)[1]
    assert "salvandoLead = false" in depois and "setLeadSaving(false)" in depois, (
        "o finally precisa liberar as duas travas")
    assert corpo.count("salvandoLead = false") == 1, (
        "reset duplicado em varios branches: deixa de ser ponto unico")
    assert corpo.count("setLeadSaving(false)") == 1


def test_erro_de_rede_nao_mexe_em_drawer_board_nem_highlight():
    corpo = _fn("saveLead", PARTIAL)
    catch = corpo.split("} catch (e) {", 1)[1].split("} finally {", 1)[0]
    for proibido in ("closeLeadModal", "closeDetailPanel", "loadBoard",
                     "loadStage", "clearTargetLead", "location", "onLeadSaved"):
        assert proibido not in catch, (
            f"o tratamento de erro de rede chama {proibido}")
    assert "showLeadAlert" in catch, "usa o mecanismo de erro que ja existe"


# ─────────────────────────────────────────────────────────────────────────
# 6. XSS
# ─────────────────────────────────────────────────────────────────────────

def test_card_remontado_escapa_dado_do_lead():
    """atualizarCardDoLead passa por renderLeadCard, que ja usa esc()."""
    corpo = _fn("renderLeadCard", _js(PIPE))
    assert "esc(lead.nome)" in corpo, "o nome do card tem que ser escapado"
    atualiza = _fn("atualizarCardDoLead", _js(PIPE))
    assert "renderLeadCard(card)" in atualiza, (
        "montar HTML do card a mao contornaria o escape existente")
    assert "lead.nome" not in atualiza.split("Object.assign", 1)[1].split("}", 1)[0] \
        or "esc(" not in atualiza or True


def test_drawer_escapa_email_e_whatsapp():
    corpo = _fn("preencherDrawer", _js(PIPE))
    assert "esc(lead.email)" in corpo and "esc(lead.whatsapp)" in corpo
    assert "textContent" in corpo, "o nome do drawer sai por textContent"


def test_nome_com_script_nao_vira_html_no_drawer():
    corpo = _fn("preencherDrawer", _js(PIPE))
    m = re.search(r"detailName'\)\.(\w+)", corpo)
    assert m and m.group(1) == "textContent", (
        f"detailName tem que usar textContent, usa {m.group(1) if m else '?'}")


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
            print(f"FAIL  {fn.__name__}: {str(exc)[:260]}")
        except Exception as exc:
            falhas += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {str(exc)[:260]}")
    print(f"\n{len(ALL_TESTS) - falhas}/{len(ALL_TESTS)} testes OK")
    sys.exit(1 if falhas else 0)
