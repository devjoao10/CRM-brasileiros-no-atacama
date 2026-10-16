# -*- coding: utf-8 -*-
"""
SEC-XSS-05 - nenhum `${...}` cru em template literal que vira markup (CRM).

O SEC-XSS-04 achou quatro campos de lead crus em innerHTML so por varredura
MANUAL: email e whatsapp em segmentacao.html renderDetailTable, t.cor em
renderTagChips e o value="${savedVal}" de renderDiasDestino no editor de lead.
Nos quatro a linha em volta ja passava os outros campos por esc(), entao o
bloco inteiro PARECIA escapado. Este arquivo transforma a varredura em regra:
o quinto caso nao pode depender de alguem ler a tela certa.

A regra: todo valor que chega a um sink de HTML (innerHTML / outerHTML /
insertAdjacentHTML / document.write) precisa ser

  (a) uma chamada de esc()/escapeHtml() - ou de DOMPurify.sanitize() -
      cobrindo o valor inteiro,
  (b) numero ou campo numerico,
  (c) constante do codigo, ou
  (d) uma entrada de PERMITIDAS, que fixa o motivo e o numero de ocorrencias.

Vale para cada `${...}` de template literal que vira markup e para o proprio
valor do sink fora de template: concatenacao (`'<td>' + x`), variavel montada
com `+=`, `[...].join('')`. Comentario `//` e `/* */` nao conta.

Por que isto nao e um grep:

  * "Vira markup" quase nunca esta na linha do `${`. O template passa por
    variavel local (`let html = ''; html += ...`), por helper do arquivo
    (formatDate, getDestinosHtml), por `.map(...).join('')`. Um template SEM
    tag nenhuma (`const label = `${l.nome}``) vira markup quando chega a um
    innerHTML. Por isso o analisador resolve identificador e funcao do mesmo
    arquivo e propaga "e markup" ate um ponto fixo.
  * Escopo importa. segmentacao.html tem DUAS variaveis `pills`: a de
    renderFilterPills recebe texto CRU e escapa tudo no fim; a outra guarda o
    retorno dela. Unir por nome no arquivo daria falso positivo.
  * Lexar exige regex literal: toda copia de esc() tem /'/g e /"/g. Um lexer
    que trate a barra como divisao perde o pareamento de aspas, o resto do
    arquivo vira lixo e a regra passa a nao enxergar nada - verde e cega.
  * esc(0) devolve '' nas copias que comecam com `if (!str) return ''`.
    Embrulhar numero em esc() apaga o zero da tela: numero e aceito pela regra
    numerica ou vai para a allowlist, nunca para esc().

Escopo: templates/**/*.html (blocos <script> inline, sem src=) e
static/js/*.js. O app de conversas fica fora: e follow-up.

Rodar:  python tests/test_frontend_interpolacao_crua.py
"""
import pathlib
import re
import sys
from collections import namedtuple

ROOT = pathlib.Path(__file__).resolve().parent.parent

falhas = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        falhas.append(msg)


Achado = namedtuple("Achado", "rel linha expr culpado")

# Colunas `*_id` declaradas String em app/models: texto, nao PK/FK inteira. A
# regra numerica pelo nome nao vale para elas (a secao 5 confere contra os
# models: coluna String `*_id` nova reprova ate entrar aqui).
IDS_TEXTO = frozenset({"etapa_id", "google_calendar_event_id"})


# ===========================================================================
# Analisador (Python puro, so stdlib)
# ===========================================================================

# -- 1. Fontes: o .js inteiro; no .html, cada <script> inline (sem src=) ------
RE_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script\s*>", re.S | re.I)


def regioes_de_script(rel, texto):
    if rel.endswith(".js"):
        return [(0, len(texto))]
    return [(m.start(2), m.end(2)) for m in RE_SCRIPT.finditer(texto)
            if not re.search(r"\bsrc\s*=", m.group(1), re.I)]


# -- 2. Lexer minimo, um passe ------------------------------------------------
RE_IDENT = re.compile(r"(?:[^\W\d]|\$)[\w$]*")
RE_NUM = re.compile(r"\d[\w.]*|\.\d\w*")
# Uma `/` abre regex no inicio, depois desta pontuacao ou destas palavras; no
# resto (identificador, numero, `)`, `]`) e divisao.
PONTUACAO_PRE_REGEX = frozenset("(,=:[!&|?{};+-*%<>~^")
PALAVRAS_PRE_REGEX = frozenset(
    "return typeof case do else in of new delete void throw instanceof yield await".split())


class Template:
    """Template literal: quasis = texto estatico; exprs = (pos do `${`, ini, fim)."""

    def __init__(self, ini, pai):
        self.ini = ini
        self.fim = None
        self.quasis = [""]
        self.exprs = []
        self.pai = pai          # (template, indice da interpolacao) se aninhado
        self.filhos = []


