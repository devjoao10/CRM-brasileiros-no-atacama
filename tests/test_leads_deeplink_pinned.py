# -*- coding: utf-8 -*-
"""
LEADS-PIN-01 — o lead que vem do Conversas fica fixado acima da listagem.

Depois do PR #29, /leads?open=<id> ja abre qualquer lead por busca direta
(GET /api/leads/{id}), mesmo fora dos primeiros 50. O que faltava era
navegacao: fechado o modal, o lead sumia no meio de ~19 mil registros.

Agora ele tambem entra num bloco "Lead em destaque" acima da tabela, que
sobrevive a fechar o modal, abrir outro lead, filtrar, buscar e Carregar mais.
So sai por acao explicita.

BACKEND: zero alteracao. GET /api/leads/{id} ja devolve tudo que o bloco
mostra. Os testes de backend aqui existem para PROVAR isso, nao porque algo
mudou.

FRONTEND: os testes rodam o codigo extraido do template no node, com DOM
stubado — mesmo caminho dos PRs #27 e #29. Nao ha Playwright/Selenium/Cypress
no repo e este pacote nao justifica instalar um.

O stub de elemento registra SEPARADAMENTE o que foi escrito em textContent e
em innerHTML. E o que torna o teste de XSS funcional: a garantia nao e "o
codigo chama esc()", e sim "o dado do lead nunca chegou a innerHTML".

Rodar:  python tests/test_leads_deeplink_pinned.py
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

pathlib.Path("scratch").mkdir(exist_ok=True)
os.environ.update({
    "ENVIRONMENT": "development",
    "DATABASE_URL": "sqlite:///./scratch/leads_pinned.db",
    "SEED_INITIAL_ADMIN": "true",
    "ADMIN_INITIAL_EMAIL": "admin@local.test",
    "ADMIN_INITIAL_PASSWORD": "LocalSmoke123!",
    "GEMINI_API_KEY": "",
})

HTML = pathlib.Path("templates/leads.html").read_text(encoding="utf-8")
XSS = '<script>alert(1)</script> & "aspas" \'simples\''
_C = {}


# ─────────────────────────────────────────────────────────────────────────
# Backend: prova de que GET /api/leads/{id} ja basta
# ─────────────────────────────────────────────────────────────────────────

def _setup():
    if _C:
        return _C["client"], _C["d"]
    from fastapi.testclient import TestClient
    from app.main import app
    from app.database import SessionLocal, Base, engine
    from app.models.lead import Lead
    from app.models.user import User
    from datetime import date, datetime, timedelta, timezone

    f = pathlib.Path("scratch/leads_pinned.db")
    if f.exists():
        try:
            f.unlink()
        except PermissionError:
            pass
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        u = User(nome="Resp", email="resp@local.test", hashed_password="x")
        db.add(u)
        db.commit()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        db.bulk_insert_mappings(Lead, [{
            "nome": (XSS if i == 0 else f"Lead {i:03d}"),
            "email": f"lead{i}@x.test",
            "whatsapp": f"+5551{i:09d}",
            "destinos": ["Atacama"] if i % 2 else ["Santiago"],
            "campos_personalizados": {},
            "status_venda": "venda", "is_active": True,
            "responsavel_id": u.id if i % 3 == 0 else None,
            "data_chegada": date(2026, 6, 1),
            "created_at": base + timedelta(seconds=i),
        } for i in range(140)])
        db.commit()
        leads = db.query(Lead).order_by(Lead.id).all()
        d = {"primeiro": leads[0].id,          # o do payload XSS
             "fundo": leads[-1].id,
             "ids": [l.id for l in leads]}
    finally:
        db.close()
    client = TestClient(app)
    client.__enter__()
    r = client.post("/api/auth/login",
                    json={"email": "admin@local.test", "password": "LocalSmoke123!"})
    assert r.status_code == 200, r.text
    _C["client"], _C["d"] = client, d
    return client, d


def test_get_por_id_devolve_tudo_que_o_bloco_mostra():
    """Prova de que nao e preciso backend novo."""
    client, d = _setup()
    r = client.get(f"/api/leads/{d['fundo']}")
    assert r.status_code == 200, r.text
    lead = r.json()
    for campo in ("id", "nome", "whatsapp", "email", "destinos", "responsavel_nome"):
        assert campo in lead, f"o bloco precisa de {campo} e o GET nao devolve"


def test_lead_fora_da_primeira_pagina_vem_por_busca_direta():
    client, d = _setup()
    ids_da_pagina = {l["id"] for l in client.get("/api/leads?limit=50").json()["leads"]}
    fora = [i for i in d["ids"] if i not in ids_da_pagina]
    assert fora, "a semente precisa ter mais leads do que cabe na 1a pagina"
    r = client.get(f"/api/leads/{fora[0]}")
    assert r.status_code == 200 and r.json()["id"] == fora[0], (
        "lead fora da 1a pagina tem que vir por busca direta")


def test_id_inexistente_continua_404():
    client, d = _setup()
    assert client.get("/api/leads/99999999").status_code == 404


def test_nenhum_endpoint_novo_foi_criado():
    """O pacote e frontend: o router de leads nao pode ter rota nova."""
    def rotas(texto):
        return set(re.findall(r'@router\.(get|post|put|delete)\("([^"]*)"', texto))
    atual = rotas(pathlib.Path("app/routers/leads.py").read_text(encoding="utf-8"))
    base = subprocess.run(["git", "show", "origin/main:app/routers/leads.py"],
                          capture_output=True, text=True, encoding="utf-8")
    if base.returncode != 0:
        return   # sem origin/main disponivel (clone raso): nada a comparar
    antes = rotas(base.stdout)
    assert atual == antes, (
        f"o router de leads mudou e este pacote e frontend. "
        f"novas={atual - antes} removidas={antes - atual}")


# ─────────────────────────────────────────────────────────────────────────
# Harness JavaScript
# ─────────────────────────────────────────────────────────────────────────

def _js():
    return max(re.findall(r"<script>(.*?)</script>", HTML, re.S), key=len)


# O editor de Lead foi extraido para partials/_lead_edit_modal.html no PR #31
# e passou a ser compartilhado com o Pipeline. As funcoes do editor moram la;
# as da listagem continuam em leads.html.
PARTIAL = pathlib.Path("templates/partials/_lead_edit_modal.html").read_text(
    encoding="utf-8")


def _fn(nome):
    for fonte in (_js(), PARTIAL):
        m = re.search(r"(async\s+)?function " + re.escape(nome) + r"\(", fonte)
        if m:
            return fonte[m.start():].split("\n    }", 1)[0] + "\n    }"
    raise AssertionError(f"{nome} nao existe nem em leads.html nem no partial")


_HARNESS = """
'use strict';
let href = %s;
let respostas = %s;
let pedidos = [], modaisAbertos = [], modalFechado = 0, historico = [];

