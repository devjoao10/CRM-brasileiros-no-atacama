"""
AUDIT-2026-08-WG + SEG-UI-01 — regressao de tres bugs da tela de Segmentacao.

BUG 1: viewSegment() adicionava .show/.open ao overlay/painel ANTES dos dois
fetches resolverem. Em qualquer falha (401 -> Auth.apiRequest devolve r nulo
e redireciona para /login; ou resposta com r.ok falso) o overlay borrado
(backdrop-filter: blur) ficava aberto e travado — so um clique manual no
botao X fechava. erroNoDetalhe() so reescrevia #detailBody, atras do
backdrop. Prova que o branch de erro fecha o painel (nao so closeDetail(),
chamado pelo X, que sempre teve a remocao de classe).

BUG 2: 14 dos filtros do formulario chamavam previewCount() direto no
onchange, sem o debounce de 300ms que ja protegia os campos de texto
(debouncePreview()). previewCount() tambem nao tinha sequenciamento de
requisicao: uma resposta lenta de um filtro antigo podia chegar DEPOIS de
uma resposta mais nova e sobrescrever a contagem exibida, que passava a nao
bater com os filtros selecionados. Prova que (a) nenhum onchange chama
previewCount() direto e (b) existe uma guarda de sequencia em
previewCount() que descarta respostas obsoletas.

BUG 3 (SEG-UI-01): mesmo no caminho de SUCESSO de viewSegment() o painel de
leads nunca aparecia — desde 9afa7ea o .detail-panel declarava
z-index:calc(var(--z-modal)+1). Em calc() o "+" PRECISA de espaco dos dois
lados; sem ele a declaracao e invalida, o z-index computado vira auto e o
painel abre ATRAS do proprio overlay borrado (z-index var(--z-modal)): a tela
inteira fica desfocada e o primeiro clique cai no overlay, que fecha tudo.
Prova que o z-index do painel e valido e fica acima do overlay.

So faz assercoes estaticas sobre o source do template (e o --z-modal de
static/css/variables.css) — nao sobe o app, nao toca rede/banco.

Roda standalone:  python tests/test_segmentacao_ui_fix.py
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "segmentacao.html"
VARIABLES_CSS = ROOT / "static" / "css" / "variables.css"

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


html = TEMPLATE.read_text(encoding="utf-8")


def slice_fn(start_marker, end_marker):
    check(start_marker in html, f"marcador encontrado: {start_marker!r}")
    check(end_marker in html, f"marcador encontrado: {end_marker!r}")
    return html.split(start_marker, 1)[1].split(end_marker, 1)[0]


# ============ BUG 1 — falha em viewSegment() fecha o painel ============
print("Segmentacao — BUG 1: falha fecha o painel (nao fica borrado/travado)")

viewsegment_body = slice_fn(
    "async function viewSegment(id) {", "\nfunction erroNoDetalhe(r) {"
)
check("erroNoDetalhe(sr)" in viewsegment_body and "erroNoDetalhe(r)" in viewsegment_body,
      "as duas falhas de viewSegment (metadado e leads) chamam erroNoDetalhe()")
# caminho de sucesso continua exatamente como era
check("classList.add('show')" in viewsegment_body and "classList.add('open')" in viewsegment_body,
      "abertura otimista do painel preservada (caminho de sucesso intacto)")
check("renderDetailTable(d.leads || [])" in viewsegment_body,
      "caminho de sucesso ainda renderiza a tabela de leads")

erro_body = slice_fn("function erroNoDetalhe(r) {", "\nfunction renderDetailTable(leads) {")
# O ponto central do BUG 1: o proprio branch de erro precisa disparar o
# fechamento — nao basta closeDetail() existir em outro lugar (o X) sem ser
# chamado daqui.
check("closeDetail()" in erro_body,
      "erroNoDetalhe() (o branch de falha) chama closeDetail() — a classe eh "
      "removida DENTRO do caminho de erro, nao so em closeDetail() isolado")
check("alert(" in erro_body,
      "erro real (r nao nulo) e avisado ao usuario, ja que #detailBody fica invisivel com o painel fechado")

closedetail_body = slice_fn("function closeDetail() {", "\nasync function exportLeads() {")
check("classList.remove('show')" in closedetail_body,
      "closeDetail() remove .show do overlay (prova que chamar closeDetail() realmente fecha)")
check("classList.remove('open')" in closedetail_body,
      "closeDetail() remove .open do painel (prova que chamar closeDetail() realmente fecha)")


# ============ BUG 2 — toda mudanca de filtro passa pelo debounce ============
print("\nSegmentacao — BUG 2: filtros roteados pelo debounce + sequencia de requisicao")

direct_calls = html.count('onchange="previewCount()"')
check(direct_calls == 0,
      f"nenhum onchange chama previewCount() direto (corrida entre 14 handlers) — achou {direct_calls}")

debounced_calls = html.count('onchange="debouncePreview()"')
check(debounced_calls >= 14,
      f"pelo menos 14 onchange roteados para o mesmo ponto de entrada debouncePreview() (achou {debounced_calls})")

check("function debouncePreview()" in html and "setTimeout(previewCount, 300)" in html,
      "debouncePreview() continua sendo o unico idioma de debounce (300ms) do arquivo")

preview_body = slice_fn("async function previewCount() {", "\nasync function viewSegment(id) {")
check(preview_body.count("++") >= 1,
      "previewCount() incrementa um contador de sequencia de requisicao")
check(preview_body.count("!==") >= 2,
      "previewCount() descarta resposta obsoleta em pelo menos 2 pontos (apos o fetch e apos o json) — "
      "mesma guarda de sequencia do listRequestSeq em conversas/static/js/conversas.js")
check("return" in preview_body,
      "a guarda de sequencia efetivamente interrompe o fluxo (return) quando a resposta esta obsoleta")


# ============ BUG 3 — painel de leads acima do overlay borrado ============
print("\nSegmentacao — BUG 3: painel de leads acima do overlay borrado (SEG-UI-01)")

m_z = re.search(r"--z-modal:\s*(\d+)\s*;", VARIABLES_CSS.read_text(encoding="utf-8"))
check(m_z is not None, "--z-modal e um inteiro literal em static/css/variables.css")
z_modal = int(m_z.group(1)) if m_z else None


def z_index_computado(seletor):
    """z-index da regra `seletor{...}` como o navegador computa, ou None quando
    a declaracao e invalida (o navegador cai para auto). Resolve so inteiro,
    var(--z-modal) e calc(A + B)/calc(A - B); qualquer outra forma devolve None
    e o check falha fechado."""
    regra = re.search(r"(?m)^\s*" + re.escape(seletor) + r"\s*\{([^}]*)\}", html)
    check(regra is not None, f"regra CSS encontrada: {seletor}")
    decl = re.search(r"z-index:([^;]+)", regra.group(1)) if regra else None
    if not decl:
        return None
    valor = decl.group(1).strip().replace("var(--z-modal)", str(z_modal))
    if valor.isdigit():
        return int(valor)
    # Em calc() o "+" e o "-" exigem espaco dos dois lados: calc(300+1) e invalido.
    conta = re.fullmatch(r"calc\(\s*(\d+)\s+([+-])\s+(\d+)\s*\)", valor)
    if not conta:
        return None
    a, op, b = int(conta.group(1)), conta.group(2), int(conta.group(3))
    return a + b if op == "+" else a - b


z_overlay = z_index_computado(".detail-panel-overlay")
z_painel = z_index_computado(".detail-panel")
check(z_painel is not None,
      "z-index do .detail-panel e valido (se invalido, o navegador cai para auto "
      "e o painel abre ATRAS do overlay borrado)")
check(z_painel is not None and z_overlay is not None and z_painel > z_overlay,
      f"painel ({z_painel}) acima do overlay borrado ({z_overlay}): os leads ficam visiveis e clicaveis")


# --- Resultado ---
if failures:
    print(f"\n{len(failures)} FALHA(S)")
    sys.exit(1)
print("\nTODOS OS TESTES DE SEGMENTACAO (UI FIX) PASSARAM")