def lexa(t, a, b, masc, atomos, templates):
    """Le t[a:b] como JS e registra os template literals.

    Em `masc` (mesmo comprimento do arquivo) apaga com espaco, preservando as
    quebras de linha: corpo de comentario, conteudo de string, corpo de regex e
    texto estatico de template (com o `${` e o `}` que delimitam a
    interpolacao). Sobra so codigo - as buscas de declaracao, atribuicao,
    return e sink nunca casam dentro de string ou comentario.
    `atomos[ini] = (fim, tipo)` marca string, regex e template inteiros.
    """
    def apaga(x, y):
        for k in range(x, y):
            if t[k] != "\n":
                masc[k] = " "

    pilha = []      # ("tpl", T) | ["expr", T, profundidade_de_chaves, ini, pos_do_$]
    prev = ""       # ultimo token significativo; "" = inicio
    i = a
    while i < b:
        if pilha and pilha[-1][0] == "tpl":
            T = pilha[-1][1]
            c = t[i]
            if c == "\\":
                T.quasis[-1] += t[i:i + 2]
                apaga(i, min(i + 2, b))
                i += 2
            elif c == "`":
                T.fim = i + 1
                atomos[T.ini] = (T.fim, "tpl")
                pilha.pop()
                i += 1
                prev = ")"
            elif c == "$" and t.startswith("{", i + 1):
                apaga(i, i + 2)
                pilha.append(["expr", T, 0, i + 2, i])
                i += 2
                prev = "("
            else:
                T.quasis[-1] += c
                apaga(i, i + 1)
                i += 1
            continue
        c = t[i]
        if c.isspace():
            i += 1
            continue
        if t.startswith("//", i):
            j = t.find("\n", i, b)
            j = b if j < 0 else j
            apaga(i, j)
            i = j
            continue
        if t.startswith("/*", i):
            j = t.find("*/", i + 2, b)
            j = b if j < 0 else j + 2
            apaga(i, j)
            i = j
            continue
        if c in "'\"":
            j = i + 1
            while j < b and t[j] != c and t[j] != "\n":
                j += 2 if t[j] == "\\" else 1
            j = min(j, b)
            apaga(i + 1, j)
            fim = j + 1 if j < b and t[j] == c else j
            atomos[i] = (fim, "str")
            i = fim
            prev = ")"
            continue
        if c == "`":
            pai = None
            if pilha:   # o topo so pode ser "expr": template dentro de ${...}
                pai = (pilha[-1][1], len(pilha[-1][1].exprs))
            T = Template(i, pai)
            if pai:
                pai[0].filhos.append(T)
            templates.append(T)
            pilha.append(("tpl", T))
            i += 1
            continue
        if c == "/" and (prev == "" or prev in PONTUACAO_PRE_REGEX or prev in PALAVRAS_PRE_REGEX):
            j = i + 1
            classe = False      # dentro de [...] a barra nao fecha a regex
            fecha = -1
            while j < b and t[j] != "\n":
                if t[j] == "\\":
                    j += 2
                    continue
                if classe:
                    classe = t[j] != "]"
                elif t[j] == "[":
                    classe = True
                elif t[j] == "/":
                    fecha = j
                    break
                j += 1
            if fecha > 0:
                j = fecha + 1
                while j < b and (t[j].isalnum() or t[j] in "_$"):
                    j += 1
                apaga(i + 1, fecha)
                atomos[i] = (j, "re")
                i = j
                prev = ")"
                continue
        if pilha and c in "{}":   # dentro de ${...}: chave conta profundidade
            fr = pilha[-1]
            if c == "{":
                fr[2] += 1
            elif fr[2]:
                fr[2] -= 1
            else:
                fr[1].exprs.append((fr[4], fr[3], i))
                fr[1].quasis.append("")
                apaga(i, i + 1)
                pilha.pop()
                i += 1
                continue
        m = RE_IDENT.match(t, i)
        if m:
            prev = m.group(0)
            i = m.end()
            continue
        m = RE_NUM.match(t, i)
        if m:
            prev = ")"
            i = m.end()
            continue
        prev = c
        i += 1
    for fr in pilha:    # fonte truncada: template sem fechamento vai ate o fim
        if fr[0] == "tpl" and fr[1].fim is None:
            fr[1].fim = b
            atomos[fr[1].ini] = (b, "tpl")


# -- 3/4. Markup e classificador ----------------------------------------------
Tok = namedtuple("Tok", "tipo ini fim txt")
# tipo: id | num | op | fecha | atom (txt: str, re, tpl) | grp (txt: ( [ {)
#       e, montados pelo classificador: new | prop | call | idx
Funcao = namedtuple("Funcao", "nome ini params corpo bloco declarada")
Lig = namedtuple("Lig", "tipo nome pos s0 s1 init palavra extra")
# Ligacao de um nome valida em [s0, s1): decl (const/let/var), destr
# (desestruturacao), param, forof/forin, catch ou func (declaracao de funcao).

OPERADORES = sorted(
    ">>>= ... === !== **= <<= >>= >>> &&= ||= ??= => == != <= >= && || ?? ?. "
    "++ -- += -= *= /= %= &= |= ^= ** << >>".split(), key=len, reverse=True)
ATRIBUICOES = frozenset("= += -= *= /= %= **= <<= >>= >>>= &= |= ^= &&= ||= ??=".split())
BINARIOS_SEGUROS = frozenset("| ^ & == != === !== < > <= >= << >> >>>".split())
CONTINUA = frozenset("? : . ?. + - * / % ** || && ?? , = == === != !== < > <= >= "
                     "& | ^ << >> >>> => += -= *= /= %= ||= &&= ??=".split())
PALAVRAS_OPERADOR = frozenset(
    "typeof void delete await new in instanceof return case throw of yield".split())

ESCAPES = frozenset({"esc", "escapeHtml"})
LITERAIS = frozenset({"true", "false", "null", "undefined", "NaN", "Infinity"})
GLOBAIS_NUMERICAS = frozenset({"Number", "parseInt", "parseFloat", "Boolean"})
METODOS_NUMERO = frozenset("getTime getFullYear getMonth getDate getDay getHours getMinutes".split())
METODOS_DATA_NUMERO = METODOS_NUMERO | frozenset(
    "toLocaleDateString toLocaleTimeString toISOString toDateString toFixed toPrecision".split())
METODOS_PRESERVAM = frozenset(
    "slice substring substr charAt toUpperCase toLowerCase trim padStart padEnd".split())
RE_NOME_NUMERICO = re.compile(
    r"id|[\w$]+_id|[a-z][\w$]*Id|total|count|[\w$]+_count|num_[\w$]+|total_[\w$]+")
# Alem da spec: o id de etapa e texto livre (StageSchema.id; ver a secao 3 de
# test_frontend_injection_contract.py) em qualquer grafia - stageId, etapaId,
# destino_etapa_id - e nao so como `.id` de `stage`/`etapa`.
RE_ID_DE_ETAPA = re.compile(r"(?:stage|etapa)_?id$", re.I)
# O mesmo id como `.id` do parametro de callback sobre a lista de etapas
# (`funnel.etapas.map(s => ... s.id ...)`), seja qual for o nome do parametro.
RE_CALLBACK_DE_ETAPAS = re.compile(
    r"\b(?:etapas|stages)\s*\.\s*(?:map|forEach|filter|find|some|every)\s*\(\s*$")
RE_TAG = re.compile(r"<[A-Za-z!/]")
RE_SINK_ATRIB = re.compile(r"\.\s*(?:innerHTML|outerHTML)\s*\+?=(?![=>])")
RE_SINK_CHAMADA = re.compile(r"\.\s*(insertAdjacentHTML)\s*\(|\bdocument\s*\.\s*write(?:ln)?\s*\(")
RE_FUNCTION = re.compile(r"\bfunction\b\s*\*?\s*((?:[^\W\d]|\$)[\w$]*)?\s*\(")
RE_NOME_ATRIBUIDO = re.compile(r"\b(?:const|let|var)\s+((?:[^\W\d]|\$)[\w$]*)\s*=\s*(?:async\s+)?$")
RE_DECL = re.compile(r"\b(const|let|var)\b")
RE_FOR = re.compile(r"\bfor\s*(?:await\s*)?(\()\s*$")
RE_CATCH = re.compile(r"\bcatch\s*\(\s*((?:[^\W\d]|\$)[\w$]*)\s*\)")
RE_RETURN = re.compile(r"\breturn\b")
RE_RECEPTOR_DE_CALLBACK = re.compile(
    r"(?<![\w$.])((?:[^\W\d]|\$)[\w$]*)\s*\.\s*(?:map|forEach)\s*\(\s*$")
