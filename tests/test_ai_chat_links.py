# -*- coding: utf-8 -*-
"""
SEC-LINK-01 - links do chat da IA: o destino externo e sempre visivel e so o
download do proprio CRM mantem o comportamento de download.

A AMEACA. templates/ai.html e o chat interno "Assistente IA" (so equipe). O LLM
le texto de terceiros (campos de lead escritos pelo WhatsApp e pelo n8n, sem
operador no meio), entao uma resposta induzida por prompt-injection pode trazer
um link enganoso (o texto da ancora nao mostra o destino), imitar o botao de
download, esconder o selo de dominio ou cobrir a pagina. O DOMPurify em perfil
padrao nao impede nada disso: mantem <style>, style=, class=, id/name, <dialog>,
download= e data-*, e deixa passar <area href>, <a> de SVG e <form action>. E o
handler de clique reconhecia o download com uma regex nao ancorada: aceitava
`/`, `..`, pontos extras e host alheio no que capturava e tolerava query no
href (a captura parava no `?`), disparando fetch e navegacao com isso.

A DECISAO E COMO SE PROVA. O link continua clicavel: externo abre em nova aba,
sem opener nem referrer, com o DOMINIO REAL como selo; download so no formato
que o servidor gera (_safe_document_target, app/services/ai_tools.py). Sem jsdom
nem passo npm no CI, o JS REAL do template e extraido do ai.html e roda sob
`node` com stubs escritos a mao (elemento falso, window.location.origin, fetch,
alert): roda o codigo que vai para o navegador, nao uma copia.

O CONTRATO (dentro do callback de DOMContentLoaded; "nivel do callback" e o
recuo da linha chatHistory.addEventListener('click' ...)):
  1. DOWNLOAD_IA_RE, numa linha, no formato do gerador: <base 1-64>_<6 hex>.<xlsx|pdf>.
  2. Hook afterSanitizeAttributes: function declarada, registrada UMA vez por
     nome, que so depende de DOWNLOAD_IA_RE e window.location.origin. http(s)
     externo: nova aba, noopener noreferrer, selo = URL.host (com porta; acima
     de 48 chars, reticencias + os 47 finais). Download canonico: href limpo.
     mailto: e tel: intactos; qualquer outro esquema perde o href. data-dominio
     forjado sai de TODO elemento. <area>, <a> de SVG e <form> perdem o destino.
  3. O handler de clique so intercepta o que o DOWNLOAD_IA_RE aprova, sem alert.
  4. CSS: a[data-dominio]::before com attr(data-dominio) e overflow-wrap:
     anywhere; botao de download so em a[href^="/api/ai/download/"]:not(
     [data-dominio]); todo seletor com [href (ou ai/download) segue esse formato.
  5. Render: PERFIL_RESPOSTA_IA (allowlist exata, sem class/style/img/data-*) e
     o statement bubble.innerHTML = isUser ? ... : DOMPurify.sanitize(
     marked.parse(content), PERFIL_RESPOSTA_IA); com um unico marked.parse( e um
     unico DOMPurify.addHook(, e nada que alargue o perfil (setConfig, ADD_*...).

Rodar:  python tests/test_ai_chat_links.py
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOST_CRM = "crm.test"
ORIGEM = "https://" + HOST_CRM
HREF_INICIAL = ORIGEM + "/ai"
BS = chr(92)  # barra invertida, montada com chr() para o fonte ficar ASCII e sem escape ambiguo
LIMITE_SELO = 48  # data-dominio acima disso vira reticencias + os (LIMITE_SELO - 1) ultimos chars
RETICENCIAS = chr(0x2026)

falhas = []


def check(cond, msg, detalhe=""):
    """PASS/FAIL de uma afirmacao; `detalhe` so aparece (e so e registrado) quando ela falha."""
    if cond:
        print(f"  PASS: {msg}")
    else:
        linha = f"{msg} | {detalhe}" if detalhe else msg
        print(f"  FAIL: {linha}")
        falhas.append(linha)


def _sem_comentarios(txt):
    """Remove comentario de linha inteira e de bloco que abre a linha (uma correcao
    COMENTADA nao pode passar). Nao toca em `//` no meio da linha: e URL ou fim de regex."""
    txt = re.sub(r"(?ms)^[ \t]*/\*.*?\*/", "", txt)
    return re.sub(r"(?m)^[ \t]*//.*$", "", txt)


def _recuo(fonte, pos):
    """Recuo (em espacos) da linha de `pos`; -1 se ha codigo antes de `pos` nela."""
    ini = fonte.rfind("\n", 0, pos) + 1
    prefixo = fonte[ini:pos]
    return len(prefixo) if prefixo.strip(" ") == "" else -1


def _extrai_ate_fechar(fonte, inicio):
    """De `inicio` ate a `}` que fecha o primeiro `{` seguinte. Contador simples, como em
    test_frontend_injection_contract.py: basta se nao ha chave em string, regex ou comentario."""
    try:
        i = fonte.index("{", inicio)
    except ValueError:
        return None
    prof = 0
    for j in range(i, len(fonte)):
        if fonte[j] == "{":
            prof += 1
        elif fonte[j] == "}":
            prof -= 1
            if prof == 0:
                return fonte[inicio:j + 1]
    return None


def _extrai_listener_click(js):
    """O statement inteiro `chatHistory.addEventListener('click', ...);`, ou None."""
    ini = js.find("chatHistory.addEventListener('click'")
    corpo = _extrai_ate_fechar(js, ini) if ini >= 0 else None
    fecha = re.match(r"\s*\)", js[ini + len(corpo):]) if corpo else None
    # `;` sempre: sem ele, o `(async () => ...)()` do harness, na linha seguinte,
    # seria lido como chamada do resultado do addEventListener.
    return js[ini:ini + len(corpo) + fecha.end()] + ";" if fecha else None


node = shutil.which("node")


def _roda_node(script, rotulo):
    """Roda `script` sob node (via stdin: sem o teto de argv do Windows) e devolve o dict JSON.

    Qualquer falha (nao executou, saiu com erro, imprimiu lixo ou JSON que nao e objeto) vira
    um FAIL e devolve None: o teste nunca morre com traceback."""
    try:
        r = subprocess.run([node, "-"], input=script, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60)
    except subprocess.TimeoutExpired:
        check(False, f"{rotulo}: node excedeu 60s (loop infinito no JS do template?)")
        return None
    except OSError as e:
        check(False, f"{rotulo}: node nao executou", f"{e.__class__.__name__}: {str(e)[:200]!a}")
        return None
    if r.returncode != 0:
        check(False, f"{rotulo}: node saiu com codigo {r.returncode}", f"{r.stderr.strip()[-400:]!a}")
        return None
    try:
        dados = json.loads(r.stdout)
    except ValueError as e:
        check(False, f"{rotulo}: a saida do node nao e JSON valido", f"{e}; saida={r.stdout[:200]!a}")
        return None
    if not isinstance(dados, dict):
        # `[]`, `null` ou `5` sao JSON valido, mas quem le dados[...] morreria com TypeError.
        check(False, f"{rotulo}: a saida do node e JSON mas nao e um objeto",
              f"{type(dados).__name__}: {r.stdout[:200]!a}")
        return None
    return dados


def _dict_ou_vazio(valor):
    """`valor` se for dict, senao {}: a saida do node so e confiavel depois de validada."""
    return valor if isinstance(valor, dict) else {}


def _caso(resultados, cid, descricao):
    """O resultado do caso `cid` (um dict), ou None com FAIL se a saida do node nao o trouxe."""
    r = resultados.get(cid)
    if isinstance(r, dict):
        return r
    check(False, f"{cid} {descricao}", "a saida do node nao traz resultado para o caso")
    return None


# Comum as secoes 2 e 3. O console e silenciado porque o handler real chama console.log/error e o
# stdout precisa ser UM documento JSON (impresso por __out). O hook so enxerga location.origin.
_PRELUDIO_JS = "\n".join([
    "const __out = process.stdout.write.bind(process.stdout);",
    "console.log = console.error = console.warn = () => {};",
    "globalThis.window = { location: { origin: " + json.dumps(ORIGEM) + " } };",
    "globalThis.location = window.location;",
])

fonte = (ROOT / "templates" / "ai.html").read_text(encoding="utf-8")
js = _sem_comentarios(fonte)

# ---- 1. Contrato estatico de templates/ai.html --------------------------------------------
print("1) contrato estatico de templates/ai.html (hook, constante, perfil da IA, CSS)")

# "Nivel do callback" = recuo da linha do handler de clique. Constante, hook, registro e perfil
# precisam estar nele: dentro de appendMessage o hook seria registrado a cada mensagem, e dentro
# de outra funcao a constante nao seria visivel para o handler de clique.
_ini = js.find("chatHistory.addEventListener('click'")
NIVEL_CALLBACK = _recuo(js, _ini) if _ini >= 0 else -1
NIVEL_CALLBACK = NIVEL_CALLBACK if NIVEL_CALLBACK >= 0 else None  # None nunca iguala um recuo
NIVEL_TXT = (f"{NIVEL_CALLBACK} espacos" if NIVEL_CALLBACK is not None else
             "indeterminado: chatHistory.addEventListener('click' nao esta no inicio de uma linha")


def _achados(regex):
    """Todos os matches de `regex` em `js`, e os que estao no nivel do callback."""
    todos = list(regex.finditer(js))
    return todos, [m for m in todos if _recuo(js, m.start()) == NIVEL_CALLBACK]


RE_ADDHOOK = re.compile(r"""DOMPurify\.addHook\(\s*['"]afterSanitizeAttributes['"]\s*,""")
RE_REGISTRO = re.compile(
    r"""DOMPurify\.addHook\(\s*['"]afterSanitizeAttributes['"]\s*,\s*([A-Za-z_$][\w$]*)\s*\)""")
todos, _ = _achados(RE_ADDHOOK)
regs, regs_nivel = _achados(RE_REGISTRO)
ok = len(todos) == 1 and len(regs) == 1 and len(regs_nivel) == 1
check(ok, f"hook afterSanitizeAttributes registrado exatamente 1 vez, por nome, no nivel do callback ({NIVEL_TXT})",
      "hook afterSanitizeAttributes nao registrado em templates/ai.html" if not todos else
      "registre o hook por nome (function declarada), nao com arrow ou expressao" if len(regs) != len(todos) else
      f"achou {len(todos)} registro(s), {len(regs_nivel)} no nivel do callback "
      "(dentro de appendMessage acumularia um hook por mensagem)")

nome_hook = regs[0].group(1) if regs else None
registro = (regs[0].group(0) + ";") if regs else None
funcao_hook = None
if nome_hook is None:
    check(False, "corpo do hook extraivel de `function <nome>(`",
          "pre-requisito ausente: sem registro por nome nao ha funcao para extrair")
else:
    decls, decls_nivel = _achados(re.compile(r"function\s+" + re.escape(nome_hook) + r"\s*\("))
    funcao_hook = _extrai_ate_fechar(js, decls[0].start()) if decls else None
    ok = len(decls) == 1 and len(decls_nivel) == 1 and funcao_hook is not None
    check(ok, f"function {nome_hook}(node) {{ ... }} declarada 1 vez no nivel do callback, com o corpo extraivel",
          f"achou {len(decls)} declaracao(oes), {len(decls_nivel)} no nivel do callback, "
          f"corpo extraivel: {funcao_hook is not None}")

# Uma linha so (o harness a copia como esta), igual ao formato do gerador
# _safe_document_target: <base 1-64>_<6 hex>.<xlsx|pdf>. O `.+?` preguicoso para na primeira `/;`.
REGEX_DOWNLOAD = r"/^\/api\/ai\/download\/([A-Za-z0-9_-]{1,64}_[0-9a-f]{6}\.(?:xlsx|pdf))$/"
consts, consts_nivel = _achados(re.compile(r"const\s+DOWNLOAD_IA_RE\s*=\s*(/.+?/[a-z]*)\s*;"))
decl_const = consts[0].group(0) if consts else None
ok = len(consts) == 1 and len(consts_nivel) == 1 and consts[0].group(1) == REGEX_DOWNLOAD
check(ok, "const DOWNLOAD_IA_RE = <formato do gerador>; declarada 1 vez, numa linha, no nivel do callback",
      f"achou {len(consts)} declaracao(oes), {len(consts_nivel)} no nivel do callback; "
      f"regex: {ascii(consts[0].group(1) if consts else None)}; esperada: {REGEX_DOWNLOAD}")

# ---- ramo da IA: allowlist. O perfil padrao do DOMPurify deixa a resposta reusar classes da
# propria pagina (class="sidebar" vira menu falso), <dialog open>, <nobr>/<marquee>, id/name (DOM
# clobbering do onclick inline), download= e data-*. A allowlist e o que o marked 12 (gfm, breaks)
# emite para texto; img, input e class ficam de fora de proposito.
PERFIL_ESPERADO = {
    "ALLOWED_TAGS": {"p", "br", "hr", "strong", "em", "del", "code", "pre", "blockquote", "ul", "ol",
                     "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead", "tbody", "tr", "th",
                     "td", "a"},
    "ALLOWED_ATTR": {"href", "title", "align", "start"},
    "ALLOW_DATA_ATTR": False,
}
N_TAGS, N_ATTRS = len(PERFIL_ESPERADO["ALLOWED_TAGS"]), len(PERFIL_ESPERADO["ALLOWED_ATTR"])
PROIBIDOS = ("setConfig", "ADD_TAGS", "ADD_ATTR", "ADD_URI_SAFE_ATTR", "ADD_DATA_URI_TAGS",
             "ALLOW_UNKNOWN_PROTOCOLS", "ALLOWED_URI_REGEXP", "CUSTOM_ELEMENT_HANDLING", "forceKeepAttr")
S = r"\s*"  # tokens do statement separados por whitespace livre; a barra invertida de /\n/g sai de re.escape
RE_RENDER = re.compile(S.join([
    r"bubble\.innerHTML", "=", "isUser", r"\?",
    r"DOMPurify\.sanitize\(", r"content\.replace\(", "/" + re.escape(BS) + "n/g", ",", r"""['"]<br>['"]""",
    r"\)", ",", r"\{", "ALLOWED_TAGS", ":", r"\[", r"""['"]br['"]""", r"\]", r"\}", r"\)", ":",
    r"DOMPurify\.sanitize\(", r"marked\.parse\(", "content", r"\)", ",", "PERFIL_RESPOSTA_IA", r"\)", ";",
]))
n_render = len(RE_RENDER.findall(js))
check(n_render == 1, "statement de render nos dois ramos: bubble.innerHTML = isUser ? DOMPurify.sanitize("
                     "content.replace(...), { ALLOWED_TAGS: ['br'] }) : DOMPurify.sanitize("
                     "marked.parse(content), PERFIL_RESPOSTA_IA);", f"achou {n_render} statement(s) nesse formato")

n_parse, n_addhook = js.count("marked.parse("), js.count("DOMPurify.addHook(")
check(n_parse == 1 and n_addhook == 1, "superficie de sanitizacao unica: exatamente 1 marked.parse( e 1 DOMPurify.addHook(",
      f"achou {n_parse} marked.parse( e {n_addhook} DOMPurify.addHook(")

achados = [n for n in PROIBIDOS if re.search(r"(?<![\w$])" + re.escape(n) + r"(?![\w$])", js)]
check(not achados, "nada alarga o perfil da IA: nenhum uso de " + ", ".join(PROIBIDOS),
      f"achou {achados} (comentario de linha inteira nao conta)")


def _difs_perfil(literal):
    """Diferencas entre o literal JS do perfil e PERFIL_ESPERADO (lista vazia = igual)."""
    txt = re.sub(r"//[^\n]*", "", literal)
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    txt = re.sub(r"'([^']*)'", r'"\1"', txt)                               # aspas simples -> duplas
    txt = re.sub(r"([{,]\s*)([A-Za-z_$][\w$]*)(\s*:)", r'\1"\2"\3', txt)  # chaves sem aspas
    txt = re.sub(r",(\s*[\]}])", r"\1", txt)                               # virgula final
    try:
        obtido = json.loads(txt)
    except ValueError as e:
        return [f"literal ilegivel ({e})"]
    if not isinstance(obtido, dict):
        return [f"nao e um objeto: {obtido!a}"]
    difs = [f"chave a mais: {k}" for k in sorted(obtido) if k not in PERFIL_ESPERADO]
    for k, esperado in PERFIL_ESPERADO.items():
        valor = obtido.get(k, "<ausente>")
        if isinstance(esperado, set):
            if not (isinstance(valor, list) and all(isinstance(x, str) for x in valor)):
                difs.append(f"{k}: esperava lista de strings, veio {valor!a}")
            elif set(valor) != esperado:
                difs.append(f"{k}: a mais {sorted(set(valor) - esperado)}, faltando {sorted(esperado - set(valor))}")
        elif valor is not esperado:
            difs.append(f"{k}: veio {valor!a}, esperado {esperado!a}")
    return difs


perfis, perfis_nivel = _achados(re.compile(r"const\s+PERFIL_RESPOSTA_IA\s*=\s*\{"))
if len(perfis) == 1 and len(perfis_nivel) == 1:
    literal = _extrai_ate_fechar(js, perfis[0].end() - 1)
    difs = _difs_perfil(literal) if literal else ["literal sem fechamento"]
else:
    difs = [f"achou {len(perfis)} declaracao(oes), {len(perfis_nivel)} no nivel do callback ({NIVEL_TXT})"]
check(not difs, f"const PERFIL_RESPOSTA_IA = {{...}}; declarada 1 vez no nivel do callback, igual a allowlist "
                f"esperada ({N_TAGS} tags, {N_ATTRS} atributos, ALLOW_DATA_ATTR false)", "; ".join(difs))

# ---- CSS: selo de dominio e botao de download --------------------------------------------
# O selo mora no INICIO do link (::before): texto longo, nbsp ou nowrap no fim nao o empurram
# para fora, e overflow-wrap: anywhere o impede de estourar a bolha.
estilos = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", fonte, flags=re.S | re.I))
estilos = re.sub(r"/\*.*?\*/", "", estilos, flags=re.S)
regras_css = re.findall(r"([^{}]+)\{([^{}]*)\}", estilos)
selos = [sel for sel, corpo in regras_css
         if "a[data-dominio]::before" in sel and "attr(data-dominio)" in corpo
         and re.search(r"overflow-wrap\s*:\s*anywhere", corpo)]
check(bool(selos), "regra CSS a[data-dominio]::before com attr(data-dominio) e overflow-wrap: anywhere "
                   "(o selo que mostra o dominio real, no inicio do link)")

# O botao oficial de download tambem e so CSS. Casar por substring ([href*=...]) deixaria com a
# cara do botao todo link externo que imita o caminho (https://golpe.example/api/ai/download/x,
# //golpe..., mailto:x@y?subject=/api/ai/download/x, /api/ai/download/../../x). Depois do hook, o
# download genuino tem href que COMECA com /api/ai/download/ e NUNCA tem data-dominio; todo http(s)
# que nao e download TEM. Prefixo + :not() alcanca so o primeiro grupo. A regra vale para todo
# composto com condicao sobre href (ou que cite ai/download), nao so o do botao.
FORMA_BOTAO = 'a[href^="/api/ai/download/"]:not([data-dominio])'
RE_PREFIXO_DOWNLOAD = re.compile(r"""(?<![\w-])a\[href\^=(["'])/api/ai/download/\1\]""")
RE_CASAMENTO_FRACO = re.compile(r"""\[[^\]]*[*$~]=[^\]]*ai/download""")  # [x*=..], [x$=..], [x~=..]
# [href], [href^=..], [ HREF *= ..], [*|href..], [ns|href..]; nao casa [hreflang..] nem [data-href..]
RE_CONDICAO_HREF = re.compile(r"\[\s*(?:(?:[\w-]+|\*)?\|)?\s*href\s*(?:[~|^$*]?=|\])", re.I)


def _compostos(sel):
    """Divide o seletor nos combinadores (espaco, >, +, ~) fora de [], () e aspas."""
    partes, atual, prof, aspa = [], "", 0, ""
    for c in sel:
        if aspa:
            atual += c
            aspa = "" if c == aspa else aspa
        elif c in ('"', "'"):
            atual += c
            aspa = c
        elif c in "[(":
            prof += 1
            atual += c
        elif c in "])":
            prof -= 1
            atual += c
        elif prof == 0 and (c.isspace() or c in ">+~"):
            if atual:
                partes.append(atual)
            atual = ""
        else:
            atual += c
    return partes + [atual] if atual else partes


def _botao_no_contrato(composto):
    """O composto tem o prefixo, o :not([data-dominio]) e nenhum casamento fraco."""
    return (RE_PREFIXO_DOWNLOAD.search(composto) is not None
            and ":not([data-dominio])" in composto
            and RE_CASAMENTO_FRACO.search(composto) is None)


def _seletor_fora_do_contrato(sel):
    """O DETECTOR do guard de CSS (o mesmo roda no <style> real e no controle sintetico).

    Julga cada composto do seletor, nao so o mais a direita: `<botao> ~ a[href*="x"]` tem o
    botao do contrato num composto e um casamento fraco no outro.
    """
    return any((RE_CONDICAO_HREF.search(c) is not None or "ai/download" in c) and not _botao_no_contrato(c)
               for c in _compostos(sel))


# Cada seletor de uma lista (`a, b { }`) e julgado sozinho.
seletores = [" ".join(s.split()) for sel, _corpo in regras_css for s in sel.split(",") if s.strip()]
fora_do_contrato = [s for s in seletores if _seletor_fora_do_contrato(s)]
check(not fora_do_contrato, "todo seletor com condicao [href (ou que cita ai/download) e o botao no formato do "
                            f"contrato: {FORMA_BOTAO}, nunca [href*=], [href$=], [href~=] nem prefixo sem :not",
      f"seletores fora do contrato: {ascii(fora_do_contrato)}")
botoes = [s for s in seletores if _botao_no_contrato(s)]
check(bool(botoes), f"existe regra de estilo do botao de download no formato do contrato ({FORMA_BOTAO}); "
                    "sem ela o check acima seria vacuo",
      f"nenhum dos {len(seletores)} seletores do <style> esta no formato do contrato")

# CONTROLE SINTETICO: o MESMO detector, sobre seletores escritos a mao. Sem isto o guard poderia
# passar so porque o detector esta quebrado e nao acusa nada.
DEVE_ACUSAR = [
    '.chat-bubble a[href*="download"]', 'a[href$=".xlsx"]', 'a[href*="/api/ai/"]', ".chat-bubble a[href]",
    'a[href^="/api/ai/download/"]',                       # prefixo sem :not([data-dominio])
    "a[href^='/api/ai/download/']", 'a[ HREF *= "download" ]', 'a[*|href*="download"]', "[xlink|href]",
    'a[href^="/api/ai/download/"]:not([data-dominio]) ~ a[href*="download"]',   # contrato so no 1o composto
    'a[href^="/api/ai/download/"]:not([data-dominio]) > a[href*="x"]',
]
DEVE_ACEITAR = [
    '.chat-bubble a[href^="/api/ai/download/"]:not([data-dominio])',
    '.chat-bubble a[href^="/api/ai/download/"]:not([data-dominio]):hover',
    ".chat-bubble a[href^='/api/ai/download/']:not([data-dominio])",
    ".chat-bubble a", '.chat-bubble a[hreflang="pt"]', ".chat-bubble a[hreflang]", "a[data-dominio]::before",
]
erros_detector = ([f"nao acusou {s!a}" for s in DEVE_ACUSAR if not _seletor_fora_do_contrato(s)]
                  + [f"acusou {s!a}" for s in DEVE_ACEITAR if _seletor_fora_do_contrato(s)])
check(not erros_detector, f"controle sintetico: o detector acusa os {len(DEVE_ACUSAR)} seletores que escapam do "
                          f"contrato e aceita os {len(DEVE_ACEITAR)} que o seguem", f"o detector errou: {erros_detector}")

# ---- 2. Comportamento do hook (o registrado de fato, sob node) ----------------------------
print("\n2) comportamento do hook afterSanitizeAttributes (rodando sob node)")

# node ausente e FALHA, nao SKIP, e e reportado UMA vez aqui: um teste que se desliga sozinho
# reporta verde sobre codigo nao verificado.
check(node is not None, "node disponivel para executar o JS real do template")

EXT = {"target": "_blank", "rel": "noopener noreferrer"}
HOMOGRAFO = "https://" + chr(0x430) + "pple.com/"  # a cirilico (U+0430), nao o a latino
HOMOGRAFO_HOST = (chr(0x430) + "pple.com").encode("idna").decode("ascii")
LONGO = "drive.google.com." + "a" * 40 + ".golpe.example"
DL = "/api/ai/download/"
DL1, PDF1, DLX = DL + "relatorio_leads_abc123.xlsx", DL + "relatorio_leads_abc123.pdf", DL + "x_abc123.xlsx"


def _host(n):
    """Host de n caracteres terminado em .golpe.example (n >= 15)."""
    return "a" * (n - len(".golpe.example")) + ".golpe.example"


def _curto(host):
    """data-dominio esperado: o host inteiro; acima do limite, reticencias + os ultimos chars."""
    return host if len(host) <= LIMITE_SELO else RETICENCIAS + host[-(LIMITE_SELO - 1):]


def _externo(href, dominio):
    """Atributos esperados de um link tratado como externo."""
    return {"href": href, **EXT, "data-dominio": dominio}


check(HOMOGRAFO_HOST.startswith("xn--") and _curto(LONGO).endswith(".golpe.example")
      and len(_curto(LONGO)) == LIMITE_SELO and _curto(_host(LIMITE_SELO)) == _host(LIMITE_SELO),
      f"dados dos casos coerentes: H11 esperado em punycode (nao o look-alike); selo longo termina no dominio "
      f"registravel e tem {LIMITE_SELO} chars", f"{HOMOGRAFO_HOST!a} {_curto(LONGO)!a}")

# (id, tagName, atributos de entrada, atributos esperados, descricao).
# tagName e o do DOM: HTML em maiusculas, <a> de SVG em minuscula.
CASOS_HOOK = [
    ("H1", "A", {"href": DL1}, {"href": DL1}, "download do CRM (relativo) fica intacto, sem target/rel/selo"),
    ("H2", "A", {"href": ORIGEM + PDF1}, {"href": PDF1}, "download em URL absoluta same-origin vira o caminho relativo"),
    ("H3", "A", {"href": DLX, "data-dominio": "drive.google.com"}, {"href": DLX},
     "selo forjado em download do CRM e removido"),
    ("H4", "A", {"href": "https://drive.google.com/drive/folders/abc"},
     _externo("https://drive.google.com/drive/folders/abc", "drive.google.com"),
     "link externo: nova aba, noopener noreferrer e selo com o dominio real"),
    ("H5", "A", {"href": "https://golpe.example/planilha"}, _externo("https://golpe.example/planilha", "golpe.example"),
     "link externo: o selo mostra o destino, nao o texto da ancora"),
    ("H6", "A", {"href": "https://drive.google.com@golpe.example/x"},
     _externo("https://drive.google.com@golpe.example/x", "golpe.example"),
     "truque de userinfo (drive.google.com@golpe.example): o selo mostra o host REAL"),
    ("H7", "A", {"href": "//golpe.example" + DLX}, _externo("//golpe.example" + DLX, "golpe.example"),
     "protocol-relative imitando o caminho de download: externo"),
    ("H8", "A", {"href": "https://golpe.example" + DLX}, _externo("https://golpe.example" + DLX, "golpe.example"),
     "mesmo caminho de download em host alheio: externo, nao e download do CRM"),
    ("H9", "A", {"href": DL + "../../api/leads/export.csv"}, _externo(DL + "../../api/leads/export.csv", HOST_CRM),
     "traversal a partir do caminho de download NAO e download: link comum"),
    ("H10", "A", {"href": "https://golpe.example", "data-dominio": "drive.google.com", "target": "_self", "rel": "opener"},
     _externo("https://golpe.example", "golpe.example"), "selo, target e rel forjados em link externo sao sobrescritos"),
    ("H11", "A", {"href": HOMOGRAFO}, _externo(HOMOGRAFO, HOMOGRAFO_HOST),
     "homografo (a cirilico): o selo mostra o punycode, nao o look-alike"),
    ("H12", "A", {"href": "/pipeline"}, _externo("/pipeline", HOST_CRM),
     "link interno comum tambem abre em nova aba, com o proprio dominio no selo"),
    ("H13", "A", {"href": "mailto:joao@example.com"}, {"href": "mailto:joao@example.com"}, "mailto: fica intacto"),
    ("H14", "A", {"href": "mailto:x@example.com", "data-dominio": "drive.google.com"}, {"href": "mailto:x@example.com"},
     "selo forjado em mailto: e removido"),
    ("H15", "A", {"name": "x"}, {"name": "x"}, "ancora sem href: nada e adicionado"),
    ("H16", "AREA", {"href": "https://golpe.example", "shape": "rect"}, {"shape": "rect"}, "<area href> perde o href"),
    ("H17", "a", {"href": "https://golpe.example", "xlink:href": "https://golpe.example"}, {},
     "<a> de SVG (tagName minusculo) perde href e xlink:href"),
    ("H18", "FORM", {"action": "https://golpe.example/coleta", "method": "post"}, {"method": "post"},
     "<form action> perde o action"),
    ("H19", "BUTTON", {"formaction": "https://golpe.example", "type": "submit"}, {"type": "submit"},
     "<button formaction> perde o formaction"),
    ("H20", "P", {"class": "x"}, {"class": "x"}, "elemento sem relacao com link fica intacto"),
    ("H21", "A", {"href": DLX + "?t=1#y"}, {"href": DLX}, "download com query e fragmento vira o caminho limpo"),
    ("H22", "A", {"href": DL + "sub/x_abc123.xlsx"}, _externo(DL + "sub/x_abc123.xlsx", HOST_CRM),
     "subdiretorio no caminho de download: NAO e download"),
    ("H23", "A", {"href": "/ai/download/x_abc123.xlsx"}, _externo("/ai/download/x_abc123.xlsx", HOST_CRM),
     "caminho sem o prefixo /api: NAO e download"),
    ("H24", "A", {"href": "http://["}, {}, "URL impossivel de interpretar: href removido, nada adicionado"),
    # Allowlist de esquemas: http(s) e tratado acima; mailto: (H13, H14) e tel: ficam como o DOMPurify
    # deixou (abrem o app de e-mail ou de telefone; nada carrega na aba do CRM). Todo OUTRO esquema
    # perde o href, sem depender so do filtro de URI do DOMPurify.
    ("H25", "A", {"href": "tel:+5511999999999"}, {"href": "tel:+5511999999999"},
     "tel: fica intacto (abre o app de telefone, nada carrega na aba do CRM)"),
    ("H26", "A", {"href": "sms:+5511999999999?body=oi"}, {}, "sms: perde o href (esquema fora da allowlist)"),
    ("H27", "A", {"href": "ftp://golpe.example/x"}, {}, "ftp: perde o href, mesmo com host: nao vira link externo"),
    ("H28", "A", {"href": "javascript:alert(1)"}, {}, "javascript: perde o href (defesa em profundidade)"),
    ("H29", "A", {"href": "data:text/html,<b>x</b>"}, {}, "data: perde o href (defesa em profundidade)"),
    ("H30", "A", {"href": "callto:golpe"}, {}, "callto: perde o href"),
    ("H31", "A", {"href": "xmpp:golpe@example.com", "data-dominio": "drive.google.com"}, {},
     "xmpp: perde o href e o selo forjado nao sobra"),
    # Selo = URL.host (com porta); acima de LIMITE_SELO chars vira reticencias + os ultimos (carregam
    # o dominio registravel). O limite e testado nos dois lados (H35 intacto, H36 truncado).
    ("H32", "A", {"href": ORIGEM + ":8443/x"}, _externo(ORIGEM + ":8443/x", HOST_CRM + ":8443"),
     "porta nao-padrao entra no selo"),
    ("H33", "A", {"href": "http://localhost:5678/webhook"}, _externo("http://localhost:5678/webhook", "localhost:5678"),
     "porta de servico interno aparece no selo"),
    ("H34", "A", {"href": "https://" + LONGO + "/x"}, _externo("https://" + LONGO + "/x", _curto(LONGO)),
     "host longo: reticencias + os ultimos chars, terminando no dominio registravel"),
    ("H35", "A", {"href": "https://" + _host(LIMITE_SELO) + "/x"},
     _externo("https://" + _host(LIMITE_SELO) + "/x", _host(LIMITE_SELO)), "host no limite exato: selo intacto"),
    ("H36", "A", {"href": "https://" + _host(LIMITE_SELO + 1) + "/x"},
     _externo("https://" + _host(LIMITE_SELO + 1) + "/x", _curto(_host(LIMITE_SELO + 1))),
     "host 1 char acima do limite: truncado para o limite"),
    ("H37", "SPAN", {"data-dominio": "drive.google.com", "class": "x"}, {"class": "x"},
     "selo forjado sai de TODO elemento, nao so do <a>"),
    ("H38", "A", {"href": "tel:+55", "data-dominio": "drive.google.com"}, {"href": "tel:+55"},
     "selo forjado em tel: e removido"),
    ("H39", "A", {"href": "HTTPS://GOLPE.EXAMPLE/x"}, _externo("HTTPS://GOLPE.EXAMPLE/x", "golpe.example"),
     "esquema e host em maiusculas: href intacto, selo normalizado"),
    ("H40", "A", {"href": "\tjavascript:alert(1)"}, {}, "javascript: com tab na frente perde o href"),
    ("H41", "A", {"href": "java\nscript:alert(1)"}, {}, "javascript: com quebra de linha no meio perde o href"),
    ("H42", "A", {"href": "//" + HOST_CRM + DLX}, {"href": DLX},
     "protocol-relative para o proprio CRM e download: href canonico"),
    ("H43", "A", {"href": DLX + "\n"}, {"href": DLX}, "quebra de linha no fim do href de download: href canonico"),
    # Formato do gerador: <base 1-64>_<6 hex minusculo>.<xlsx|pdf>; o resto NAO e download.
    ("H44", "A", {"href": DL + "x.html"}, _externo(DL + "x.html", HOST_CRM), "extensao .html NAO e download"),
    ("H45", "A", {"href": DL + "relatorio_abc123.exe"}, _externo(DL + "relatorio_abc123.exe", HOST_CRM),
     "extensao .exe NAO e download"),
    ("H46", "A", {"href": DL + "relatorio_ABC123.xlsx"}, _externo(DL + "relatorio_ABC123.xlsx", HOST_CRM),
     "hex em maiusculo NAO e download"),
    ("H47", "A", {"href": DL + "a" * 65 + "_abc123.xlsx"}, _externo(DL + "a" * 65 + "_abc123.xlsx", HOST_CRM),
     "base de 65 caracteres NAO e download"),
    ("H48", "A", {"href": DL + "a" * 64 + "_abc123.xlsx"}, {"href": DL + "a" * 64 + "_abc123.xlsx"},
     "base de 64 caracteres (limite do gerador) e download"),
    ("H49", "A", {"href": DL + "documento_0a1b2c.pdf"}, {"href": DL + "documento_0a1b2c.pdf"},
     "download pdf com hex minusculo e download"),
]

# Casos em que o hook deve deixar o elemento como esta (esperado == entrada), tirados da propria
# tabela. Neles um hook no-op passa por definicao.
INTACTOS = {cid for cid, _t, entrada, esperado, _d in CASOS_HOOK if entrada == esperado}

# `fn.call(DOMPurify, el, null, {})` e a mesma chamada que o DOMPurify 3.0.9 faz (this = DOMPurify;
# argumentos: no, dados, config). O no-op roda sobre copias novas de cada caso (controle de mutacao).
_CORPO_HOOK_JS = """
function __novoEl(tag, attrs) {
  return {
    tagName: tag,
    _a: new Map(Object.entries(attrs)),
    getAttribute(n) { return this._a.has(n) ? this._a.get(n) : null; },
    setAttribute(n, v) { this._a.set(n, String(v)); },
    removeAttribute(n) { this._a.delete(n); },
    hasAttribute(n) { return this._a.has(n); },
  };
}
function __aplica(fn, c) {
  const el = __novoEl(c.tag, c.attrs);
  fn.call(globalThis.DOMPurify, el, null, {});
  return Object.fromEntries(el._a);
}
const __res = { registrados: __registrados.map((r) => r.nome), reais: {}, erros: {}, noop: {} };
for (const c of __casos) {
  try {
    __res.reais[c.id] = __aplica(__registrados[0].fn, c);
  } catch (e) {
    __res.erros[c.id] = String(e);
  }
  __res.noop[c.id] = __aplica(() => {}, c);
}
__out(JSON.stringify(__res));
"""

if funcao_hook is None or registro is None:
    check(False, "secao 2 nao executavel: hook afterSanitizeAttributes nao registrado por nome em "
                 "templates/ai.html (ou sem corpo extraivel)", f"nenhum dos {len(CASOS_HOOK)} casos rodou")
elif node is not None:  # node ausente ja foi reportado, uma vez, acima
    entradas = [{"id": cid, "tag": tag, "attrs": attrs} for cid, tag, attrs, _e, _d in CASOS_HOOK]
    script = "\n".join([
        _PRELUDIO_JS, decl_const or "", funcao_hook,
        # O stub captura o que o template REGISTRA de fato: o registro extraido e avaliado aqui,
        # e o hook testado e o que chegou no addHook.
        "const __registrados = [];",
        "globalThis.DOMPurify = { addHook(nome, fn) { __registrados.push({ nome, fn }); } };",
        registro, "const __casos = " + json.dumps(entradas) + ";", _CORPO_HOOK_JS,
    ])
    dados = _roda_node(script, "secao 2")
    if dados is not None:
        # Tudo que vem do node e lido com .get: chave ausente vira FAIL, nunca KeyError.
        registrados = dados.get("registrados")
        reais, erros, noop = (_dict_ou_vazio(dados.get(k)) for k in ("reais", "erros", "noop"))
        check(registrados == ["afterSanitizeAttributes"],
              "o registro avaliado chama addHook exatamente 1 vez, com 'afterSanitizeAttributes'",
              f"registrou: {registrados!a}")
        for cid, tag, entrada, esperado, desc in CASOS_HOOK:
            if erros.get(cid) is not None:
                check(False, f"{cid} {desc}", f"o hook lancou excecao: {erros.get(cid)!a}")
                continue
            obtido = _caso(reais, cid, desc)
            if obtido is not None:
                check(obtido == esperado, f"{cid} {desc}",
                      f"entrada={tag} {entrada!a} obtido={obtido!a} esperado={esperado!a}")
        # CONTROLE DE MUTACAO: o MESMO harness, com um hook que nao faz nada, tem de devolver a
        # entrada intacta; so assim todo caso fora de INTACTOS (esperado != entrada) e reprovado
        # sem hook. Um harness que alterasse a entrada tornaria o verde dos casos sem sentido.
        infieis = [cid for cid, _t, entrada, _e, _d in CASOS_HOOK if noop.get(cid) != entrada]
        check(not infieis, "controle de mutacao: o harness com um hook no-op devolve a entrada intacta, entao "
                           f"nenhum dos {len(CASOS_HOOK) - len(INTACTOS)} casos fora de INTACTOS passa sem hook",
              f"casos em que o harness alterou ou perdeu a entrada: {infieis}")

# ---- 3. Handler de clique (o handler real, sob node) ---------------------------------------
print("\n3) handler de clique de download (o handler REAL, sob node)")

# (id, href, intercepta, motivo). Intercepta so o download canonico do CRM; o resto segue o
# comportamento normal do navegador (sem preventDefault, sem fetch, sem navegar, sem alert).
CLIQUES = [
    ("C1", DL1, True, "download legitimo"),
    ("C2", DL + "relatorio_abc123.pdf", True, "download legitimo (pdf)"),
    ("C3", DL + "../../api/leads/export.csv", False, "traversal"),
    ("C4", DL + "sub/x_abc123.xlsx", False, "subdiretorio no nome"),
    ("C5", "https://golpe.example" + DLX, False, "host alheio imitando o caminho"),
    ("C6", "/ai/download/x_abc123.xlsx", False, "caminho sem o prefixo /api"),
    ("C7", DL + "x_abc123", False, "nome sem extensao"),
    ("C8", DL + "a.b.xlsx", False, "ponto extra no nome"),
    ("C9", "https://drive.google.com/x", False, "link externo comum"),
    ("C10", DLX + "?x=1", False, "query na URL"),
    # Barra invertida DE VERDADE no href: o navegador a le como `/` em URL http(s), entao `..\..\x` e
    # traversal. Vai para o JS por json.dumps, que a escapa, e o JS a le como 1 barra.
    ("C11", DL + ".." + BS + ".." + BS + "x_abc123.xlsx", False, "traversal com barra invertida"),
    ("C12", DL + "%2e%2e%2fx_abc123.xlsx", False, "traversal percent-encoded"),
    ("C13", DL + "..%2fx_abc123.xlsx", False, "traversal com ..%2f"),
    ("C14", DLX + "\n", False, "quebra de linha no fim do href"),
    ("C15", DL + "x.html", False, "extensao .html"),
    ("C16", DL + "relatorio_abc123.exe", False, "extensao .exe"),
    ("C17", DL + "a" * 64 + "_abc123.xlsx", True, "base de 64 caracteres (limite do gerador)"),
    ("C18", DL + "documento_0a1b2c.pdf", True, "download legitimo (pdf, hex minusculo)"),
]

_CORPO_CLIQUE_JS = """
(async () => {
  const res = {};
  for (const c of __casos) {
    window.location.href = __HREF_INICIAL__;
    __fetched.length = 0;
    __alertas.length = 0;
    let prevented = false;
    const link = { getAttribute: (n) => (n === 'href' ? c.href : null), style: {} };
    const ev = {
      target: { closest: (s) => (s === 'a' ? link : null) },
      preventDefault() { prevented = true; },
      stopPropagation() {},
    };
    try {
      await __handler(ev);
      res[c.id] = { prevented, fetched: __fetched.slice(), navegou: window.location.href,
                    alertas: __alertas.slice() };
    } catch (e) {
      res[c.id] = { erro: String(e) };
    }
  }
  __out(JSON.stringify(res));
})();
""".replace("__HREF_INICIAL__", json.dumps(HREF_INICIAL))

listener = _extrai_listener_click(js)
check(listener is not None, "handler chatHistory.addEventListener('click', ...) extraivel de templates/ai.html")

if listener is not None and node is not None:
    script = "\n".join([
        _PRELUDIO_JS, decl_const or "",
        "let __handler = null;",
        "const chatHistory = { addEventListener(tipo, fn) { if (tipo === 'click') __handler = fn; } };",
        "const __fetched = [];",
        "const __alertas = [];",
        "globalThis.fetch = async (u) => { __fetched.push(u); "
        "return { ok: true, json: async () => ({}), text: async () => '' }; };",
        "globalThis.alert = (m) => { __alertas.push(m); };",
        listener, "const __casos = " + json.dumps([{"id": cid, "href": href} for cid, href, _i, _m in CLIQUES]) + ";",
        _CORPO_CLIQUE_JS,
    ])
    cliques = _roda_node(script, "secao 3")
    if cliques is not None:
        # .get em tudo que vem do node (chave ausente = FAIL). O fetch do stub responde ok, entao
        # qualquer alert, em qualquer caso, e defeito.
        for cid, href, intercepta, motivo in CLIQUES:
            r = _caso(cliques, cid, f"{motivo} {href!a}")
            if r is None:
                continue
            navegou = r.get("navegou")
            if intercepta:
                ok = ("erro" not in r and r.get("prevented") is True and r.get("fetched") == [href]
                      and isinstance(navegou, str) and navegou.startswith(href + "?t=")
                      and r.get("alertas") == [])
                msg = f"{cid} {motivo} {href!a}: interceptado, fetch do caminho e navegacao para ele com ?t=, sem alert"
            else:
                ok = ("erro" not in r and r.get("prevented") is False and r.get("fetched") == []
                      and navegou == HREF_INICIAL and r.get("alertas") == [])
                msg = (f"{cid} {motivo} {href!a}: NAO e interceptado "
                       "(sem preventDefault, sem fetch, sem navegar, sem alert)")
            check(ok, msg, f"obtido={r!a}")

print()
if falhas:
    print(f"{len(falhas)} FALHA(S)")
    sys.exit(1)
print("OK: links do chat da IA - destino externo visivel, download so do CRM (SEC-LINK-01)")
