# -*- coding: utf-8 -*-
"""
PIPE-HL-02 — saida do destaque do card alvo do deep-link.

O PR #26 fez o card de `?lead_id=` ficar destacado. Sobrou o problema de UX:
o destaque era permanente. Nada removia a classe (so `setTargetLead` de um
alvo NOVO), o `lead_id` ficava na URL para sempre e, no F5, `aplicarDeepLink`
reaplicava tudo. O usuario ficava preso no highlight.

Este arquivo cobre a saida: `clearTargetLead()` e os gatilhos que a disparam.

DUAS TECNICAS, cada uma onde e honesta:

1. EXECUCAO REAL (node): a logica de estado + URL de `clearTargetLead` e
   `setTargetLead` e extraida do proprio template e RODADA. O `URL` e o do
   Node, de verdade — entao "os outros query params permanecem" e uma
   propriedade verificada, nao a grafia do codigo. Os stubs (`classList`,
   `history`) sao fieis porque so registram a chamada.

2. INSPECAO ESTATICA: os gatilhos (clique em card/funil/area vazia) dependem
   de DOM, evento e bubbling reais. Stubar `closest()` seria escrever a
   resposta no proprio teste, entao aqui se verifica a FIACAO — quem chama
   quem, e com qual guarda. E o que da para garantir sem browser.

LIMITACAO ASSUMIDA: sem Playwright/Selenium/Cypress no repo, e este hotfix
nao justifica instalar um. Nao ha teste de clique real nem de pixel.

O `node` do bloco 1 nao e dependencia nova: o runner ubuntu-latest precisa
dele para executar actions/checkout (action node20). Se sumisse, o workflow
inteiro nao rodaria — por isso o teste exige em vez de pular em silencio.

Rodar:  python tests/test_pipeline_target_clear.py
   ou:  python -m pytest tests/test_pipeline_target_clear.py
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

HTML = pathlib.Path("templates/pipeline.html").read_text(encoding="utf-8")
CLASSE = "pipeline-target-card"


def _sem_comentarios(src):
    """
    Tira `// comentario` preservando `http://`.

    Sem isto, comentar a chamada — `// clearTargetLead();` — deixaria o teste
    passar: a string continua no arquivo. Foi exatamente assim que a mutacao
    "trocar de funil deixa de limpar" sobreviveu na primeira rodada.
    """
    return re.sub(r"(?<!:)//.*", "", src)


def _corpo(nome):
    """Codigo real da funcao, extraido do template: o teste exercita producao."""
    marca = "function " + nome + "("
    assert marca in HTML, f"funcao {nome} nao existe mais em pipeline.html"
    corpo = HTML.split(marca, 1)[1].split("\n    }", 1)[0]
    return _sem_comentarios(marca + corpo + "\n    }")


# ─────────────────────────────────────────────────────────────────────────
# 1. EXECUCAO REAL — estado + URL
# ─────────────────────────────────────────────────────────────────────────

_HARNESS = """
'use strict';
let removidas = [];
const cards = [{ classList: { remove: c => removidas.push(c) } }];
const btn = { style: { display: '' } };
let href = %s;

const document = {
    querySelectorAll: sel => (sel === '.%s' ? cards : []),
    getElementById: id => (id === 'clearTargetBtn' ? btn : null),
};
const window = { location: { get href() { return href; } } };
const history = { replaceState: (s, t, url) => { href = String(url); } };
let highlightLeadId = 42;

%s

%s