RE_NOME_DE_PADRAO = re.compile(r"(?<![\w$])((?:[^\W\d]|\$)[\w$]*)(?=\s*(?:[,}\]=]|$))")
RE_OP_DE_ESCRITA = re.compile(r"(?:[-+*/%&|^]|\*\*|<<|>>>?|&&|\|\||\?\?)?=(?![=>])")


class Analise:
    """Um arquivo de front: lexa, indexa funcoes e ligacoes, julga as interpolacoes."""

    def __init__(self, rel, texto):
        self.rel = rel
        self.t = texto
        self.regioes = regioes_de_script(rel, texto)
        masc = [c if c == "\n" else " " for c in texto]
        for a, b in self.regioes:
            masc[a:b] = texto[a:b]
        self.atomos = {}
        self.templates = []
        for a, b in self.regioes:
            lexa(texto, a, b, masc, self.atomos, self.templates)
        self.m = "".join(masc)
        self.tpl_por_ini = {T.ini: T for T in self.templates}
        self.interp = [(x, y) for T in self.templates for _, x, y in T.exprs]
        self.par = {}
        self.bloco = [-1] * len(texto)      # '{' mais interno que contem cada posicao
        for a, b in self.regioes:
            self._pareia(a, b)
        self.funcoes = []
        self.funcao_por_ini = {}
        self.funcoes_por_nome = {}
        self._indexa_funcoes()
        self.ligacoes = {}
        self._indexa_ligacoes()
        self.markup = set()
        self.fila = []
        self.memo = {}
        self.visitando = set()
        self.ciclos = 0
        self._escritos = {}

    # -- infraestrutura -------------------------------------------------------
    def _pareia(self, a, b):
        m, pilha, chaves = self.m, [], []
        abre_de = {")": "(", "]": "[", "}": "{"}
        for i in range(a, b):
            self.bloco[i] = chaves[-1] if chaves else -1
            c = m[i]
            if c in "([{":
                pilha.append(i)
                if c == "{":
                    chaves.append(i)
            elif c in ")]}" and pilha and m[pilha[-1]] == abre_de[c]:
                j = pilha.pop()
                self.par[j], self.par[i] = i, j
                if c == "}":
                    chaves.pop()

    def _ws(self, i, lim):
        while i < lim and self.m[i].isspace():
            i += 1
        return i

    def _tok(self, i):
        m = self.m
        if i in self.atomos:
            fim, tipo = self.atomos[i]
            return Tok("atom", i, fim, tipo)
        c = m[i]
        if c in "([{":
            j = self.par.get(i)
            return Tok("grp", i, j + 1, c) if j is not None else Tok("op", i, i + 1, c)
        if c in ")]}":
            return Tok("fecha", i, i + 1, c)
        if c.isdigit() or (c == "." and m[i + 1:i + 2].isdigit()):
            mm = RE_NUM.match(m, i)
            return Tok("num", i, mm.end(), mm.group(0))
        mm = RE_IDENT.match(m, i)
        if mm:
            return Tok("id", i, mm.end(), mm.group(0))
        for op in OPERADORES:
            if m.startswith(op, i):
                return Tok("op", i, i + len(op), op)
        return Tok("op", i, i + 1, c)

    def tokens(self, a, b):
        """Tokens de topo de m[a:b]; string/regex/template e (...)/[...]/{...} sao um token so."""
        out = []
        i = self._ws(a, b)
        while i < b:
            tk = self._tok(i)
            out.append(tk)
            i = self._ws(tk.fim, b)
        return out

    def apara(self, a, b):
        """Tira espaco e parenteses externos balanceados."""
        while True:
            a = self._ws(a, b)
            while b > a and self.m[b - 1].isspace():
                b -= 1
            if a < b and self.m[a] == "(" and self.par.get(a) == b - 1:
                a, b = a + 1, b - 1
            else:
                return a, b

    def norm(self, a, b):
        return " ".join(self.t[a:b].split())

    def _args(self, a, b):
        """Partes de topo de m[a:b] separadas por virgula (vazias descartadas)."""
        return [(x, y) for x, y in self._divide(self.tokens(a, b), a, b, (",",))
                if self._ws(x, y) < y]

    @staticmethod
    def _divide(toks, a, b, ops):
        partes, ini = [], a
        for tk in toks:
            if tk.tipo == "op" and tk.txt in ops:
                partes.append((ini, tk.ini))
                ini = tk.fim
        partes.append((ini, b))
        return partes

    def _limite(self, p):
        """Fim do contexto de p: a interpolacao mais interna que o contem, ou o <script>."""
        dentro = [y for x, y in self.interp if x <= p <= y]
        if dentro:
            return min(dentro)
        return next((b for a, b in self.regioes if a <= p < b), len(self.m))

    def fim_expr(self, ini):
        """Fim do comando que comeca em ini (RHS, corpo de arrow, return).

        Termina no `;` ou `,` de topo, no fechamento do grupo que o contem, ou
        numa quebra de linha de topo onde a expressao nao pode continuar: nem o
        token anterior nem o proximo e operador.
        """
        lim = self._limite(ini)
        ant = None
        i = self._ws(ini, lim)
        while i < lim:
            tk = self._tok(i)
            if tk.tipo == "fecha" or (tk.tipo == "op" and tk.txt in (";", ",")):
                break
            if ant is not None and "\n" in self.m[ant.fim:tk.ini] and not (
                    self._continua(ant, True) or self._continua(tk, False)):
                break
            ant = tk
            i = self._ws(tk.fim, lim)
        return ant.fim if ant is not None else ini

    @staticmethod
    def _continua(tk, antes):
        if tk.tipo == "op":
            return tk.txt in CONTINUA
        return tk.tipo == "id" and tk.txt in (PALAVRAS_OPERADOR if antes else ("in", "instanceof"))

    @staticmethod
    def _primeiro(*culpados):
        return next((c for c in culpados if c is not None), None)

    def _memoizado(self, chave, calcula):
        if chave in self.memo:
            return self.memo[chave]
        if chave in self.visitando:
            self.ciclos += 1        # NOME += `...${NOME}...`: a referencia recursiva e segura
            return None
        self.visitando.add(chave)
        antes = self.ciclos
        res = calcula()
        self.visitando.discard(chave)
        # Resultado otimista (dependeu de um ciclo ainda aberto) nao vai para o memo.
        if res is not None or self.ciclos == antes or not self.visitando:
            self.memo[chave] = res
        return res

    # -- indices: funcoes e ligacoes de nome ----------------------------------
    def _indexa_funcoes(self):
        m, n = self.m, len(self.m)
        for mm in RE_FUNCTION.finditer(m):
            abre = mm.end() - 1
            fecha = self.par.get(abre)
            k = self._ws(fecha + 1, n) if fecha is not None else n
            if k < n and m[k] == "{" and k in self.par:
                nome = mm.group(1) or self._nome_atribuido(mm.start())
                self._registra(Funcao(nome, mm.start(), (abre + 1, fecha), (k, self.par[k] + 1),
                                      True, mm.group(1) is not None))
        for mm in re.finditer(r"=>", m):
            j = mm.start() - 1
            while j >= 0 and m[j].isspace():
                j -= 1
            if j >= 0 and m[j] == ")" and j in self.par:
                ini = self.par[j]
                params = (ini + 1, j)
            else:
                mi = re.search(r"(?:[^\W\d]|\$)[\w$]*$", m[max(0, j - 80):j + 1])
                if not mi:
                    continue
                ini = max(0, j - 80) + mi.start()
                params = (ini, j + 1)
            ma = re.search(r"\basync\s+$", m[max(0, ini - 12):ini])
            ini_async = max(0, ini - 12) + ma.start() if ma else ini
            k = self._ws(mm.end(), n)
            if k < n and m[k] == "{" and k in self.par:
                corpo, bloco = (k, self.par[k] + 1), True
            else:
                corpo, bloco = (k, self.fim_expr(k)), False
            self._registra(Funcao(self._nome_atribuido(ini_async), ini_async, params, corpo,
                                  bloco, False), ini)

    def _registra(self, F, *outros_inis):
        self.funcoes.append(F)
        for p in (F.ini,) + outros_inis:
            self.funcao_por_ini[p] = F
        if F.nome:
            self.funcoes_por_nome.setdefault(F.nome, []).append(F)

    def _nome_atribuido(self, ini):
        mm = RE_NOME_ATRIBUIDO.search(self.m, max(0, ini - 120), ini)
        return mm.group(1) if mm else None

    def _escopo_de_bloco(self, pos):
        blk = self.bloco[pos]
        if blk < 0:
            return (0, len(self.m))
        # `{` sem par (fonte desbalanceada): o bloco vai ate o fim do arquivo.
        return (blk, self.par.get(blk, len(self.m) - 1) + 1)

    def _liga(self, L):
        self.ligacoes.setdefault(L.nome, []).append(L)

    def _indexa_ligacoes(self):
        for mm in RE_DECL.finditer(self.m):
            self._declaracao(mm.start(), mm.end(), mm.group(1))
        for F in self.funcoes:
            for nome, pos in self._nomes_de_params(*F.params):
                self._liga(Lig("param", nome, pos, F.corpo[0], F.corpo[1], None, None, F))
            if F.declarada:
                s0, s1 = self._escopo_de_bloco(F.ini)
                self._liga(Lig("func", F.nome, F.ini, s0, s1, None, None, F))
        for mm in RE_CATCH.finditer(self.m):
            k = self._ws(mm.end(), len(self.m))
            if self.m.startswith("{", k) and k in self.par:
                self._liga(Lig("catch", mm.group(1), mm.start(1), k, self.par[k] + 1,
                               None, None, None))

    def _declaracao(self, kw, k, palavra):
        m, n = self.m, len(self.m)
        escopo_for = None
        mf = RE_FOR.search(m, max(0, kw - 30), kw)
        if mf:
            abre = mf.start(1)
            fecha = self.par.get(abre)
            if fecha is None:
                return
            c = self._ws(fecha + 1, n)
            fim_corpo = self.par[c] + 1 if m.startswith("{", c) and c in self.par else self.fim_expr(c)
            escopo_for = (abre, fim_corpo)
        s0, s1 = escopo_for or self._escopo_de_bloco(kw)
        i = k
        while True:
            i = self._ws(i, n)
            if i < n and m[i] in "{[" and i in self.par:
                fim = self.par[i] + 1
                nomes, tipo = self._nomes_de_padrao(i, fim), "destr"
            else:
                mi = RE_IDENT.match(m, i)
                if not mi:
                    return
                fim, nomes, tipo = mi.end(), [(mi.group(0), i)], "decl"
            j = self._ws(fim, n)
            init = None
            if m.startswith("=", j) and not m.startswith(("==", "=>"), j):
                init = (j + 1, self.fim_expr(j + 1))
                fim = init[1]
            elif escopo_for and re.match(r"(?:of|in)\b", m[j:j + 3]):
                if tipo == "decl":
                    tipo = "forof" if m.startswith("of", j) else "forin"
                for nome, pos in nomes:   # for (const NOME of EXPR): vale no corpo
                    self._liga(Lig(tipo, nome, pos, fecha + 1, escopo_for[1], None, palavra,
                                   (j + 2, fecha)))
                return
            for nome, pos in nomes:
                self._liga(Lig(tipo, nome, pos, s0, s1, init, palavra, None))
            j = self._ws(fim, n)
            if not m.startswith(",", j):
                return
            i = j + 1

    def _nomes_de_padrao(self, a, b):
        out = []
        for mm in RE_NOME_DE_PADRAO.finditer(self.m, a, b):
            p = mm.start(1)
            if self.m[p - 1] == "." and not self.m.startswith("...", p - 3):
                continue
            out.append((mm.group(1), p))
        return out

    def _nomes_de_params(self, a, b):
        out = []
        for x, y in self._args(a, b):
            x = self._ws(x, y)
            if self.m.startswith("...", x):
                x = self._ws(x + 3, y)
            if self.m[x] in "{[" and x in self.par:
                out += self._nomes_de_padrao(x, self.par[x] + 1)
            else:
                mi = RE_IDENT.match(self.m, x)
                if mi:
                    out.append((mi.group(0), x))
        return out

    # -- 3. quem e MARKUP ------------------------------------------------------
    def marca(self, T):
        """T vira MARKUP e entra na fila; o que esta aninhado nele tambem (regra 3)."""
        if T.ini in self.markup:
            return
        self.markup.add(T.ini)
        self.fila.append(T)
        for F in T.filhos:
            self.marca(F)

    def raizes_de_sink(self):
        """Valor de .innerHTML/.outerHTML =, += e argumento HTML de insertAdjacentHTML/write."""
        out = [(mm.end(), self.fim_expr(mm.end())) for mm in RE_SINK_ATRIB.finditer(self.m)]
        for mm in RE_SINK_CHAMADA.finditer(self.m):
            abre = mm.end() - 1
            if abre in self.par:
                args = self._args(abre + 1, self.par[abre])
                out += args[1:] if mm.group(1) else args
        return out

    def achados(self):
        for T in self.templates:
            if any(RE_TAG.search(q) for q in T.quasis):
                self.marca(T)
        out = []
        # O valor que chega ao sink e julgado como uma interpolacao: todo template
        # que ele alcanca vira markup, e o trecho cru que ele carrega fora de
        # template (concatenacao, variavel, .join) e achado no proprio sink.
        for x, y in self.raizes_de_sink():
            culpado = self.julga(x, y)
            if culpado is not None:
                out.append(Achado(self.rel, self.t.count("\n", 0, self._ws(x, y)) + 1,
                                  self.norm(x, y), culpado))
        while self.fila:
            T = self.fila.pop(0)
            for dolar, x, y in T.exprs:
                culpado = self.julga(x, y)
                if culpado is not None:
                    out.append(Achado(self.rel, self.t.count("\n", 0, dolar) + 1,
                                      self.norm(x, y), culpado))
        return sorted(out)

    # -- 4. classificador: None = SEGURO; senao o trecho cru culpado ----------
    def julga(self, a, b):
        a, b = self.apara(a, b)
        if a >= b:
            return None
        toks = self.tokens(a, b)
        texto = self.norm(a, b)
        ops = [(k, tk) for k, tk in enumerate(toks) if tk.tipo == "op"]
        virgulas = [tk for _, tk in ops if tk.txt == ","]
        if virgulas:                                    # a, b: vale o ultimo
            return self.julga(virgulas[-1].fim, b)
        if any(tk.txt == "=>" for _, tk in ops):        # funcao como valor imprime o codigo
            return texto
        for _, tk in ops:                               # atribuicao: vale o lado direito
            if tk.txt in ATRIBUICOES:
                if tk.txt == "+=":
                    return self._primeiro(self.julga(a, tk.ini), self.julga(tk.fim, b))
                return self.julga(tk.fim, b) if tk.txt in ("=", "||=", "??=", "&&=") else None
        q = next((k for k, tk in ops if tk.txt == "?"), None)
        if q is not None:                               # c ? x : y -> x e y (c nao e impresso)
            prof = 0
            for k, tk in ops:
                if k <= q or tk.txt not in ("?", ":"):
                    continue
                if tk.txt == "?":
                    prof += 1
                elif prof:
                    prof -= 1
                else:
                    return self._primeiro(self.julga(toks[q].fim, tk.ini), self.julga(tk.fim, b))
            return texto
        partes = self._divide(toks, a, b, ("||", "??"))
        if len(partes) > 1:                             # qualquer operando pode ser impresso
            return self._primeiro(*[self.julga(x, y) for x, y in partes])
        partes = self._divide(toks, a, b, ("&&",))
        if len(partes) > 1:                             # o falsy de a imprime false/0/''/null
            return self.julga(*partes[-1])
        if any(tk.txt in BINARIOS_SEGUROS for _, tk in ops) or any(
                tk.tipo == "id" and tk.txt in ("instanceof", "in") for tk in toks):
            return None                                 # comparacao/bit a bit: bool ou numero
        bins = [k for k, tk in ops
                if tk.txt in ("+", "-") and k and self._fim_de_operando(toks[k - 1])]
        if bins:                                        # a + b: todo operando; antes do ultimo - e numero
            def operando(j):
                return (a if j == 0 else toks[bins[j - 1]].fim,
                        b if j == len(bins) else toks[bins[j]].ini)
            menos = [j for j, k in enumerate(bins) if toks[k].txt == "-"]
            de = menos[-1] + 2 if menos else 0
            return self._primeiro(*[self.julga(*operando(j)) for j in range(de, len(bins) + 1)])
        if any(tk.txt in ("*", "/", "%", "**") for _, tk in ops):
            return None                                 # aritmetica: numero
        t0 = toks[0]
        if (t0.tipo == "op" and t0.txt in ("!", "-", "+", "~", "++", "--")) or (
                t0.tipo == "id" and t0.txt in ("typeof", "void", "delete")) or (
                toks[-1].tipo == "op" and toks[-1].txt in ("++", "--")):
            return None                                 # unario/posfixo: bool, numero, tipo
        if t0.tipo == "id" and t0.txt == "await" and len(toks) > 1:
            return self.julga(toks[1].ini, b)
        cadeia = self._parse_cadeia(toks)
        return texto if cadeia is None else self._valor_da_cadeia(*cadeia, a, texto)

    @staticmethod
    def _fim_de_operando(tk):
        if tk.tipo in ("num", "atom", "grp"):
            return True
        if tk.tipo == "id":
            return tk.txt not in PALAVRAS_OPERADOR
        return tk.tipo == "op" and tk.txt in ("++", "--")

    @staticmethod
    def _parse_cadeia(toks):
        """(primario, sufixos) de `P.prop[i](args)...`; None se nao for uma cadeia."""
        t0, k, prim = toks[0], 1, toks[0]
        if t0.tipo == "id" and t0.txt == "new":
            nomes = []
            while k < len(toks) and toks[k].tipo == "id":
                nomes.append(toks[k].txt)
                k += 1
                if (k + 1 < len(toks) and toks[k].tipo == "op" and toks[k].txt == "."
                        and toks[k + 1].tipo == "id"):
                    k += 1
                else:
                    break
            if k < len(toks) and toks[k].tipo == "grp" and toks[k].txt == "(":
                k += 1
            prim = Tok("new", t0.ini, toks[k - 1].fim, ".".join(nomes))
        elif t0.tipo not in ("id", "num", "atom", "grp"):
            return None
        suf = []
        while k < len(toks):
            tk = toks[k]
            nx = toks[k + 1] if k + 1 < len(toks) else None
            if tk.tipo == "op" and tk.txt in (".", "?.") and nx is not None and nx.tipo == "id":
                suf.append(Tok("prop", tk.ini, nx.fim, nx.txt))
                k += 2
            elif (tk.tipo == "op" and tk.txt == "?." and nx is not None and nx.tipo == "grp"
                    and nx.txt in ("(", "[")):
                suf.append(Tok("call" if nx.txt == "(" else "idx", nx.ini, nx.fim, nx.txt))
                k += 2
            elif tk.tipo == "grp" and tk.txt in ("(", "["):
                suf.append(Tok("call" if tk.txt == "(" else "idx", tk.ini, tk.fim, tk.txt))
                k += 1
            else:
                return None
        return prim, suf

    @staticmethod
    def _receptor(prim, suf):
        """Nome logo antes do ultimo `.prop` (o `stage` de `stage.id`)."""
        if len(suf) >= 2 and suf[-2].tipo == "prop":
            return suf[-2].txt
        return prim.txt if len(suf) == 1 and prim.tipo == "id" else None

    @staticmethod
    def _nome_numerico(nome, receptor):
        """PK/FK inteira ou contador pelo nome - menos os ids que sao texto."""
        if not RE_NOME_NUMERICO.fullmatch(nome) or nome in IDS_TEXTO or RE_ID_DE_ETAPA.search(nome):
            return False
        return not (nome == "id" and receptor in ("stage", "etapa"))

    def _id_de_etapa(self, prim, suf):
        """`X.id` com X parametro de callback sobre `.etapas`/`.stages`: id de etapa, texto."""
        if not (prim.tipo == "id" and len(suf) == 1 and suf[0].tipo == "prop" and suf[0].txt == "id"):
            return False
        return any(L.tipo == "param" and RE_CALLBACK_DE_ETAPAS.search(
                       self.m, max(0, L.extra.ini - 200), L.extra.ini)
                   for L in self._visiveis(prim.txt, prim.ini))

    def _valor_da_cadeia(self, prim, suf, a, texto):
        n = len(suf)
        if not n:
            return self._primario(prim, texto)
        ult = suf[-1]

        def ate(j):     # fim da cadeia com so os j primeiros sufixos
            return suf[j - 1].fim if j else prim.fim

        if prim.tipo == "id" and prim.txt in ESCAPES and n == 1 and ult.tipo == "call":
            return None                                 # esc(...) cobrindo o valor inteiro
        if (prim.tipo == "id" and prim.txt == "DOMPurify" and n == 2 and suf[0].tipo == "prop"
                and suf[0].txt == "sanitize" and ult.tipo == "call"):
            return None                                 # sanitizador de HTML cobrindo o valor inteiro
        if ult.tipo == "prop" and (ult.txt in ("length", "size")
                                   or (self._nome_numerico(ult.txt, self._receptor(prim, suf))
                                       and not self._id_de_etapa(prim, suf))):
            return None
        if prim.tipo == "id" and all(s.tipo in ("prop", "idx") for s in suf):
            if self._constante(prim.txt, prim.ini) is not None:
                return None                             # TABELA[k], TABELA.x
            if all(s.tipo == "prop" for s in suf) and self._laco_constante(prim.txt, prim.ini, True):
                return None                             # x.campo, x de laco sobre array constante
        if ult.tipo == "call":
            if n == 1 and prim.tipo == "id":
                if prim.txt in GLOBAIS_NUMERICAS:
                    return None
                return self._chamada(prim.txt, prim.ini, texto)
            if n >= 2 and suf[-2].tipo == "prop":
                metodo, rec = suf[-2].txt, (a, ate(n - 2))
                if (n == 2 and prim.tipo == "id" and prim.txt == "Math") or metodo in METODOS_DATA_NUMERO:
                    return None
                if metodo == "toLocaleString":  # String herda o de Object: devolve o proprio texto
                    return None if self._e_new_date(*rec) or self.numerico(*rec) else texto
                if metodo in METODOS_PRESERVAM:
                    return self.julga(*rec)
                if metodo == "join":            # alem da spec: qualquer separador seguro
                    seps = [self.julga(x, y) for x, y in self._args(ult.ini + 1, ult.fim - 1)]
                    return self._primeiro(self.julga(*rec), *seps)
                if metodo == "map":             # array: imprime o que o callback devolve
                    return self._callback(ult, texto)
        if (ult.tipo == "idx" and n >= 3 and suf[-2].tipo == "call" and suf[-3].tipo == "prop"
                and suf[-3].txt == "split"):
            dentro = self.tokens(ult.ini + 1, ult.fim - 1)
            if len(dentro) == 1 and dentro[0].tipo == "num":
                return self.julga(a, ate(n - 3))    # x.split(...)[N]
        return texto

    def _primario(self, prim, texto):
        if prim.tipo == "atom":
            if prim.txt == "tpl":
                # Template aninhado: seguro NESTE ponto. Ele vira MARKUP e cada
                # interpolacao dele e julgada - e reportada - no proprio ponto.
                self.marca(self.tpl_por_ini[prim.ini])
            return texto if prim.txt == "re" else None
        if prim.tipo == "num":
            return None
        if prim.tipo == "new":
            return None if prim.txt == "Date" else texto
        if prim.tipo == "grp":
            if prim.txt == "(":
                return self.julga(prim.ini + 1, prim.fim - 1)
            if prim.txt == "[":
                return self._primeiro(*[self.julga(x, y) for x, y in self._args(prim.ini + 1, prim.fim - 1)])
            return None                                 # objeto literal: [object Object]
        if prim.tipo == "id":
            if prim.txt in LITERAIS or self._laco_constante(prim.txt, prim.ini, False):
                return None
            # O nome so vale para nome SOLTO (global, de outro arquivo). Nome ligado
            # no arquivo e resolvido como qualquer outro: `const total = l.nome` e o
            # parametro `id` de addStageRow (que recebe o id de etapa) sao texto.
            if self._nome_numerico(prim.txt, None) and not self._visiveis(prim.txt, prim.ini):
                return None
            return self._resolve(prim.txt, prim.ini, texto)
        return texto

    # -- resolucao com escopo --------------------------------------------------
    def _visiveis(self, nome, pos):
        """As ligacoes de `nome` no escopo mais interno que contem pos."""
        cands = [L for L in self.ligacoes.get(nome, ()) if L.s0 <= pos < L.s1]
        if not cands:
            return []
        s0 = max(L.s0 for L in cands)
        return [L for L in cands if L.s0 == s0]

    def _resolve(self, nome, pos, texto):
        ligs = self._visiveis(nome, pos)
        if not ligs:
            return texto        # global, de outro arquivo, nao declarado: CRU
        return self._primeiro(*[self._valor(L) for L in ligs])

    def _valor(self, L):
        """Seguro sse todo valor que a ligacao pode ter e seguro."""
        if L.tipo != "decl":
            return L.nome       # parametro, desestruturacao, catch, funcao: CRU

        def calcula():
            valores = ([L.init] if L.init else []) + self._escritas(L)
            return self._primeiro(*[self.julga(x, y) for x, y in valores])
        return self._memoizado(("lig", L.pos), calcula)

    def _escritas(self, L):
        """RHS de toda escrita em NOME no escopo: =, +=, ||=, ??=, NOME[i] =, push/unshift."""
        m, nome, out = self.m, re.escape(L.nome), []
        for mm in re.compile(r"(?<![\w$.])" + nome + r"\s*(?:\+=|\|\|=|\?\?=|&&=|=)(?![=>])"
                             ).finditer(m, L.s0, L.s1):
            if mm.start() != L.pos:
                out.append((mm.end(), self.fim_expr(mm.end())))
        for mm in re.compile(r"(?<![\w$.])" + nome + r"\s*\[").finditer(m, L.s0, L.s1):
            abre = mm.end() - 1
            if abre in self.par:
                j = self._ws(self.par[abre] + 1, len(m))
                if m.startswith("=", j) and not m.startswith(("==", "=>"), j):
                    out.append((j + 1, self.fim_expr(j + 1)))
        for mm in re.compile(r"(?<![\w$.])" + nome + r"\s*\.\s*(?:push|unshift)\s*\("
                             ).finditer(m, L.s0, L.s1):
            abre = mm.end() - 1
            if abre in self.par:
                out += self._args(abre + 1, self.par[abre])
        return out

    def _chamada(self, nome, pos, texto):
        """NOME(...) de funcao do arquivo: segura sse todo retorno de toda declaracao e seguro."""
        ligs = self._visiveis(nome, pos)
        if not ligs or not all(L.tipo == "func" or (L.tipo == "decl" and L.init
                                                     and self._funcao_em(L.init[0]))
                               for L in ligs):
            return texto
        return self._memoizado(("fn", nome), lambda: self._primeiro(
            *[self._retorno_de(F) for F in self.funcoes_por_nome.get(nome, ())]))

    def _retorno_de(self, F):
        if not F.bloco:
            return self.julga(*F.corpo)
        culpados = []
        for mm in RE_RETURN.finditer(self.m, F.corpo[0], F.corpo[1]):
            if self._funcao_de(mm.start()) is not F:
                continue        # return de callback aninhado nao conta
            j = self._ws(mm.end(), F.corpo[1])
            if j >= F.corpo[1] or self.m[j] in ";}" or "\n" in self.m[mm.end():j]:
                continue        # `return;` devolve undefined
            culpados.append(self.julga(mm.end(), self.fim_expr(mm.end())))
        return self._primeiro(*culpados)

    def _funcao_de(self, p):
        dentro = [F for F in self.funcoes if F.corpo[0] <= p < F.corpo[1]]
        return max(dentro, key=lambda F: F.corpo[0]) if dentro else None

    def _funcao_em(self, pos):
        pos = self._ws(pos, len(self.m))
        F = self.funcao_por_ini.get(pos)
        if F is None and self.m.startswith("async", pos):
            F = self.funcao_por_ini.get(self._ws(pos + 5, len(self.m)))
        return F

    def _callback(self, chamada, texto):
        """X.map(cb): seguro sse o que cb devolve e seguro."""
        args = self._args(chamada.ini + 1, chamada.fim - 1)
        if not args:
            return texto
        x, y = self.apara(*args[0])
        F = self._funcao_em(x)
        if F is not None:
            return self._retorno_de(F)
        if RE_IDENT.fullmatch(self.m, x, y):
            return self._chamada(self.m[x:y], x, texto)
        return texto

    def _laco_constante(self, nome, pos, objetos):
        """NOME vem de ARR.map(NOME =>, ARR.forEach(NOME => ou for (const NOME of ARR), ARR constante?"""
        ligs = self._visiveis(nome, pos)
        if len(ligs) != 1:
            return False
        L = ligs[0]
        if L.tipo == "param":
            params = self._nomes_de_params(*L.extra.params)
            mm = RE_RECEPTOR_DE_CALLBACK.search(self.m, max(0, L.extra.ini - 200), L.extra.ini)
            if not params or params[0][0] != nome or not mm:
                return False
            arr, arr_pos = mm.group(1), mm.start(1)
        elif L.tipo == "forof":
            x, y = self.apara(*L.extra)
            if not RE_IDENT.fullmatch(self.m, x, y):
                return False
            arr, arr_pos = self.m[x:y], x
        else:
            return False
        span = self._constante(arr, arr_pos)
        if span is None or self.m[span[0]] != "[":
            return False
        # NOME.x so vale quando todo elemento e objeto literal.
        return not objetos or all(self.m[self._ws(x, y)] == "{"
                                  for x, y in self._args(span[0] + 1, span[1] - 1))

    def _constante(self, nome, pos):
        """(ini, fim) de `const NOME = {...}|[...]` so de literais e nunca escrito; senao None."""
        ligs = self._visiveis(nome, pos)
        if len(ligs) != 1 or ligs[0].tipo != "decl" or ligs[0].palavra != "const" or not ligs[0].init:
            return None
        x, y = self.apara(*ligs[0].init)
        if x >= y or self.m[x] not in "{[" or self.par.get(x) != y - 1:
            return None
        if not self._literal_puro(x + 1, y - 1) or self._escrito(nome):
            return None
        return (x, y)

    def _literal_puro(self, a, b):
        toks = self.tokens(a, b)
        for k, tk in enumerate(toks):
            nx = toks[k + 1] if k + 1 < len(toks) else None
            chave = nx is not None and nx.tipo == "op" and nx.txt == ":"
            if tk.tipo == "grp":
                if tk.txt == "(" or not self._literal_puro(tk.ini + 1, tk.fim - 1):
                    return False
            elif tk.tipo == "atom":
                if tk.txt != "str":
                    return False
            elif tk.tipo == "id":
                if not chave and tk.txt not in ("true", "false", "null", "undefined"):
                    return False
            elif tk.tipo == "op":
                if tk.txt not in (",", ":", "-", "+"):
                    return False
            elif tk.tipo != "num":
                return False
        return True

    def _escrito(self, nome):
        """O arquivo escreve em NOME? (NOME[..] =, NOME.x =, push/unshift/splice, Object.assign)"""
        if nome not in self._escritos:
            self._escritos[nome] = self._procura_escrita(nome)
        return self._escritos[nome]

    def _procura_escrita(self, nome):
        m, n = self.m, re.escape(nome)
        if re.search(r"(?<![\w$.])" + n + r"\s*\.\s*(?:push|unshift|splice)\s*\(", m) or re.search(
                r"\bObject\s*\.\s*assign\s*\(\s*" + n + r"(?![\w$])", m):
            return True
        for mm in re.finditer(r"(?<![\w$.])" + n + r"(?![\w$])", m):
            j, houve = mm.end(), False
            while True:
                j = self._ws(j, len(m))
                mi = RE_IDENT.match(m, self._ws(j + 1, len(m))) if m.startswith(".", j) else None
                if mi and not m.startswith("...", j):
                    j, houve = mi.end(), True
                elif m.startswith("[", j) and j in self.par:
                    j, houve = self.par[j] + 1, True
                else:
                    break
            if houve and RE_OP_DE_ESCRITA.match(m, self._ws(j, len(m))):
                return True
        return False

    def numerico(self, a, b):
        """Numero por construcao (o receptor que .toLocaleString aceita)."""
        a, b = self.apara(a, b)
        toks = self.tokens(a, b)
        if not toks:
            return False
        ops = [tk.txt for tk in toks if tk.tipo == "op"]
        unario = toks[0].tipo == "op" and toks[0].txt in ("-", "+")     # -x e +x sao numero
        if unario:
            ops = ops[1:]
        aritmetica = ("-", "*", "/", "%", "**")
        # Sem `+` (pode concatenar texto), ternario ou logico no topo.
        if (unario or any(o in aritmetica for o in ops)) and all(
                o in (".", "?.") + aritmetica for o in ops):
            return True
        cadeia = self._parse_cadeia(toks)
        if cadeia is None:
            return False
        prim, suf = cadeia
        if not suf:
            return prim.tipo == "num" or (prim.tipo == "id" and self._nome_numerico(prim.txt, None))
        ult = suf[-1]
        if ult.tipo == "prop":
            return ult.txt in ("length", "size") or self._nome_numerico(ult.txt, self._receptor(prim, suf))
        if ult.tipo != "call":
            return False
        if prim.tipo == "id" and ((len(suf) == 1 and prim.txt in ("Number", "parseInt", "parseFloat"))
                                  or (len(suf) == 2 and prim.txt == "Math")):
            return True
        return len(suf) >= 2 and suf[-2].tipo == "prop" and suf[-2].txt in METODOS_NUMERO

    def _e_new_date(self, a, b):
        a, b = self.apara(a, b)
        tk = [(t.tipo, t.txt) for t in self.tokens(a, b)]
        return tk[:2] == [("id", "new"), ("id", "Date")] and tk[2:] in ([], [("grp", "(")])