// Elemento que registra SEPARADAMENTE textContent e innerHTML: e o que
// permite provar que o dado do lead nunca virou HTML.
function novoEl() {
    return { _text: '', _html: '', style: {}, value: '', disabled: false, rows: [],
        set textContent(v) { this._text = String(v); },
        get textContent() { return this._text; },
        set innerHTML(v) { this._html = String(v); },
        get innerHTML() { return this._html; },
        insertAdjacentHTML(p, h) { this._html += h; },
        classList: { add(){}, remove(){}, contains(){ return false; } } };
}
const els = {};
const document = { getElementById: id => (els[id] = els[id] || novoEl()) };
const window = { location: { get href() { return href; } } };
const history = { replaceState: (a, b, u) => { href = String(u); historico.push(String(u)); } };
class AbortController { constructor(){ this.signal = { aborted: false }; } abort(){ this.signal.aborted = true; } }
const URLSearchParams = globalThis.URLSearchParams;

const Auth = { apiRequest: async (url, opts) => {
    pedidos.push(url);
    const r = respostas[url] !== undefined ? respostas[url] : respostas['*'];
    if (r === undefined) throw new Error('sem resposta stub para ' + url);
    if (r.status && r.status >= 400) return { ok: false, status: r.status, json: async () => r.body };
    return { ok: true, status: 200, json: async () => r.body };
}};