clearTargetLead();
const apos = { href, removidas, alvo: highlightLeadId, btn: btn.style.display };
clearTargetLead();          // 2a chamada: a guarda tem que segurar
console.log(JSON.stringify(Object.assign(apos, { hrefFinal: href })));
"""


def _roda(url_inicial):
    node = shutil.which("node")
    assert node, "node e necessario para exercitar clearTargetLead de verdade"
    script = _HARNESS % (json.dumps(url_inicial), CLASSE,
                         _corpo("setTargetLead"), _corpo("clearTargetLead"))
    p = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert p.returncode == 0, f"node falhou: {p.stderr.strip()}"
    return json.loads(p.stdout)


def test_limpar_remove_a_classe_do_card():
    r = _roda("https://crm.test/pipeline?lead_id=42")
    assert r["removidas"] == [CLASSE], (
        f"clearTargetLead precisa tirar a classe do card: {r['removidas']}"
    )


def test_limpar_zera_o_estado_js():
    r = _roda("https://crm.test/pipeline?lead_id=42")
    assert r["alvo"] is None, (
        "com highlightLeadId setado, o proximo renderStage repinta o card: "
        f"o estado precisa zerar, veio {r['alvo']!r}"
    )


def test_limpar_remove_lead_id_da_url():
    r = _roda("https://crm.test/pipeline?lead_id=42")
    assert "lead_id" not in r["href"], f"lead_id sobreviveu na URL: {r['href']}"
    assert r["href"] == "https://crm.test/pipeline", (
        f"sem outros params a URL fica limpa: {r['href']}"
    )


def test_outros_query_params_permanecem():
    """A propriedade central: so o lead_id sai. Rodado de verdade, nao lido."""
    r = _roda("https://crm.test/pipeline?funil=3&lead_id=42&aba=kanban#topo")
    assert r["href"] == "https://crm.test/pipeline?funil=3&aba=kanban#topo", (
        f"os outros params (e o fragmento) tem que sobreviver: {r['href']}"
    )


def test_refresh_apos_limpar_nao_ressuscita_o_alvo():
    """
    Composicao: a URL limpa (acima) + aplicarDeepLink saindo cedo sem lead_id.
    Sem as duas metades o F5 traria o destaque de volta.
    """
    r = _roda("https://crm.test/pipeline?lead_id=42")
    assert "lead_id" not in r["href"]
    deep = _corpo("aplicarDeepLink")
    assert re.search(r"get\('lead_id'\)", deep), "o deep-link le lead_id da URL"
    assert re.search(r"if \(!urlLeadId\) return;", deep), (
        "sem lead_id na URL o deep-link precisa sair cedo — e o que faz o F5 "
        "nao reaplicar o destaque que o usuario dispensou"
    )


def test_limpar_duas_vezes_e_inofensivo():
    r = _roda("https://crm.test/pipeline?funil=3")
    assert r["hrefFinal"] == r["href"], "a 2a chamada nao pode remexer na URL"


def test_botao_de_saida_some_junto_com_o_destaque():
    r = _roda("https://crm.test/pipeline?lead_id=42")
    assert r["btn"] == "none", (
        f"sem alvo o botao 'Limpar destaque' nao faz sentido: display={r['btn']!r}"
    )


# ─────────────────────────────────────────────────────────────────────────
# 2. FIACAO DOS GATILHOS
# ─────────────────────────────────────────────────────────────────────────

def test_clique_em_outro_card_limpa():
    corpo = _corpo("openDetailPanel")
    assert "clearTargetLead()" in corpo, (
        "todo clique em card passa por openDetailPanel: e o ponto unico onde "
        "'abriu outro lead' pode ser detectado"
    )
    assert re.search(r"leadId !== highlightLeadId", corpo), (
        "a limpeza precisa da guarda: reabrir o PROPRIO alvo nao pode limpar"
    )


def test_reabrir_o_proprio_alvo_nao_limpa():
    """
    aplicarDeepLink chama openDetailPanel(alvo) logo apos setTargetLead.
    Sem guarda, o destaque se apagaria no mesmo instante em que nasceu.
    """
    corpo = _corpo("openDetailPanel")
    linha = [l for l in corpo.splitlines() if "clearTargetLead()" in l]
    assert linha and "if" in linha[0], (
        f"clearTargetLead incondicional em openDetailPanel: {linha}"
    )
    deep = _corpo("aplicarDeepLink")
    assert "setTargetLead(lid)" in deep and "openDetailPanel(" in deep, (
        "o cenario que a guarda protege precisa continuar existindo"
    )


def test_trocar_de_funil_limpa():
    corpo = _corpo("switchFunnel")
    assert "clearTargetLead()" in corpo, (
        "o alvo pertence a um funil; trocar de aba deixa o destaque orfao"
    )


def test_clique_em_area_vazia_do_board_limpa():
    lis = _listener_do_board()
    assert "clearTargetLead()" in lis, "area vazia do board precisa limpar"


def test_carregar_mais_e_filtros_da_coluna_nao_limpam():
    """
    'Carregar mais' e os filtros por etapa vivem DENTRO do #kanbanBoard.
    Sem estes nomes na guarda, usar a coluna apagaria o destaque.
    """
    partes = _selector_da_guarda()
    for exigido in ("button", "input", "select"):
        assert exigido in partes, (
            f"'{exigido}' fora da guarda: {exigido} dentro do board limparia "
            f"o destaque sem o usuario pedir. Guarda atual: {sorted(partes)}"
        )


def test_clique_no_card_nao_conta_como_area_vazia():
    partes = _selector_da_guarda()
    assert ".lead-card" in partes, (
        "sem .lead-card na guarda, clicar num card dispararia limpeza pelo "
        "board alem do openDetailPanel — inclusive numa tag dentro do card"
    )


def test_o_listener_e_do_board_e_nao_do_documento():
    """Clique dentro do drawer nao pode limpar."""
    assert re.search(r"getElementById\('kanbanBoard'\)\.addEventListener\('click'",
                     HTML), (
        "o listener tem que ser do #kanbanBoard: em document ele pegaria "
        "cliques do drawer, do menu e da barra de filtros"
    )
    painel = HTML.split('id="detailPanel"', 1)[1].split("</script>", 1)[0]
    assert "clearTargetLead" not in painel.split('id="kanbanBoard"')[0][:4000], (
        "nada dentro do drawer pode chamar clearTargetLead"
    )


def test_fechar_o_drawer_continua_nao_limpando():
    """O motivo do recurso: achar o card DEPOIS de fechar as informacoes."""
    corpo = _corpo("closeDetailPanel")
    for proibido in ("clearTargetLead", "highlightLeadId", "setTargetLead", CLASSE):
        assert proibido not in corpo, (
            f"closeDetailPanel nao pode mexer no target (achou {proibido!r})"
        )


def test_filtros_globais_nao_limpam_sozinhos():
    for fn in ("applyFilters", "clearPipeFilters"):
        assert "clearTargetLead" not in _corpo(fn), (
            f"{fn} nao pode limpar o destaque: filtrar nao e dispensar o alvo"
        )


def test_limpar_nao_recarrega_o_board_nem_fecha_o_drawer():
    corpo = _corpo("clearTargetLead")
    for proibido in ("loadBoard", "loadStage", "closeDetailPanel", "applyFilters",
                     "currentFunnelId"):
        assert proibido not in corpo, (
            f"clearTargetLead so desfaz o destaque; achou {proibido!r}"
        )
    assert "location.reload" not in corpo and "location.href =" not in corpo, (
        "a URL sai por replaceState, sem navegar"
    )
    assert "replaceState" in corpo, "sem replaceState o F5 traz o destaque de volta"


def test_no_maximo_um_destaque():
    corpo = _corpo("setTargetLead")
    assert f"querySelectorAll('.{CLASSE}')" in corpo and "classList.remove" in corpo, (
        "setar um alvo novo precisa varrer o DOM: a coluna antiga pode nao "
        "re-renderizar e sobrariam dois cards destacados"
    )


def test_deep_link_ainda_aplica_o_destaque():
    """Guarda de nao-regressao do PR #26: a entrada nao pode quebrar na saida."""
    assert f"classList.add('{CLASSE}')" in HTML, "o card alvo perdeu a marcacao"
    assert f".lead-card.{CLASSE}" in HTML, "a regra CSS do destaque sumiu"
    assert "setTargetLead(lid)" in _corpo("aplicarDeepLink")


