# -*- coding: utf-8 -*-
"""
SEC-XSS-06 - nenhum valor cru chega a um sink de HTML no front do Conversas.

O SEC-XSS-05 transformou em regra a varredura manual do SEC-XSS-04, mas so no
front do CRM (tests/test_frontend_interpolacao_crua.py). O Conversas tem front
proprio - lista de conversas, chips de tag, bolhas de mensagem, telas de
configuracao - e texto de terceiros chega la por innerHTML: o nome de perfil do
contato do WhatsApp, escolhido por ele, entra pelo webhook da Meta. A primeira
varredura achou quatro interpolacoes cruas: as iniciais do contato, a hora de
abertura e a de fechamento do expediente e o tipo de variavel. Esta regra impede
a proxima.

A regra: todo valor que chega a um sink de HTML (innerHTML / outerHTML /
insertAdjacentHTML / document.write) e escapeHtml() cobrindo o valor inteiro -
ou DOMPurify.sanitize() so na raiz do sink -, numero, constante do codigo ou
uma entrada de PERMITIDAS (abaixo), que fixa o motivo e o numero de ocorrencias.
Quem julga isso e o analisador de tests/analisador_interpolacao_crua.py, e o
docstring dele e o contrato: o que conta como valor seguro, as extensoes e o que
a regra NAO pega.

Escopo: conversas/templates/**/*.html (blocos <script> inline, sem src=) e
conversas/static/js/**/*.js. O front do CRM tem o teste dele.

Rodar:  python tests/test_conversas_interpolacao_crua.py
"""
import pathlib
import re
import sys

# `python tests/<arquivo>.py` poe tests/ em sys.path[0]: o analisador irmao importa direto.
from analisador_interpolacao_crua import (
    RE_OP_DE_ESCRITA, Analise, analisa, arquivos_do_front, confere_permitidas, fora_da_regra,
    ids_texto_declarados, varre)

ROOT = pathlib.Path(__file__).resolve().parent.parent
# O nome CONVERSAS_DIR e o discriminador de job do CI (.github/workflows/test.yml): o
# arquivo que o contem roda no job do Conversas (Python 3.12). Nao renomear.
CONVERSAS_DIR = ROOT / "conversas"

falhas = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        falhas.append(msg)


# Piso do scan (hoje sao 9 arquivos): glob quebrado = zero achados e regra verde e cega.
MINIMO_DE_ARQUIVOS = 8

CONVERSAS_JS = "conversas/static/js/conversas.js"
SETTINGS_JS = "conversas/static/js/settings.js"
TEMPLATES_JS = "conversas/static/js/templates.js"


# ---------------------------------------------------------------------------
# Allowlist: (arquivo, expressao normalizada) -> (ocorrencias, motivo).
# Cada entrada aceita EXATAMENTE `ocorrencias` achados crus com aquela chave:
# uma interpolacao a mais (ou a menos) com a mesma expressao muda a contagem e
# reprova. A contagem NAO ve o que chega ao nome: uma fonte nova escrita em
# `content`, em `monos` ou em `originalText`, ou um safeTagColor mais fraco,
# deixam a contagem igual - quem reprova isso sao os pinos de procedencia da
# secao 4. O motivo diz de onde o valor vem e por que ele nao carrega markup.
# Numero NAO vai para escapeHtml() (a copia de templates.js devolve '' para 0 e
# apaga o zero da tela) e valor JA escapado tambem nao (escapar de novo duplica
# entidades): os dois ficam aqui, com a procedencia.
# ---------------------------------------------------------------------------
PERMITIDAS = {
    # -- cor de tag: safeTagColor revalida no cliente (defesa em profundidade); a
    #    contagem 5 obriga a reauditar qualquer uso novo da cor em style=
    (CONVERSAS_JS, "safeTagColor(t.cor)"): (
        5, "safeTagColor (conversas.js) devolve o proprio valor so se casar regex ancorada de "
           "#hex de 6 digitos, senao o literal #999999; t.cor vem de TagResponse.cor (/api/tags "
           "e tags da conversa); sem aspas, ponto e virgula nem markup"),
    # -- texto JA escapado por escapeHtml; renderWhatsappMarkup so acrescenta b, i, s e code
    (CONVERSAS_JS, "monos[Number(i)]"): (
        1, "monos guarda so trechos de bloco de tres crases do argumento de "
           "renderWhatsappMarkup, que por contrato chega escapado por escapeHtml (unico "
           "chamador de producao: renderWhatsappMarkup(escapeHtml(msg.content))); indice "
           "numerico do placeholder; escapar de novo duplicaria entidades"),
    (CONVERSAS_JS, "content"): (
        5, "content nasce de renderWhatsappMarkup(escapeHtml(msg.content)) (escapa primeiro; "
           "so insere b, i, s e code sem atributos) e os demais ramos de appendMessageElement "
           "so concatenam markup estatico, Number(id), escapeHtml(media_url ou filename) e o "
           "proprio content (direto ou via captionHtml); escapar de novo duplicaria entidades"),
    # -- numero do servidor que a regra numerica nao reconhece pelo nome
    (SETTINGS_JS, "h.weekday"): (
        1, "weekday e int 0 a 6: BusinessHoursResponse.weekday: int (GET "
           "/api/settings/business-hours valida por model_validate), coluna Integer e escrita "
           "com Field(ge=0, le=6); numero no JSON, nao carrega markup"),
    # -- innerHTML de botao estatico, salvo antes do spinner e restaurado no finally
    (SETTINGS_JS, "originalText"): (
        1, "originalText = btn.innerHTML de #btnTestConnection, botao estatico de "
           "settings.html (svg + texto Testar Conexao); so este metodo escreve nele (spinner "
           "literal) e o restaura; nenhum dado entra e escapar quebraria o svg"),
    (TEMPLATES_JS, "originalText"): (
        1, "originalText = btn.innerHTML de #btnSyncTemplates, botao estatico de "
           "templates.html (svg + texto Sincronizar com Meta); so este metodo escreve nele "
           "(spinner literal) e o restaura; nenhum dado entra e escapar quebraria o svg"),
}


