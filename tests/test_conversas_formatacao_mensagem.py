"""
AUDIT-2026-08-WF — formatacao da mensagem no inbox.

Bugs de operacao que este arquivo trava:

  1. "mensagem rapida sai desformatada" / "quebra de linha some":
     `appendMessageElement` injeta o conteudo via `innerHTML` e `.message-content`
     nao declarava `white-space`. O default do CSS (`normal`) COLAPSA `\\n` e
     sequencias de espaco, entao um orcamento escrito em paragrafos chegava ao
     operador como um bloco unico — enquanto no WhatsApp do cliente aparecia
     certo. O texto nunca esteve perdido: `content` e `Text` no banco e o JS so
     faz `.trim()`. O que faltava era o CSS.

  2. "negrito/italico nao aparecem": nao havia nenhuma renderizacao da
     marcacao do WhatsApp (`*x*`, `_x_`, `~x~`, ```bloco```). O operador via os
     asteriscos crus e nao conseguia conferir o que o cliente ia receber.

  3. "copiar uma mensagem enviada destroi a formatacao": nao existia botao de
     copiar/reaproveitar em lugar nenhum. Selecionar a bolha e copiar traz o
     texto RENDERIZADO — sem os marcadores, com os espacos normalizados pelo
     navegador. Agora ha um botao que devolve ao composer o CORPO ARMAZENADO.

Estes tres sao de FRONTEND puro; nao ha rota para exercitar. As asercoes de
comportamento rodam a funcao de marcacao de verdade no Node (nao e grep: o
codigo e extraido do arquivo e EXECUTADO), e o restante sao guards estaticos
sobre CSS/JS que morrem se alguem remover a correcao.

Roda standalone:  python tests/test_conversas_formatacao_mensagem.py
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONVERSAS_DIR = ROOT / "conversas"
JS = (CONVERSAS_DIR / "static" / "js" / "conversas.js").read_text(encoding="utf-8")
CSS = (CONVERSAS_DIR / "static" / "css" / "conversas.css").read_text(encoding="utf-8")

failures = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        failures.append(msg)


# ============ 1. quebras de linha preservadas (CSS) ============
print("1 — quebra de linha na bolha")

bloco_ini = CSS.find(".message-content {")
check(bloco_ini >= 0, ".message-content existe no CSS")
bloco = CSS[bloco_ini:CSS.find("}", bloco_ini)] if bloco_ini >= 0 else ""

check("white-space: pre-wrap" in bloco,
      "`.message-content` declara white-space: pre-wrap (o \\n vira quebra visivel)")
check("overflow-wrap" in bloco,
      "palavra longa/URL nao estoura a bolha (overflow-wrap)")


# ============ 2. marcacao do WhatsApp — EXECUTADA, nao grepada ============
print("\n2 — marcacao do WhatsApp (executando a funcao real)")

node = shutil.which("node")
if node is None:
    # Sem node nao ha como EXECUTAR. Falhar e melhor que fingir cobertura:
    # ROOT-017 desta auditoria e exatamente "teste que so faz grep afirmando
    # comportamento". Um skip silencioso reproduziria o mesmo defeito.
    check(False, "node disponivel para executar renderWhatsappMarkup (sem ele nao ha prova)")
else:
    ini = JS.find("const WA_MARKUP = [")
    fim = JS.find("window._renderWhatsappMarkup")
    check(ini >= 0 and fim > ini, "renderWhatsappMarkup encontrado no conversas.js")

    if ini >= 0 and fim > ini:
        trecho = "\n".join(l[4:] if l.startswith("    ") else l
                           for l in JS[ini:fim].splitlines())
        casos = [
            # (entrada JA ESCAPADA, saida esperada, descricao)
            ["ola *mundo* fim", "ola <b>mundo</b> fim", "negrito"],
            ["_italico_", "<i>italico</i>", "italico"],
            ["~riscado~ ok", "<s>riscado</s> ok", "riscado"],
            ["```bloco *nao* muda```", "<code>bloco *nao* muda</code>",
             "bloco mono nao recebe outras marcacoes"],
            ["http://a.com/x_y_z", "http://a.com/x_y_z",
             "underscore dentro de URL NAO vira italico"],
            ["2*3*4", "2*3*4", "asterisco colado a numero nao e marcacao"],
            ["linha1\nlinha2", "linha1\nlinha2", "quebra preservada (quem quebra e o CSS)"],
            ["*a* e *b*", "<b>a</b> e <b>b</b>", "dois negritos na mesma linha"],
            ["&lt;script&gt;alert(1)&lt;/script&gt; *x*",
             "&lt;script&gt;alert(1)&lt;/script&gt; <b>x</b>",
             "conteudo JA ESCAPADO continua escapado (a marcacao nao reabre XSS)"],
            ["*sem fechar", "*sem fechar", "marcador sem par fica literal"],
        ]
        harness = (
            trecho
            + "\nconst casos = " + json.dumps(casos, ensure_ascii=False) + ";\n"
            + "const out = casos.map(([i, e, d]) => "
              "[d, renderWhatsappMarkup(i) === e, renderWhatsappMarkup(i), e]);\n"
            + "console.log(JSON.stringify(out));\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(harness)
            script = fh.name
        try:
            proc = subprocess.run([node, script], capture_output=True, text=True,
                                  encoding="utf-8", timeout=60)
            check(proc.returncode == 0,
                  f"harness node executa (rc={proc.returncode}) {proc.stderr[-200:]}")
            if proc.returncode == 0:
                for desc, ok, got, esperado in json.loads(proc.stdout.strip().splitlines()[-1]):
                    check(ok, f"{desc} (obtido {got!r}, esperado {esperado!r})")
        finally:
            pathlib.Path(script).unlink(missing_ok=True)

check("renderWhatsappMarkup(escapeHtml(msg.content))" in JS,
      "escapa PRIMEIRO e formata depois — nunca o contrario")
check("escapeHtml(renderWhatsappMarkup" not in JS,
      "nao existe o caminho invertido (formatar e depois escapar apagaria as tags)")


# ============ 3. reaproveitar mensagem enviada ============
print("\n3 — reaproveitar mensagem enviada")

check("msg-reuse-btn" in JS, "botao de reaproveitar existe no render da bolha")
check("window._reuseMessage" in JS, "handler exposto para o onclick")
check(".msg-reuse-btn" in CSS, "botao tem estilo (nao fica invisivel)")

ini_r = JS.find("function reuseMessage(")
fim_r = JS.find("window._reuseMessage")
corpo = JS[ini_r:fim_r] if ini_r >= 0 else ""
check(ini_r >= 0, "funcao reuseMessage definida")
check("activeConversation" in corpo and "messages" in corpo,
      "le a mensagem do OBJETO em memoria")
check("innerText" not in corpo and "textContent" not in corpo,
      "NAO le do DOM — innerText ja perdeu marcadores e espacos")
check("input.value = msg.content" in corpo,
      "devolve o corpo armazenado byte a byte ao composer")
check("sendMessage" not in corpo,
      "reaproveitar NAO envia: o operador edita e envia quando quiser")

# O botao so faz sentido em outbound e nunca num placeholder de midia.
bloco_out = JS[JS.find("if (msg.direction === 'outbound') {"):]
bloco_out = bloco_out[:bloco_out.find("let content =")]
check("msg-reuse-btn" in bloco_out,
      "botao so e montado dentro do ramo outbound")
check("[A-Z]+" in bloco_out,
      "placeholder de midia ([IMAGE]/[AUDIO]/...) nao ganha botao de reaproveitar")


# ============ 4. quick reply continua inserindo sem enviar ============
print("\n4 — mensagem rapida (regressao do caminho existente)")

ini_q = JS.find("function selectQuickReply(")
corpo_q = JS[ini_q:JS.find("}", JS.find("setSelectionRange", ini_q))] if ini_q >= 0 else ""
check(ini_q >= 0, "selectQuickReply existe")
check("input.value = r.content" in corpo_q,
      "insere o conteudo cru no textarea (o \\n sobrevive: textarea e pre)")
check("sendMessage" not in corpo_q,
      "selecionar mensagem rapida NAO envia")


print()
if failures:
    print(f"FALHOU ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("TODOS OS TESTES DE FORMATACAO DE MENSAGEM PASSARAM")
