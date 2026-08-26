# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-WC — regressao estatica dos seis defeitos de C4 (pipeline /
leads UI). Cada checagem le o TEMPLATE fonte e falha se o texto que prova o
fix sumir — nao precisa subir o app nem um browser, so grep no arquivo certo.
Cada item tambem checa a tag `AUDIT-2026-08-WC (...)` que acompanha o fix no
codigo-fonte, para nao colidir com padroes ja existentes em outras funcoes do
mesmo arquivo (ex.: `} else if (resp) {` ja aparece em executeTransfer()).

C5 (filtro de viajantes) nao entra aqui: a investigacao encontrou o filtro em
`>=` (minimo) e DUAS regressoes ja existentes em
tests/test_pipeline_review_final.py (`test_filtro_viajantes_minimo`,
`test_combinacao_destino_tags_viajantes`) que fixam esse `>=` como o
comportamento CORRETO e testado — mudar para "exato" quebraria essas duas
sem poder edita-las (regra do RULES: nao enfraquecer teste existente). Ver o
relatorio da tarefa para os file:line. Nada mudou em app/routers/pipeline.py
por causa disso, e por isso nao ha checagem nova aqui.

Rodar:  python tests/test_pipeline_ui_fixes.py
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):  # pragma: no cover
    pass

falhas = []


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        falhas.append(msg)


pipeline_html = (ROOT / "templates" / "pipeline.html").read_text(encoding="utf-8")
leads_html = (ROOT / "templates" / "leads.html").read_text(encoding="utf-8")
modal_html = (ROOT / "templates" / "partials" / "_lead_edit_modal.html").read_text(encoding="utf-8")


# ─── C4.1 — "Ver no Funil": falha de /locate deixa de ser um no-op mudo ──
print("C4.1) aplicarDeepLink() avisa o usuario quando /locate falha")

check("AUDIT-2026-08-WC (C4.1)" in pipeline_html,
      "a marca do fix de C4.1 esta no arquivo")
check("async function aplicarDeepLink()" in pipeline_html,
      "aplicarDeepLink() ainda existe em pipeline.html")
check("Não foi possível localizar este lead no funil." in pipeline_html,
      "o ramo de falha do /locate mostra uma mensagem ao operador em vez de retornar em silencio")


# ─── C4.2 — card fantasma: onDrop() so muda o DOM a partir da resposta ──
print()
print("C4.2) onDrop() usa a resposta persistida como fonte de verdade e avisa em falha")

check("AUDIT-2026-08-WC (C4.2)" in pipeline_html,
      "a marca do fix de C4.2 esta no arquivo")
check("async function onDrop(e, stageId)" in pipeline_html, "onDrop() ainda existe em pipeline.html")
check("await loadStage(stageId, true);" in pipeline_html,
      "a coluna de destino e recarregada A PARTIR DA RESPOSTA (loadStage), nunca editada no DOM direto")
check("Não foi possível mover o lead. Tente novamente." in pipeline_html,
      "o operador e avisado quando o move nao persiste, em vez do card so ficar 'preso' na coluna antiga")


# ─── C4.3 — formatWhatsappInput: uma unica definicao, no partial compartilhado
print()
print("C4.3) [JA CORRIGIDO — regressao] formatWhatsappInput mora so no partial compartilhado")

check("function formatWhatsappInput(input)" in modal_html,
      "formatWhatsappInput() existe em _lead_edit_modal.html (o partial compartilhado)")
check("function formatWhatsappInput(" not in leads_html,
      "leads.html NAO tem copia duplicada de formatWhatsappInput (senao pipeline.html volta a divergir)")
check('{% include "partials/_lead_edit_modal.html" %}' in leads_html,
      "leads.html inclui o partial compartilhado (e assim que ele ganha a funcao)")
check('{% include "partials/_lead_edit_modal.html" %}' in pipeline_html,
      "pipeline.html inclui o MESMO partial (mesma fonte da funcao nos dois hosts)")


# ─── C4.4 — filtro "Chegada em X dias" agora vira parametro de request ───
print()
print("C4.4) pipeFilterDays vira chegada_de/chegada_ate em stageParams()")

check("AUDIT-2026-08-WC (F-430)" in pipeline_html,
      "a marca do fix de C4.4/F-430 esta no arquivo")
check("function stageParams(stageId, extra)" in pipeline_html, "stageParams() ainda existe em pipeline.html")
check("g('pipeFilterDays')" in pipeline_html,
      "stageParams() agora LE pipeFilterDays (antes so hasActiveFilters()/clearPipeFilters() liam)")
check("p.set('chegada_de', fmt(hoje));" in pipeline_html and "p.set('chegada_ate', fmt(limite));" in pipeline_html,
      "o valor de pipeFilterDays vira chegada_de/chegada_ate no querystring enviado ao backend")


# ─── C4.5 — loadAllTags() e aguardado antes do deep-link ?open= abrir o editor
print()
print("C4.5) leads.html aguarda loadAllTags() antes de abrir o editor via ?open=")

check("AUDIT-2026-08-WC (F-419)" in leads_html,
      "a marca do fix de C4.5/F-419 esta no arquivo")
check("const tagsProntas = loadAllTags();" in leads_html,
      "a Promise de loadAllTags() e capturada em vez de disparada e esquecida")
check("tagsProntas.then(" in leads_html,
      "o deep-link so abre o editor (editLead) DEPOIS que loadAllTags() resolve")


# ─── C4.6 — loadStage nao descarta mais chamadas concorrentes; busca global tem debounce
print()
print("C4.6) loadStage() nao dropa chamada concorrente, e a busca global tem debounce")

check("AUDIT-2026-08-WC (F-165)" in pipeline_html,
      "a marca do fix de C4.6/F-165 esta no arquivo")
check("if (!st || st.loading) return;" not in pipeline_html,
      "a guarda antiga (`if (!st || st.loading) return`) que DESCARTAVA a chamada inteira sumiu")
check("async function loadStage(stageId, reset, extra)" in pipeline_html, "loadStage() ainda existe")
check("const meuReq = ++st.reqId;" in pipeline_html,
      "loadStage() continua usando o sequenciador reqId para descartar RESPOSTA velha (nao a chamada)")
check("function debounceApplyFilters()" in pipeline_html,
      "existe um debounce para applyFilters(), mesmo idioma de debouncePreview() em segmentacao.html")
check('id="pipeFilterSearch"' in pipeline_html and 'oninput="debounceApplyFilters()"' in pipeline_html,
      "a busca global (pipeFilterSearch) chama o debounce, nao mais applyFilters() a cada tecla")


print()
if falhas:
    print(f"{len(falhas)} FALHA(S)")
    sys.exit(1)
print("OK: os seis defeitos de C4 continuam corrigidos (checagem estatica)")