# ---------------------------------------------------------------------------
# 1. A REGRA no front do Conversas
# ---------------------------------------------------------------------------
print("1) todo valor que chega a um sink de HTML no Conversas e escapeHtml(), numero, constante ou allowlist")

arquivos = arquivos_do_front(CONVERSAS_DIR)
# raiz = ROOT, nao CONVERSAS_DIR: o `rel` dos achados comeca com `conversas/`, como as chaves acima.
achados, dessincronizados = varre(ROOT, arquivos)
check(len(arquivos) >= MINIMO_DE_ARQUIVOS, f"arquivos de front analisados: {len(arquivos)}")
check(not dessincronizados, f"lexer em sincronia com a fonte real: {dessincronizados!a}")
problemas = confere_permitidas(achados, PERMITIDAS)
for linha in problemas:
    print(f"    {linha!a}")
check(not problemas, f"achados crus x allowlist: {len(problemas)} problema(s)")

# ---------------------------------------------------------------------------
# 2. Controle de mutacao no arquivo REAL: reverter um escapeHtml() auditado tem que acusar
# ---------------------------------------------------------------------------
print("\n2) reverter cada correcao (SEC-XSS-06) no arquivo real acusa o campo")

HORA_ABRE = "h.open_time || '09:00'"
HORA_FECHA = "h.close_time || '18:00'"
MUTACOES = [
    # getInitials tambem alimenta dois textContent (chatAvatar, leadAvatar), onde escapar
    # mostraria `&lt;`: o escape fica na interpolacao da lista, nao dentro dela.
    (CONVERSAS_JS, "${escapeHtml(initials)}", "${initials}", "initials"),
    # o escape cobre o `||` inteiro: o fallback tambem e impresso
    (SETTINGS_JS, "${escapeHtml(" + HORA_ABRE + ")}", "${" + HORA_ABRE + "}", HORA_ABRE),
    (SETTINGS_JS, "${escapeHtml(" + HORA_FECHA + ")}", "${" + HORA_FECHA + "}", HORA_FECHA),
    (SETTINGS_JS, "var-kind-badge ${escapeHtml(v.kind)}", "var-kind-badge ${v.kind}", "v.kind"),
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
          f"{rel}: sem o escapeHtml() o analisador acusa {esperado!a}")

# ---------------------------------------------------------------------------
# 3. IDS_TEXTO x conversas/app/models e schemas (SEC-XSS-06): `*_id` texto novo reprova
# ---------------------------------------------------------------------------
print("\n3) ids texto do Conversas conferidos contra conversas/app/models e conversas/app/schemas")

# Os ids da Meta/WhatsApp sao String/str: a regra numerica pelo nome nao vale para eles.
COLUNAS_TEXTO = {"meta_phone_number_id", "meta_waba_id", "whatsapp_msg_id", "meta_media_id",
                 "meta_template_id"}
CAMPOS_TEXTO = COLUNAS_TEXTO | {"waba_id"}
colunas, campos = ids_texto_declarados(CONVERSAS_DIR / "app")
check(COLUNAS_TEXTO <= colunas, f"o scan acha as colunas String *_id dos models ({sorted(colunas)})")
check(CAMPOS_TEXTO <= campos, f"o scan acha os campos str *_id dos schemas ({sorted(campos)})")
check(not fora_da_regra(colunas | campos),
      f"todo *_id texto de models e schemas esta em IDS_TEXTO ou e id de etapa "
      f"(fora: {fora_da_regra(colunas | campos)})")
obtido = [(a.expr, a.culpado)
          for a in analisa("sintetico.js", "el.innerHTML = `<i>${m.whatsapp_msg_id}</i>`;")]
check(obtido == [("m.whatsapp_msg_id", "m.whatsapp_msg_id")],
      f"acusa o id da Meta cru, que e texto: {obtido!a}")