def analisa(rel, texto):
    """Ponto de entrada unico: os achados CRUS de um arquivo (rel = caminho relativo)."""
    return Analise(rel, texto).achados()


def arquivos_do_front():
    return sorted(list((ROOT / "templates").rglob("*.html"))
                  + list((ROOT / "static" / "js").glob("*.js")))


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

MOTIVO_MINIMO = 25


def descreve(a):
    s = f"{a.rel}:{a.linha} -> ${{{a.expr}}}"
    if a.culpado != a.expr:
        s += f"  [cru: {a.culpado}]"
    return s


def confere_permitidas(achados, permitidas):
    """Problemas entre os achados crus e a allowlist (lista vazia = ok)."""
    problemas = []
    por_chave = {}
    for a in achados:
        por_chave.setdefault((a.rel, a.expr), []).append(a)
    for chave, lista in sorted(por_chave.items()):
        if chave not in permitidas:
            problemas += [f"nao classificado: {descreve(a)}" for a in lista]
        elif len(lista) != permitidas[chave][0]:
            problemas.append(
                f"contagem: {chave[0]} ${{{chave[1]}}} auditado "
                f"{permitidas[chave][0]}x, achado {len(lista)}x: "
                + "; ".join(descreve(a) for a in lista))
    for chave, (_, motivo) in sorted(permitidas.items()):
        if chave not in por_chave:
            problemas.append(f"entrada morta: {chave[0]} ${{{chave[1]}}} nao tem mais achado")
        if len(motivo.strip()) < MOTIVO_MINIMO:
            problemas.append(f"motivo curto (<{MOTIVO_MINIMO}): {chave[0]} ${{{chave[1]}}}")
    return problemas


