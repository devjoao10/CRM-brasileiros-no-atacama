# -*- coding: utf-8 -*-
"""
AUDIT-2026-08 — o contrato de escape do front, como REGRA e nao como lista.

Este arquivo existe porque a auditoria global encontrou a MESMA falha em oito
lugares diferentes, escrita por pessoas diferentes, em anos diferentes. Corrigir
os oito sem escrever a regra garante o nono.

A regra, e por que ela nao e obvia:

    O `esc()` / `escapeHtml()` deste repositorio transforma `'` em `&#39;`.
    Isso e CORRETO em conteudo (`<td>${esc(x)}</td>`) e em valor de atributo
    (`title="${esc(x)}"`), porque o parser de HTML decodifica a entidade e o
    resultado vira TEXTO.

    O valor de um atributo `on*` nao vira texto. Depois de decodificado, ele e
    COMPILADO COMO JAVASCRIPT. O parser devolve o `'` ANTES de o compilador ver
    a linha:

        nome    = "x');alert(1);//"
        esc()   = "x&#39;);alert(1);//"
        markup  = onclick="deleteLead(1, 'x&#39;);alert(1);//')"
        o JS ve = deleteLead(1, 'x');alert(1);//')

    Ou seja: dentro de `on*`, `esc()` NAO protege — e pior, da a aparencia de
    proteger, que foi exatamente o que aconteceu em `leads.html`, `pipeline.html`,
    `segmentacao.html`, `tarefas.html`, `tags.html` e `equipes.html`.

    Nomes de lead e destinos sao escritos pelo webhook do WhatsApp e pelo n8n,
    sem passar por operador. Isso e XSS armazenado alcancavel por terceiro, nao
    hipotese de laboratorio.

O que este arquivo afirma:

  1. Nenhum arquivo de front interpola dado dentro de um literal de string JS
     num handler inline. Vale para todo template e todo .js — inclusive os que
     ainda nem existem.
  2. Toda copia de `esc`/`escapeHtml` do repositorio escapa `& < > " '`.
     Verificado EXECUTANDO cada copia sob `node`, nao lendo o codigo dela.
  3. Os sinks especificos que a auditoria fechou continuam fechados.
  4. A fronteira valida `StageSchema.id`.

Rodar:  python tests/test_frontend_injection_contract.py
"""
import io
import json
import os
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ENVIRONMENT", "development")

falhas = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        falhas.append(msg)


def _sem_comentarios(txt):
    """Remove // e /* */ para que uma correcao COMENTADA nao passe no grep."""
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    txt = re.sub(r"(?m)^\s*//.*$", "", txt)
    return txt


ARQUIVOS_FRONT = sorted(
    [p for p in (ROOT / "templates").rglob("*.html")]
    + [p for p in (ROOT / "static" / "js").rglob("*.js")]
    + [p for p in (ROOT / "conversas" / "templates").rglob("*.html")]
    + [p for p in (ROOT / "conversas" / "static" / "js").rglob("*.js")]
)

# ───────────────────────────────────────────────────────────────────────────
# 1. A REGRA: nada interpolado dentro de literal JS de handler inline
# ───────────────────────────────────────────────────────────────────────────
print("1) nenhum dado interpolado dentro de literal JS em handler inline")

# on<algo>="... '${ ... }' ..."  — aspa simples abrindo string logo antes do ${
PADRAO_SINK = re.compile(r"""on[a-z]+\s*=\s*"[^"]*'\$\{([^}]*)\}""")

# Unica excecao aceita, e ela precisa de justificativa que o teste possa conferir:
# uma constante literal declarada no proprio arquivo, cujo conteudo nao vem do
# servidor. Hoje so a paleta de cores de segmentacao.html se qualifica.
def _e_constante_do_arquivo(expr, fonte):
    nome = expr.strip()
    if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", nome):
        return False
    # a variavel do map tem que vir de um array literal const/let de literais
    for m in re.finditer(r"(?:const|let|var)\s+(\w+)\s*=\s*\[([^\]]*)\]", fonte):
        itens = m.group(2)
        if not itens.strip():
            continue
        if re.fullmatch(r"[\s,]*(?:'[^']*'|\"[^\"]*\")(?:\s*,\s*(?:'[^']*'|\"[^\"]*\"))*[\s,]*", itens):
            # o array e 100% literal; a var do map itera sobre ele?
            if re.search(rf"{m.group(1)}\s*\.\s*map\s*\(\s*{re.escape(nome)}\b", fonte):
                return True
    return False