# ---------------------------------------------------------------------------
# 4. Procedencia auditada da allowlist (SEC-XSS-06): a contagem confia num NOME
# ---------------------------------------------------------------------------
print("\n4) a procedencia que a allowlist auditou continua a mesma")

# Os pinos leem o CODIGO (Analise.m: comentario, texto de string e de template e corpo de
# regex viram espaco): mudar o markup estatico nao reprova; mudar o que flui para `content`,
# para `monos` ou para `originalText`, ou o gate de safeTagColor, reprova ate a entrada e o
# pino serem reauditados juntos.


def analise(rel):
    return Analise(rel, (ROOT / rel).read_text(encoding="utf-8"))


def codigo(A, a, b):
    return " ".join(A.m[a:b].split())


def confere(obtido, auditado, msg):
    check(obtido == auditado, msg if obtido == auditado else f"{msg} (obtido: {obtido!a})")


CJ = analise(CONVERSAS_JS)
check(len(re.findall(r"renderWhatsappMarkup\s*\(", CJ.m)) == 2 and len(re.findall(
    r"renderWhatsappMarkup\s*\(\s*escapeHtml\s*\(\s*msg\s*\.\s*content\s*\)\s*\)", CJ.m)) == 1,
      "renderWhatsappMarkup: a definicao e uma chamada so, renderWhatsappMarkup(escapeHtml(msg.content))")
# renderWhatsappMarkup so poe tags constantes, sem atributo, em volta do texto ja escapado, e
# `monos` so guarda trecho dele (a entrada monos[Number(i)]).
RENDER_AUDITADO = (
    "{ if (!escaped) return escaped; const monos = []; let out = String(escaped).replace(/ /g, "
    "(_, corpo) => { monos.push(corpo); return ` monos.length - 1 `; }); for (const [re, tag] of "
    "WA_MARKUP) { const close = tag.replace(' ', ' '); out = out.replace(re, (_, pre, corpo) => "
    "` pre tag corpo close `); } return out.replace(/ /g, (_, i) => ` monos[Number(i)] `); }")
confere([codigo(CJ, *F.corpo) for F in CJ.funcoes_por_nome.get("renderWhatsappMarkup", [])],
        [RENDER_AUDITADO], "o codigo de renderWhatsappMarkup e o auditado")
# Cada escrita em `content` dentro de appendMessageElement: o operador e o lado direito (a
# entrada content x5; escrita por desestruturacao nao e vista - ver Limites do analisador).
ESCRITAS_AUDITADAS = [
    "= renderWhatsappMarkup(escapeHtml(msg.content))",
    "= ` Number(msg.media_asset.id) captionHtml `",
    "= ` escapeHtml(msg.media_url) content `",
    "= ` content `",
    "= ` Number(msg.media_asset.id) ` + (msg.content && msg.content !== ' ' ? ` content ` : '')",
    "= ' '",
    "= ` fn fn Number(msg.media_asset.id) captionHtml `",
    "= ' '",
    "= ` Number(msg.media_asset.id) captionHtml `",
    "= ' '",
    "+= ` Number(msg.media_asset.id) `",
]
RE_ESCRITA_DE_CONTENT = re.compile(r"(?<![\w$.])content\s*(" + RE_OP_DE_ESCRITA.pattern + ")")
confere([mm.group(1) + " " + codigo(CJ, mm.end(), CJ.fim_expr(mm.end()))
         for F in CJ.funcoes_por_nome.get("appendMessageElement", [])
         for mm in RE_ESCRITA_DE_CONTENT.finditer(CJ.m, *F.corpo)],
        ESCRITAS_AUDITADAS, f"as escritas em content sao as {len(ESCRITAS_AUDITADAS)} auditadas")
# safeTagColor: a regex ancorada de #hex6 e o literal de fallback (a entrada safeTagColor x5)
confere([CJ.norm(*F.corpo) for F in CJ.funcoes_por_nome.get("safeTagColor", [])],
        ["{ return /^#[0-9A-Fa-f]{6}$/.test(cor || '') ? cor : '#999999'; }"],
        "safeTagColor e o gate auditado")
# originalText: guarda btn.innerHTML antes do spinner e so volta para ele (entradas originalText)
for rel in (SETTINGS_JS, TEMPLATES_JS):
    m = analise(rel).m
    check(len(re.findall(r"(?<![\w$.])originalText(?![\w$])", m)) == 2
          and len(re.findall(r"\bconst\s+originalText\s*=\s*btn\s*\.\s*innerHTML\s*;", m)) == 1
          and len(re.findall(r"\bbtn\s*\.\s*innerHTML\s*=\s*originalText\s*;", m)) == 1,
          f"{rel}: originalText e escrito uma vez (btn.innerHTML) e lido so na restauracao")


print()
if falhas:
    print(f"{len(falhas)} FALHA(S)")
    sys.exit(1)
print("OK: nenhum valor cru em sink de HTML do Conversas fora da allowlist")