# ---------------------------------------------------------------------------
# 1. A REGRA no front do CRM
# ---------------------------------------------------------------------------
print("1) todo ${...} de markup do CRM e esc(), numero, constante ou allowlist")

arquivos = arquivos_do_front()
achados = []
for p in arquivos:
    achados += analisa(p.relative_to(ROOT).as_posix(), p.read_text(encoding="utf-8"))
check(len(arquivos) >= 20, f"arquivos de front analisados: {len(arquivos)}")
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
    ("stageId (alem da spec: o mesmo id de etapa)", "el.innerHTML = `<i>${stageId}</i>`;",
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
    ("template sem tag aninhado em argumento tambem vira markup (regra 3)",
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
    # _procura_escrita: `T[k] = ...` tira o status de constante da tabela
    ("tabela const escrita (T[k] = ...) nao e constante",
     "const T = { a: 'x' };\nT[k] = y.nome;\nel.innerHTML = `<td>${T[k]}</td>`;",
     [("T[k]", "T[k]")]),
    # _procura_escrita: `T.push(...)` (outro caminho) tambem
    ("tabela const escrita (T.push) nao e constante",
     "const T = ['a'];\nT.push(y.nome);\nel.innerHTML = `<td>${T[0]}</td>`;",
     [("T[0]", "T[0]")]),
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
# 5. IDS_TEXTO x app/models: coluna String `*_id` nova reprova ate ser classificada
# ---------------------------------------------------------------------------
print("\n5) excecoes de id texto conferidas contra app/models")

RE_COLUNA_ID_TEXTO = re.compile(r"(\w+_id)\s*=\s*Column\(\s*String")
colunas = set()
for p in sorted((ROOT / "app" / "models").rglob("*.py")):
    colunas |= set(RE_COLUNA_ID_TEXTO.findall(p.read_text(encoding="utf-8")))
check(bool(colunas), f"o scan acha as colunas String *_id dos models ({sorted(colunas)})")
check(colunas <= IDS_TEXTO,
      f"toda coluna String *_id esta em IDS_TEXTO (fora: {sorted(colunas - IDS_TEXTO)})")
novo = set(RE_COLUNA_ID_TEXTO.findall("    codigo_id = Column(String(40), nullable=True)"))
check(bool(novo - IDS_TEXTO), "controle: uma coluna String *_id nova fica fora de IDS_TEXTO")
check(crus("el.innerHTML = `<i>${t.google_calendar_event_id}</i>`;")
      == [("t.google_calendar_event_id", "t.google_calendar_event_id")],
      "a regra numerica respeita IDS_TEXTO")


print()
if falhas:
    print(f"{len(falhas)} FALHA(S)")
    sys.exit(1)
print("OK: nenhum ${...} cru em markup do CRM fora da allowlist")