achados = []
for p in ARQUIVOS_FRONT:
    fonte = _sem_comentarios(p.read_text(encoding="utf-8"))
    for m in PADRAO_SINK.finditer(fonte):
        expr = m.group(1)
        if _e_constante_do_arquivo(expr, fonte):
            continue
        linha = fonte[: m.start()].count("\n") + 1
        achados.append(f"{p.relative_to(ROOT).as_posix()}:{linha} -> {m.group(0)[:70]}")

check(not achados, "nenhum handler inline com dado dentro de literal JS "
                   + (f"(achados: {achados})" if achados else ""))

# CONTROLE: sem isto, o teste acima poderia passar por o regex estar quebrado.
CONTROLE = """<button onclick="deleteLead(${l.id}, '${esc(l.nome)}')">x</button>"""
check(PADRAO_SINK.search(CONTROLE) is not None,
      "o detector ACUSA a marcacao antiga (controle: o teste nao e vacuo)")
check(not _e_constante_do_arquivo("esc(l.nome)", CONTROLE),
      "a excecao de constante nao abre a porta para chamada de funcao")

# ───────────────────────────────────────────────────────────────────────────
# 2. Toda copia de esc/escapeHtml escapa & < > " '  (executada, nao lida)
# ───────────────────────────────────────────────────────────────────────────
print("\n2) cada copia de esc()/escapeHtml() escapa & < > \" ' (rodando sob node)")

node = None
for cand in ("node", "node.exe"):
    try:
        subprocess.run([cand, "--version"], capture_output=True, check=True)
        node = cand
        break
    except (OSError, subprocess.CalledProcessError):
        continue
# node ausente e FALHA, nao SKIP: um teste que se desliga sozinho reporta verde
# sobre codigo nao verificado, que e o defeito que esta auditoria mais achou.
check(node is not None, "node disponivel para executar os helpers de escape")

RE_ASSINATURA = re.compile(r"function\s+(esc|escapeHtml)\s*\(\s*\w+\s*\)\s*\{")


def _extrai_funcao(fonte, inicio):
    """Do `{` da assinatura ate a chave que o fecha, contando profundidade.

    Regex nao serve aqui: as copias do repositorio existem em DUAS formas —
    corpo multilinha e one-liner (templates/gestao/pendencias.html:157).
    Qualquer ancora do tipo "chave no comeco da linha" engole codigo alheio na
    segunda forma, e o helper extraido nem compila.
    """
    i = fonte.index("{", inicio)
    prof = 0
    for j in range(i, len(fonte)):
        if fonte[j] == "{":
            prof += 1
        elif fonte[j] == "}":
            prof -= 1
            if prof == 0:
                return fonte[inicio:j + 1]
    return None

STUB = """
globalThis.document = {
  createElement: () => ({
    set textContent(v) { this._t = String(v); },
    get innerHTML() {
      return this._t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    },
  }),
};
"""

copias = []
for p in ARQUIVOS_FRONT:
    fonte = p.read_text(encoding="utf-8")
    for m in RE_ASSINATURA.finditer(fonte):
        corpo = _extrai_funcao(fonte, m.start())
        if corpo:
            copias.append((p, corpo))

check(len(copias) >= 8, f"as copias do helper foram encontradas ({len(copias)})")

for p, corpo in copias:
    nome = re.search(r"function\s+(\w+)", corpo).group(1)
    script = STUB + corpo + f"""
const entrada = "a&b<c>d\\"e'f";
const saida = {nome}(entrada);
console.log(JSON.stringify(saida));
"""
    r = subprocess.run([node or "node", "-e", script], capture_output=True, text=True, encoding="utf-8", errors="replace")
    rel = p.relative_to(ROOT).as_posix()
    if r.returncode != 0:
        check(False, f"{rel}: {nome}() nao executou ({r.stderr.strip()[:80]})")
        continue
    saida = json.loads(r.stdout.strip())
    for bruto, esperado in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"),
                            ('"', "&quot;"), ("'", "&#39;")):
        ok = bruto not in saida.replace("&amp;", "").replace("&lt;", "") \
            .replace("&gt;", "").replace("&quot;", "").replace("&#39;", "")
        if not ok:
            check(False, f"{rel}: {nome}() deixa passar {bruto!r} -> {saida!r}")
            break
    else:
        check(True, f"{rel}: {nome}() escapa os cinco")