const PAGE_SIZE = 50;
let nextCursor = null, hasMore = false, carregando = false;
let leadsNaTela = [], reqAtual = null, debounceTimer = null, totalConhecido = 0;
let leadFixado = null;
function openLeadModal(lead) { modaisAbertos.push(lead && lead.id); }
function closeLeadModal() { modalFechado++; document.getElementById('leadModal'); }
function linhaDoLead(l) { return '<tr data-id="' + l.id + '"></tr>'; }
function renderFooter() {}
function renderTable(total, novos, primeira) {
    const tb = document.getElementById('leadsTableBody');
    const h = (novos || []).map(linhaDoLead).join('');
    if (primeira) tb.innerHTML = h; else tb.insertAdjacentHTML('beforeend', h);
}
function mostrarErro() {}

%s

(async () => {
%s
})();
"""

_FUNCOES = ("fixarLead", "renderLeadFixado", "abrirLeadFixado", "removerDestaque",
            "editLead", "atualizarLinha", "filtrosAtuais", "reloadLeads",
            "loadLeads", "loadMore")


def _roda(corpo, href="https://crm.test/leads?open=7", respostas=None):
    node = shutil.which("node")
    assert node, "node e necessario para exercitar o JS da pagina"
    script = _HARNESS % (
        json.dumps(href), json.dumps(respostas or {}),
        "\n".join(_fn(f) for f in _FUNCOES), corpo)
    p = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert p.returncode == 0, f"node falhou: {p.stderr.strip()[:500]}"
    return json.loads(p.stdout)


def _lead(lid=7, nome="Joao Silva", **extra):
    base = {"id": lid, "nome": nome, "whatsapp": "+5551999", "email": "j@x.test",
            "destinos": ["Santiago"], "responsavel_nome": "Resp", "tags": [],
            "funis": []}
    base.update(extra)
    return base


def _stub(lid=7, **kw):
    return {f"/api/leads/{lid}": {"body": _lead(lid, **kw)},
            "*": {"body": {"leads": [], "total": 0, "next_cursor": None,
                           "has_more": False}}}


# ─────────────────────────────────────────────────────────────────────────
# Deep-link fixa o lead
# ─────────────────────────────────────────────────────────────────────────

def test_deep_link_abre_modal_e_fixa_com_um_unico_get():
    r = _roda("""
        const lead = await editLead(7);
        if (lead) fixarLead(lead);
        console.log(JSON.stringify({
            pedidos, modaisAbertos, fixado: leadFixado && leadFixado.id,
            display: document.getElementById('leadFixado').style.display,
            nome: document.getElementById('leadFixadoNome').textContent,
        }));
    """, respostas=_stub())
    assert r["pedidos"] == ["/api/leads/7"], (
        f"o alvo tem que vir de UM GET direto por id: {r['pedidos']}")
    assert r["modaisAbertos"] == [7], "o modal do lead precisa abrir"
    assert r["fixado"] == 7 and r["display"] == "", "o bloco tem que aparecer"
    assert r["nome"] == "Joao Silva"


def test_deep_link_nao_varre_a_listagem():
    r = _roda("""
        const lead = await editLead(7);
        if (lead) fixarLead(lead);
        console.log(JSON.stringify({ pedidos }));
    """, respostas=_stub())
    listagens = [u for u in r["pedidos"] if u.startswith("/api/leads?")]
    assert not listagens, f"o deep-link paginou/scaneou a lista: {listagens}"


def test_bloco_mostra_contato_e_destino():
    r = _roda("""
        fixarLead(%s);
        console.log(JSON.stringify({
            detalhe: document.getElementById('leadFixadoDetalhe').textContent }));
    """ % json.dumps(_lead()), respostas=_stub())
    for esperado in ("+5551999", "j@x.test", "Resp", "Santiago"):
        assert esperado in r["detalhe"], f"{esperado} nao aparece: {r['detalhe']!r}"


# ─────────────────────────────────────────────────────────────────────────
# O destaque sobrevive
# ─────────────────────────────────────────────────────────────────────────

def test_fechar_modal_nao_limpa_o_destaque():
    """Mutation A protege exatamente isto."""
    r = _roda("""
        fixarLead(%s);
        closeLeadModal();
        console.log(JSON.stringify({
            fixado: leadFixado && leadFixado.id, modalFechado,
            display: document.getElementById('leadFixado').style.display }));
    """ % json.dumps(_lead()), respostas=_stub())
    assert r["modalFechado"] == 1, "o modal precisa ter fechado"
    assert r["fixado"] == 7 and r["display"] == "", (
        "fechar o modal apagou o destaque — e o motivo do recurso existir")


def test_abrir_outro_lead_nao_limpa_o_destaque():
    r = _roda("""
        fixarLead(%s);
        await editLead(9);                    // usuario clica noutra linha
        console.log(JSON.stringify({
            fixado: leadFixado && leadFixado.id, modaisAbertos }));
    """ % json.dumps(_lead()), respostas={
        "/api/leads/9": {"body": _lead(9, nome="Outro")},
        "*": {"body": {"leads": [], "total": 0, "has_more": False}}})
    assert r["modaisAbertos"] == [9], "o modal do outro lead abre normalmente"
    assert r["fixado"] == 7, "o destaque veio do Conversas: navegar nao o remove"


def test_carregar_mais_nao_toca_no_destaque():
    r = _roda("""
        fixarLead(%s);
        await loadLeads();                    // 1a pagina
        await loadLeads();                    // Carregar mais
        console.log(JSON.stringify({
            fixado: leadFixado && leadFixado.id,
            display: document.getElementById('leadFixado').style.display,
            nomeNoBloco: document.getElementById('leadFixadoNome').textContent,
            htmlDaTabela: document.getElementById('leadsTableBody').innerHTML }));
    """ % json.dumps(_lead()), respostas={"*": {"body": {
        "leads": [{"id": 1}], "total": 2, "next_cursor": "C2", "has_more": True}}})
    assert r["fixado"] == 7 and r["display"] == ""
    assert r["nomeNoBloco"] == "Joao Silva"
    assert "leadFixado" not in r["htmlDaTabela"], (
        "o bloco nao pode viver dentro do container que recebe as paginas")


def test_troca_de_filtro_nao_limpa_o_destaque():
    """Mutation C protege isto."""
    r = _roda("""
        fixarLead(%s);
        await reloadLeads();                  // como se um filtro tivesse mudado
        console.log(JSON.stringify({
            fixado: leadFixado && leadFixado.id,
            display: document.getElementById('leadFixado').style.display }));
    """ % json.dumps(_lead()), respostas={"*": {"body": {
        "leads": [], "total": 0, "next_cursor": None, "has_more": False}}})
    assert r["fixado"] == 7 and r["display"] == "", (
        "filtrar nao pode remover a referencia que veio do Conversas")


def test_busca_nao_limpa_o_destaque():
    r = _roda("""
        fixarLead(%s);
        document.getElementById('filterSearch').value = 'Maria';
        await reloadLeads();
        console.log(JSON.stringify({
            fixado: leadFixado && leadFixado.id,
            urlBuscada: pedidos[pedidos.length - 1] }));
    """ % json.dumps(_lead()), respostas={"*": {"body": {
        "leads": [], "total": 0, "next_cursor": None, "has_more": False}}})
    assert r["fixado"] == 7, "buscar outro nome nao remove o destaque"
    assert "search=Maria" in r["urlBuscada"], "a busca precisa ter acontecido"


# ─────────────────────────────────────────────────────────────────────────
# Remover destaque
# ─────────────────────────────────────────────────────────────────────────

def test_remover_destaque_some_com_bloco_e_tira_open_da_url():
    r = _roda("""
        fixarLead(%s);
        removerDestaque();
        console.log(JSON.stringify({
            fixado: leadFixado,
            display: document.getElementById('leadFixado').style.display,
            href, historico }));
    """ % json.dumps(_lead()), href="https://crm.test/leads?open=7",
        respostas=_stub())
    assert r["fixado"] is None and r["display"] == "none"
    assert "open=" not in r["href"], f"open sobreviveu na URL: {r['href']}"
    assert r["historico"], "a URL tem que ser reescrita por replaceState"


def test_remover_destaque_preserva_outros_params_e_hash():
    """Mutation F protege isto."""
    r = _roda("""
        fixarLead(%s);
        removerDestaque();
        console.log(JSON.stringify({ href }));
    """ % json.dumps(_lead()),
        href="https://crm.test/leads?foo=bar&open=7&baz=1#ancora",
        respostas=_stub())
    assert r["href"] == "https://crm.test/leads?foo=bar&baz=1#ancora", (
        f"perdeu params ou fragmento: {r['href']}")


def test_remover_destaque_nao_recarrega_nem_fecha_modal():
    r = _roda("""
        fixarLead(%s);
        removerDestaque();
        console.log(JSON.stringify({ pedidos, modalFechado }));
    """ % json.dumps(_lead()), respostas=_stub())
    assert r["pedidos"] == [], f"remover destaque nao pode chamar a API: {r['pedidos']}"
    assert r["modalFechado"] == 0, "nao pode fechar o modal automaticamente"


def test_remover_duas_vezes_e_inofensivo():
    r = _roda("""
        fixarLead(%s);
        removerDestaque();
        const depois = href;
        removerDestaque();
        console.log(JSON.stringify({ igual: depois === href, historico }));
    """ % json.dumps(_lead()), respostas=_stub())
    assert r["igual"] and len(r["historico"]) == 1


def test_sem_open_na_url_nada_e_fixado():
    """F5 depois de limpar: o bloco nao volta."""
    js = _js()
    bloco = js.split("const openId", 1)[1].split("});", 1)[0]
    assert "if (openId)" in bloco, "sem open na URL nao pode fixar nada"
    assert "fixarLead" in bloco, "com open na URL tem que fixar"
    r = _roda("""
        const openId = new URLSearchParams(new URL(href).search).get('open');
        if (openId) { const l = await editLead(parseInt(openId)); if (l) fixarLead(l); }
        console.log(JSON.stringify({ fixado: leadFixado, pedidos }));
    """, href="https://crm.test/leads?foo=bar", respostas=_stub())
    assert r["fixado"] is None and r["pedidos"] == [], (
        "sem open a pagina nao pode nem buscar nem fixar")


def test_um_unico_lead_fixado_por_vez():
    r = _roda("""
        fixarLead(%s);
        fixarLead(%s);
        console.log(JSON.stringify({
            fixado: leadFixado && leadFixado.id,
            nome: document.getElementById('leadFixadoNome').textContent }));
    """ % (json.dumps(_lead(7)), json.dumps(_lead(9, nome="Segundo"))),
        respostas=_stub())
    assert r["fixado"] == 9 and r["nome"] == "Segundo", (
        "o estado representa so o open atual, nao uma lista")


# ─────────────────────────────────────────────────────────────────────────
# Save
# ─────────────────────────────────────────────────────────────────────────

def test_save_do_lead_fixado_atualiza_o_bloco():
    """Mutation E protege isto."""
    r = _roda("""
        fixarLead(%s);
        await atualizarLinha(7);
        console.log(JSON.stringify({
            nome: document.getElementById('leadFixadoNome').textContent,
            pedidos }));
    """ % json.dumps(_lead()), respostas={
        "/api/leads/7": {"body": _lead(7, nome="Joao Pedro")},
        "*": {"body": {"leads": [], "total": 0, "has_more": False}}})
    assert r["nome"] == "Joao Pedro", (
        f"o bloco ficou com dado velho: {r['nome']!r}")
    assert r["pedidos"] == ["/api/leads/7"], "um GET so"


def test_save_do_fixado_que_tambem_esta_na_lista_atualiza_os_dois():
    r = _roda("""
        fixarLead(%s);
        leadsNaTela = [{ id: 7, nome: 'Joao Silva' }];
        document.getElementById('leadsTableBody').rows = [ { outerHTML: '' } ];
        const ok = await atualizarLinha(7);
        console.log(JSON.stringify({
            ok,
            nomeBloco: document.getElementById('leadFixadoNome').textContent,
            nomeLista: leadsNaTela[0].nome,
            pedidos }));
    """ % json.dumps(_lead()), respostas={
        "/api/leads/7": {"body": _lead(7, nome="Joao Pedro")},
        "*": {"body": {"leads": [], "total": 0, "has_more": False}}})
    assert r["nomeBloco"] == "Joao Pedro", "bloco desatualizado"
    assert r["nomeLista"] == "Joao Pedro", "linha da lista desatualizada"
    assert r["ok"] is True
    assert r["pedidos"] == ["/api/leads/7"], (
        f"bloco e linha tem que sair do MESMO GET: {r['pedidos']}")


def test_save_de_outro_lead_nao_mexe_no_fixado():
    r = _roda("""
        fixarLead(%s);
        leadsNaTela = [{ id: 9, nome: 'Outro' }];
        document.getElementById('leadsTableBody').rows = [ { outerHTML: '' } ];
        await atualizarLinha(9);
        console.log(JSON.stringify({
            fixado: leadFixado && leadFixado.id,
            nomeBloco: document.getElementById('leadFixadoNome').textContent }));
    """ % json.dumps(_lead()), respostas={
        "/api/leads/9": {"body": _lead(9, nome="Outro Alterado")},
        "*": {"body": {"leads": [], "total": 0, "has_more": False}}})
    assert r["fixado"] == 7 and r["nomeBloco"] == "Joao Silva", (
        "salvar outro lead nao pode mexer no bloco")


# ─────────────────────────────────────────────────────────────────────────
# Erros nao podem criar bloco fantasma
# ─────────────────────────────────────────────────────────────────────────

def test_404_nao_fixa_nem_abre_modal():
    r = _roda("""
        const lead = await editLead(999);
        if (lead) fixarLead(lead);
        console.log(JSON.stringify({
            retorno: lead, fixado: leadFixado, modaisAbertos,
            display: document.getElementById('leadFixado').style.display || 'intocado' }));
    """, href="https://crm.test/leads?open=999", respostas={
        "/api/leads/999": {"status": 404, "body": {"detail": "Lead não encontrado"}}})
    assert r["retorno"] is None and r["fixado"] is None
    assert r["modaisAbertos"] == [], "404 nao pode abrir modal com lixo"
    assert r["display"] != "", (
        f"404 nao pode deixar bloco fantasma (display={r['display']!r}); "
        f"'intocado' significa que renderLeadFixado nem rodou")


def test_500_nao_fixa():
    r = _roda("""
        const lead = await editLead(7);
        if (lead) fixarLead(lead);
        console.log(JSON.stringify({ fixado: leadFixado, retorno: lead }));
    """, respostas={"/api/leads/7": {"status": 500, "body": {"detail": "boom"}}})
    assert r["fixado"] is None, "erro de servidor nao pode virar bloco parcial"


# ─────────────────────────────────────────────────────────────────────────
# XSS — propriedade, nao busca de string
# ─────────────────────────────────────────────────────────────────────────

def test_dados_do_lead_nunca_chegam_a_innerhtml():
    """
    O stub registra textContent e innerHTML separadamente. Se o payload
    aparecer em innerHTML, o navegador o executaria; em textContent, nao.
    """
    r = _roda("""
        fixarLead(%s);
        const nome = document.getElementById('leadFixadoNome');
        const det = document.getElementById('leadFixadoDetalhe');
        console.log(JSON.stringify({
            textoNome: nome._text, htmlNome: nome._html,
            textoDet: det._text, htmlDet: det._html }));
    """ % json.dumps(_lead(nome=XSS, email=XSS)), respostas=_stub())
    assert r["textoNome"] == XSS, "o nome tem que aparecer como TEXTO literal"
    assert r["htmlNome"] == "", (
        f"o nome foi parar em innerHTML: {r['htmlNome']!r} — isso e XSS")
    assert r["htmlDet"] == "", f"o detalhe foi para innerHTML: {r['htmlDet']!r}"
    assert XSS in r["textoDet"], "o email tambem sai como texto"


def test_bloco_nao_monta_html_com_dado_do_lead():
    corpo = _fn("renderLeadFixado")
    assert "innerHTML" not in corpo, (
        "renderLeadFixado nao pode montar HTML: use textContent")
    assert corpo.count("textContent") >= 2, "os dois campos saem por textContent"


def test_nome_com_caracteres_especiais_nao_quebra_o_bloco():
    for payload in ("<b>x</b>", "a & b", 'aspas "duplas"', "apostrofo ' ", "< > & \" '"):
        r = _roda("""
            fixarLead(%s);
            const el = document.getElementById('leadFixadoNome');
            console.log(JSON.stringify({ texto: el._text, html: el._html }));
        """ % json.dumps(_lead(nome=payload)), respostas=_stub())
        assert r["texto"] == payload, f"{payload!r} saiu alterado: {r['texto']!r}"
        assert r["html"] == "", f"{payload!r} virou HTML"


# ─────────────────────────────────────────────────────────────────────────
# Estrutura
# ─────────────────────────────────────────────────────────────────────────

def test_bloco_fica_fora_do_container_da_listagem():
    antes = HTML.split('<div class="leads-table-wrapper"', 1)[0]
    assert 'id="leadFixado"' in antes, (
        "o bloco tem que estar ANTES da leads-table-wrapper — dentro dela o "
        "insertAdjacentHTML das paginas novas o afetaria")
    corpo_tabela = HTML.split('id="leadsTableBody"', 1)[1].split("</table>", 1)[0]
    assert "leadFixado" not in corpo_tabela


def test_fechar_modal_nao_menciona_o_destaque():
    corpo = _fn("closeLeadModal")
    for proibido in ("leadFixado", "removerDestaque", "fixarLead"):
        assert proibido not in corpo, (
            f"closeLeadModal nao pode tocar no destaque (achou {proibido!r})")


def test_reset_de_filtros_nao_menciona_o_destaque():
    for fn in ("reloadLeads", "clearFilters", "loadMore"):
        corpo = _fn(fn)
        assert "leadFixado" not in corpo and "removerDestaque" not in corpo, (
            f"{fn} nao pode mexer no destaque")


def test_saida_explicita_existe_na_interface():
    m = re.search(r'<button[^>]*onclick="removerDestaque\(\)"[^>]*>([^<]*)</button>',
                  HTML)
    assert m, "botao de remover destaque nao encontrado"
    assert "destaque" in m.group(1).lower(), f"rotulo pouco claro: {m.group(1)!r}"
    assert 'id="leadFixado"' in HTML and "display:none" in HTML.split(
        'id="leadFixado"', 1)[1][:120], "sem lead, o bloco comeca escondido"


def test_abrir_pelo_bloco_usa_o_mesmo_modal():
    corpo = _fn("abrirLeadFixado")
    assert "editLead(" in corpo, "o bloco tem que reutilizar editLead"
    assert "openLeadModal" not in corpo, "nao pode abrir um segundo modal"


def test_estado_tem_fonte_unica():
    js = _js()
    assert js.count("let leadFixado") == 1, "uma unica declaracao do estado"
    assert "pinnedLeadId" not in js and "leadFixadoId" not in js, (
        "id paralelo ao objeto seria uma segunda fonte de verdade")


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
            print(f"FAIL  {fn.__name__}: {str(exc)[:280]}")
        except Exception as exc:
            falhas += 1
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {str(exc)[:280]}")
    print(f"\n{len(ALL_TESTS) - falhas}/{len(ALL_TESTS)} testes OK")
    sys.exit(1 if falhas else 0)