def test_saida_explicita_e_discreta_e_reaproveita_o_estilo_existente():
    m = re.search(r'<button[^>]*id="clearTargetBtn"[^>]*>([^<]*)</button>', HTML, re.S)
    assert m, "botao de saida explicita nao encontrado"
    tag = m.group(0)
    assert 'class="filter-clear"' in tag, (
        "reutilizar o estilo do '✕ Limpar' ja existente, sem CSS novo"
    )
    assert "display:none" in tag, "sem alvo, o botao nao aparece"
    assert "clearTargetLead()" in tag
    assert "destaque" in m.group(1).lower(), f"rotulo pouco claro: {m.group(1)!r}"
    assert f".{CLASSE}" not in tag, "o botao nao e o card"
    # so o setTargetLead controla a visibilidade: fonte unica de verdade
    assert "clearTargetBtn" in _corpo("setTargetLead")


# ── helpers dos gatilhos ─────────────────────────────────────────────────

def _listener_do_board():
    m = re.search(
        r"getElementById\('kanbanBoard'\)\.addEventListener\('click',(.*?)\n\s*\}\);",
        HTML, re.S)
    assert m, "listener de clique do #kanbanBoard nao encontrado"
    return _sem_comentarios(m.group(1))


def _selector_da_guarda():
    m = re.search(r"closest\('([^']+)'\)", _listener_do_board())
    assert m, "o listener precisa de closest() para separar card de area vazia"
    return {p.strip() for p in m.group(1).split(",")}


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