# ───────────────────────────────────────────────────────────────────────────
# 3. Os sinks que a auditoria fechou continuam fechados
# ───────────────────────────────────────────────────────────────────────────
print("\n3) sinks especificos fechados por esta auditoria")

pipe = _sem_comentarios((ROOT / "templates" / "pipeline.html").read_text(encoding="utf-8"))
# stage.id vem de StageSchema.id (str livre do cliente), nao de uma PK inteira.
check(not re.search(r'(?:id|data-stage)="[^"]*\$\{stage\.id\}', pipe),
      "pipeline.html: stage.id nao vai CRU para atributo")
check(pipe.count("${esc(stage.id)}") >= 6,
      f"pipeline.html: stage.id escapado nos atributos (achou {pipe.count('${esc(stage.id)}')})")
check("this.dataset.stage" in pipe,
      "pipeline.html: os handlers leem stage do dataset, nao de literal interpolado")

seg = _sem_comentarios((ROOT / "templates" / "segmentacao.html").read_text(encoding="utf-8"))
# allDestinos = GET /api/leads/destinos = agregado do campo `destinos` de TODO
# lead, que o n8n escreve. Nao e vocabulario fechado.
check('data-destino="${esc(d)}"' in seg and "toggleDestino(this.dataset.destino)" in seg,
      "segmentacao.html: chip de destino passa pelo dataset")

leads = _sem_comentarios((ROOT / "templates" / "leads.html").read_text(encoding="utf-8"))
check("openConversas(this)" in leads and "deleteLead(this)" in leads,
      "leads.html: nome/whatsapp do lead saem do onclick e vao para data-*")

tpl_js = _sem_comentarios(
    (ROOT / "conversas" / "static" / "js" / "templates.js").read_text(encoding="utf-8"))
check("escapeHtml(t.language)" in tpl_js and "escapeHtml(t.meta_template_id)" in tpl_js,
      "templates.js: language e meta_template_id escapados")

set_js = _sem_comentarios(
    (ROOT / "conversas" / "static" / "js" / "settings.js").read_text(encoding="utf-8"))
check("this.dataset.trigger" in set_js and "${r.trigger}" not in set_js,
      "settings.js: trigger da auto-reply nao e mais interpolado cru")

# ───────────────────────────────────────────────────────────────────────────
# 4. A fronteira: escapar no template e a defesa; validar aqui evita a proxima
# ───────────────────────────────────────────────────────────────────────────
print("\n4) StageSchema.id validado na entrada")

from app.schemas.pipeline import StageSchema  # noqa: E402
import pydantic  # noqa: E402

# AUDIT-2026-08-F2: o padrao deixou de ser uma allowlist de slug.
# Motivo, por extenso em app/schemas/pipeline.py: `FunnelUpdate` revalida a
# lista `etapas` INTEIRA, entao exigir slug faria QUALQUER edicao de um funil de
# producao cuja etapa tenha espaco ou acento devolver 422 — e o system message
# do "Agente Gerenciador de Leads" chama a etapa de "Sem Contato". Defesa em
# profundidade que derruba funcionalidade legitima nao e defesa.
# O que segue travado e o que de fato protege: o esc() no template, verificado
# na secao 3 acima e na regra da secao 1.
for bom in ("novo", "contato", "negociacao", "e1", "stage-1", "A_b-9",
            "sem_contato", "Sem Contato", "Pré-venda", "etapa 1"):
    try:
        StageSchema(id=bom, nome="x")
        check(True, f"id legitimo aceito: {bom!r}")
    except pydantic.ValidationError as e:
        check(False, f"id legitimo REJEITADO: {bom!r} ({e})")

for mau, porque in (
    ("x');alert(1);//", "quebra literal JS"),
    ('a"b', "quebra atributo aspeado"),
    ("<script>", "markup"),
    ("a&b", "entidade HTML"),
    ("a\\b", "barra invertida"),
    ("tab\tid", "caractere de controle"),
    ("nul\x00id", "NUL"),
    ("", "vazio"),
    ("x" * 65, "acima de 64 (funnel_entries.etapa_id e String(100))"),
):
    try:
        StageSchema(id=mau, nome="x")
        check(False, f"payload aceito indevidamente ({porque}): {mau[:20]!r}")
    except pydantic.ValidationError:
        check(True, f"payload rejeitado ({porque})")


print()
if falhas:
    print(f"{len(falhas)} FALHA(S)")
    sys.exit(1)
print("OK: contrato de escape do front mantido")
