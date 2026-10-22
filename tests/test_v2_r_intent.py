"""
BIA-V2 Fase 4 - guarda estatica R-INTENT (decisao 15 do subgate de
especificacao de 2026-09-02).

Fonte normativa: docs/superpowers/plans/2026-08-29-bia-v2.md, blocos
"R-INTENT" (FASE 1 - Contratos) e "Decisao 15 - guarda R-INTENT" (Subgate de
especificacao da Fase 4). A regra funcional e simples e ABSOLUTA: nenhuma
decisao de estado, fila, handoff, responsavel, bot ou qualquer efeito pode
ramificar sobre o valor livre de `AIInterpretationResult.intent` enquanto nao
existir vocabulario fechado aprovado. Este arquivo NAO cria essa regra - ele
so tenta pegar, estruturalmente, uma parte das formas de quebra-la.

ESTA GUARDA E PARCIAL, NUNCA PROVA SEMANTICA. Ela varre AST em busca de
padroes SINTATICOS conhecidos; nao tem inferencia de tipo, nao segue
data-flow entre variaveis nem entre arquivos. A regra funcional R-INTENT
continua valendo integralmente mesmo onde esta guarda nao alcanca - ver
LIMITES DESTA GUARDA abaixo, documentados aqui de proposito (decisao 15
exige isso explicitamente, "nunca implicito").

ESCOPO - tres globs, resolvidos a cada execucao via pathlib (NUNCA lista fixa
de nomes, para nao perder silenciosamente um arquivo novo como
v2_agent.py/v2_lead_domain.py, que a File Structure do plano ja prevê):

  1. conversas/app/v2/**/*.py
  2. conversas/app/routers/v2_*.py
  3. app/routers/v2_*.py

Tres arquivos da File Structure ficam FORA dos tres globs, e a exclusao e
estrutural (nenhum segue a convencao "sob app/v2/" nem "prefixo v2_"), nao
uma lista negativa mantida a mao:

  - conversas/app/models/evento.py     (declaracao de schema ORM)
  - conversas/app/models/handoff_op.py (idem)
  - conversas/app/logging_config.py    (dictConfig)

Nenhum dos tres hospeda logica decisoria; se algum dia hospedar, o glob
precisa crescer junto - a cobertura desta guarda e dos ARQUIVOS VARRIDOS, nao
do conceito abstrato "codigo V2". `tests/` nunca entra em nenhum dos tres
globs (todos apontam para dentro de app/), e a premissa de que codigo de
producao sob esses globs nunca importa de `tests/` fica so escrita, nao
imposta em runtime.

DETECCAO - no minimo (decisao 15), os 4 padroes de acesso a `intent`:
`.intent`, `["intent"]`, `.get("intent")`, `getattr(x, "intent")`.

POSICOES DECISORIAS - no minimo (decisao 15): `if`/`elif`, `while`, expressao
condicional (ternario), `if` de comprehension, `match`/patterns quando
`intent` participa da decisao, e uso direto de `intent` como chave/indice
para escolher acao ou estado (ex.: `ACOES[resultado.intent]`). `MatchClass`
com keyword `intent=` (ex.: `case AIInterpretationResult(intent="x"):`) e
tratado EXPLICITAMENTE pelo scanner abaixo (nao e so uma limitacao anotada).
Como extra alem do minimo, `MatchMapping` com chave `"intent"`
(`case {"intent": "x"}:`) tambem e coberto, por ser o par estrutural direto
de `["intent"]` e custar o mesmo para implementar quanto para documentar como
lacuna.

A guarda NAO falha quando `intent` e so transportado como dado opaco (
atribuicao, campo de dict/payload, argumento de log), exibido em contexto
informativo, ou usado fora das posicoes decisorias acima.

LIMITES DESTA GUARDA (documentados aqui, nunca implicitos - decisao 15):

  - Alias e data-flow profundo: `x = resultado.intent` seguido de
    `if x == "humano":` NAO e pego - o teste so ve o Name `x`, o vinculo
    sintatico com `.intent` ja se perdeu na atribuicao anterior. Mesma
    lacuna quando uma funcao devolve um bool derivado de intent e o
    CHAMADOR decide em cima do retorno (ex.: `def e_humano(r): return
    r.intent == "humano"` - ver caso N10 abaixo: sozinha, essa funcao passa
    limpo por esta guarda, e a decisao real so aparece em outro arquivo).
  - `getattr(x, campo)` com NOME DINAMICO (variavel, nao literal string) nao
    e pego - so `getattr(x, "intent")` com o literal exato e detectado.
  - `**kwargs`: repasse via desempacotamento (ex.: `despachar(**dados)` onde
    `dados` veio de `resultado.model_dump()` e `despachar` decide
    internamente sobre `kwargs.get("intent")`) nao e pego - a guarda e por
    arquivo/AST, sem rastrear argumentos entre chamadas.
  - `model_dump()` seguido de repasse: uma vez serializado para dict e
    passado adiante, o vinculo sintatico com `AIInterpretationResult.intent`
    se dissolve; uma decisao tomada em OUTRO arquivo ou funcao sobre esse
    dict nao e rastreada ate a origem.
  - Qualquer outra transformacao equivalente que dissolva o vinculo sintatico
    (round-trip por json, cache, fila, variavel de modulo etc.) tem a mesma
    lacuna acima - a guarda e estrutural, nao segue o dado.
  - Posicoes FORA da lista minima nao sao varridas: curto-circuito `and`/`or`
    usado como controle de fluxo fora de if/while/ternario, e `assert`, por
    exemplo. So conta quando o acesso a intent esta DENTRO do subtree de uma
    posicao coberta.
  - A guarda NAO verifica o TIPO do objeto: `.intent`, `["intent"]`,
    `.get("intent")` e `getattr(x, "intent")` disparam a MESMA checagem em
    QUALQUER objeto, mesmo que nao venha de AIInterpretationResult.
    Deliberado - favorece falso positivo (obriga renomear ou justificar)
    sobre falso negativo (deixar passar uma decisao real).

Implementacao e testes no mesmo arquivo, sem modulo separado sob
conversas/app/v2/ - e o proprio arquivo que a File Structure do plano lista.

Roda standalone:  python tests/test_v2_r_intent.py
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Conversas - o literal abaixo tambem e o discriminador de job do CI
# (.github/workflows/test.yml separa as suites com grep -l CONVERSAS_DIR).
CONVERSAS_DIR = ROOT / "conversas"

failures = []
executados = []


def check(cond, msg):
    executados.append(msg)
    if cond:
        print(f"OK   {msg}")
    else:
        print(f"FAIL {msg}")
        failures.append(msg)


# === SCANNER - deteccao dos 4 padroes minimos de acesso a `intent` ========
_CHAVE_INTENT = "intent"


def _eh_constante_intent(no):
    """True se `no` for o literal string "intent" (ast.Constant)."""
    return isinstance(no, ast.Constant) and isinstance(no.value, str) and no.value == _CHAVE_INTENT


def _eh_acesso_intent(no):
    """True se `no` for um dos 4 padroes MINIMOS de acesso a `intent` que a
    decisao 15 exige detectar: `.intent`, `["intent"]`, `.get("intent")` e
    `getattr(x, "intent")`. Ver LIMITES DESTA GUARDA no docstring do modulo
    para o que fica FORA deste conjunto (alias, data-flow, getattr dinamico,
    **kwargs, model_dump()+repasse).
    """
    if isinstance(no, ast.Attribute) and no.attr == _CHAVE_INTENT:
        return True
    if isinstance(no, ast.Subscript) and _eh_constante_intent(no.slice):
        return True
    if (
        isinstance(no, ast.Call)
        and isinstance(no.func, ast.Attribute)
        and no.func.attr == "get"
        and no.args
        and _eh_constante_intent(no.args[0])
    ):
        return True
    if (
        isinstance(no, ast.Call)
        and isinstance(no.func, ast.Name)
        and no.func.id == "getattr"
        and len(no.args) >= 2
        and _eh_constante_intent(no.args[1])
    ):
        return True
    return False


def _contem_acesso_intent(subarvore):
    """Varre `subarvore` (uma expressao AST) e diz se ALGUM no dentro dela e
    um acesso a `intent`, em qualquer profundidade (pega `.intent` escondido
    dentro de chamada, f-string, bool op etc. - tudo que ast.walk alcanca)."""
    return any(_eh_acesso_intent(no) for no in ast.walk(subarvore))


def _varrer_arvore(arvore):
    """Varre um MODULO AST completo e devolve as violacoes de R-INTENT como
    lista de tuplas (lineno, motivo). Lista vazia = nenhuma decisao sobre
    `intent` encontrada nas posicoes decisorias minimas da decisao 15.

    Cada regra abaixo e independente das outras (nao e if/elif): um mesmo
    trecho pode acionar mais de uma regra (ex.: `intent` usado como chave de
    subscript DENTRO de um if-test aciona a regra do if E a do subscript) -
    isso e esperado e so torna o diagnostico mais rico, nunca menos correto.
    """
    violacoes = []

    def _reporta(no_com_posicao, motivo):
        violacoes.append((getattr(no_com_posicao, "lineno", 0), motivo))

    for no in ast.walk(arvore):
        # if / elif - elif e um If aninhado em `orelse`; ast.walk alcanca os dois.
        if isinstance(no, ast.If) and _contem_acesso_intent(no.test):
            _reporta(no, "if/elif: intent participa do teste")

        # while
        if isinstance(no, ast.While) and _contem_acesso_intent(no.test):
            _reporta(no, "while: intent participa do teste")

        # expressao condicional (ternario): `a if <teste> else b`
        if isinstance(no, ast.IfExp) and _contem_acesso_intent(no.test):
            _reporta(no, "expressao condicional (ternario): intent participa do teste")

        # if de comprehension - comprehension nao tem lineno proprio; o
        # `if` individual (expr) tem.
        if isinstance(no, ast.comprehension):
            for cond in no.ifs:
                if _contem_acesso_intent(cond):
                    _reporta(cond, "if de comprehension: intent participa do teste")

        # uso direto como CHAVE/INDICE para escolher acao ou estado, ex.:
        # ACOES[resultado.intent](). So a fatia (slice) conta - o OBJETO
        # sendo indexado (`.value`) nao, para nao confundir com
        # `resultado.intent["outra_chave"]` (indexar DENTRO do valor de
        # intent e operacao diferente, fora do escopo desta regra).
        if isinstance(no, ast.Subscript) and _contem_acesso_intent(no.slice):
            _reporta(no, "subscript: intent usado como chave/indice")

        # match <subject>: - `match resultado.intent:`
        if isinstance(no, ast.Match) and _contem_acesso_intent(no.subject):
            _reporta(no, "match <subject>: intent participa da decisao")

        # case ... if <guard>: - match_case nao tem lineno proprio; o
        # guard (expr) tem.
        if isinstance(no, ast.match_case) and no.guard is not None and _contem_acesso_intent(no.guard):
            _reporta(no.guard, "case ... if <guard>: intent participa da decisao")

        # case Classe(intent=...): - MatchClass com keyword pattern "intent".
        # Tratado EXPLICITAMENTE (decisao 15 exige um dos dois; este e o
        # escolhido), nao e so uma linha na lista de limitacoes.
        if isinstance(no, ast.MatchClass) and _CHAVE_INTENT in no.kwd_attrs:
            _reporta(no, "case Classe(intent=...): MatchClass com keyword 'intent'")

        # case {"intent": ...}: - MatchMapping com chave "intent". EXTRA
        # alem do minimo da decisao 15 (que so exige o MatchClass acima) -
        # ver docstring do modulo.
        if isinstance(no, ast.MatchMapping) and any(
            isinstance(chave, ast.Constant) and chave.value == _CHAVE_INTENT
            for chave in no.keys
        ):
            _reporta(no, "case {'intent': ...}: MatchMapping com chave 'intent'")

    return violacoes


def violacoes_do_codigo(codigo_fonte, rotulo="<snippet>"):
    """Faz parse de uma STRING de codigo Python e devolve as violacoes de
    R-INTENT nela. Usado pelos casos sinteticos (GRUPO P/N) - nenhum arquivo
    de fixture e criado no repo, exatamente como o escopo pede."""
    arvore = ast.parse(codigo_fonte, filename=rotulo)
    return _varrer_arvore(arvore)


def caso_positivo(rotulo, codigo_fonte, contendo=None):
    """Espera PELO MENOS UMA violacao - prova que a guarda ACUSA uso
    decisorio. Se `contendo` for passado, exige que o motivo de alguma
    violacao contenha esse texto (prova que foi a REGRA CERTA que disparou,
    nao uma violacao coincidente de outra regra)."""
    viol = violacoes_do_codigo(codigo_fonte, rotulo)
    check(len(viol) > 0, f"{rotulo}: guarda ACUSA uso decisorio de intent (violacoes={viol})")
    if contendo is not None:
        achou = any(contendo in motivo for _, motivo in viol)
        check(achou, f"{rotulo}: violacao contem o motivo esperado {contendo!r} (obtido={viol})")
    return viol


def caso_negativo(rotulo, codigo_fonte):
    """Espera ZERO violacoes - prova que transporte puro / uso informativo
    NAO derruba a guarda (o falso positivo que este projeto rejeita)."""
    viol = violacoes_do_codigo(codigo_fonte, rotulo)
    check(len(viol) == 0, f"{rotulo}: guarda LIBERA transporte puro (violacoes inesperadas={viol})")
    return viol


# === GRUPO P - casos POSITIVOS: uso decisorio, a guarda tem de ACUSAR =====
# Cobre os 4 padroes minimos de acesso (.intent, ["intent"], .get, getattr)
# cruzados com as posicoes decisorias minimas (if, elif, while, ternario,
# comprehension-if, subscript-chave, match subject/guard/MatchClass), mais
# um extra de MatchMapping e um de profundidade (intent dentro de bool op
# aninhado no teste).

caso_positivo(
    "P01 if com .intent",
    """
def escolher_fila(resultado):
    if resultado.intent == "humano":
        return "fila_humana"
    return "fila_bot"
""",
    contendo="if/elif",
)

caso_positivo(
    "P02 elif com ['intent']",
    """
def escolher_fila(resultado, ativo):
    if not ativo:
        return "inativo"
    elif resultado["intent"] == "humano":
        return "fila_humana"
    return "fila_bot"
""",
    contendo="if/elif",
)

caso_positivo(
    "P03 while com .get('intent')",
    """
def aguardar(resultado, processar):
    while resultado.get("intent") == "aguardando":
        processar()
""",
    contendo="while",
)

caso_positivo(
    "P04 ternario com getattr(x, 'intent')",
    """
def rotular(resultado):
    return "humano" if getattr(resultado, "intent") == "humano" else "bot"
""",
    contendo="ternario",
)

caso_positivo(
    "P05 if de comprehension com .intent",
    """
def filtrar(lote):
    return [item for item in lote if item.intent == "humano"]
""",
    contendo="comprehension",
)

caso_positivo(
    "P06 subscript: intent como chave para escolher acao",
    """
ACOES = {"humano": ativar_humano, "bot": ativar_bot}


def despachar(resultado):
    return ACOES[resultado.intent]()
""",
    contendo="subscript",
)

caso_positivo(
    "P07 match <subject> com .intent",
    """
def rotear(resultado):
    match resultado.intent:
        case "humano":
            return fila_humana()
        case _:
            return fila_bot()
""",
    contendo="match <subject>",
)

caso_positivo(
    "P08 case ... if <guard> com .intent",
    """
def rotear(resultado):
    match resultado:
        case _ if resultado.intent == "humano":
            return fila_humana()
        case _:
            return fila_bot()
""",
    contendo="guard",
)

caso_positivo(
    "P09 MatchClass com keyword intent=",
    """
def rotear(resultado):
    match resultado:
        case AIInterpretationResult(intent="humano"):
            return fila_humana()
        case _:
            return fila_bot()
""",
    contendo="MatchClass",
)

caso_positivo(
    "P10 (extra) MatchMapping com chave 'intent'",
    """
def rotear(payload):
    match payload:
        case {"intent": "humano"}:
            return fila_humana()
        case _:
            return fila_bot()
""",
    contendo="MatchMapping",
)

caso_positivo(
    "P11 intent aninhado dentro de bool op no if-test",
    """
def escolher(resultado, outro):
    if outro and (resultado.intent == "humano" or resultado.intent == "manual"):
        return "fila_humana"
    return "fila_bot"
""",
    contendo="if/elif",
)


# === GRUPO N - casos NEGATIVOS: transporte puro, a guarda tem de LIBERAR ==
# Cada caso isola uma forma legitima de mexer em `intent` SEM decidir nada
# sobre ele - a mesma forma que um grep textual ingenuo confundiria com
# decisao (ex.: N04 tem um `if` na mesma funcao, mas intent nao esta no
# teste dele).

caso_negativo(
    "N01 atribuicao simples (dado opaco)",
    """
def transportar(resultado):
    dados_intent = resultado.intent
    return dados_intent
""",
)

caso_negativo(
    "N02 campo de dict/payload (transporte)",
    """
def montar_payload(resultado):
    return {"intent": resultado.intent, "outro_campo": 1}
""",
)

caso_negativo(
    "N03 log informativo",
    """
def registrar(resultado, logger):
    logger.info("intent recebido: %s", resultado.intent)
""",
)

caso_negativo(
    "N04 .intent no BODY do if, nao no teste (prova sensibilidade a posicao)",
    """
def exemplo(resultado, condicao_independente):
    if condicao_independente:
        texto = resultado.intent
        return texto
    return None
""",
)

caso_negativo(
    "N05 subscript com OUTRA chave, intent so no valor (RHS)",
    """
def anotar(resultado, destino_dict):
    destino_dict["intent_recebido"] = resultado.intent
""",
)

caso_negativo(
    "N06 pass-through simples (getter)",
    """
def extrair_intent(resultado):
    return resultado.intent
""",
)

caso_negativo(
    "N07 match cujo subject/guard NAO usa intent (.intent so no BODY do case)",
    """
def rotear(resultado, canal, registrar):
    match canal:
        case "whatsapp":
            registrar(resultado.intent)
            return "ok"
        case _:
            return "ignorado"
""",
)

caso_negativo(
    "N08 MatchClass com OUTRA keyword, nao 'intent' (prova precisao da regra)",
    """
def rotear(resultado):
    match resultado:
        case AIInterpretationResult(explicit_human_request=True):
            return "humano_direto"
        case _:
            return "bot"
""",
)

caso_negativo(
    "N09 subscript por OUTRO atributo, nao intent (prova precisao da regra)",
    """
def buscar_outro_campo(resultado, tabela):
    return tabela[resultado.explicit_human_request]
""",
)

caso_negativo(
    "N10 bool derivado de intent devolvido (alias/data-flow - ver LIMITES no docstring)",
    """
def e_humano(resultado):
    return resultado.intent == "humano"
""",
)


# === GRUPO E - escopo DERIVADO (decisao 15: nunca lista fixa de nomes) ====

def _bate_glob_v2_recursivo(caminho_rel_posix):
    """Replica MANUALMENTE, sem usar Path.glob, a semantica de
    `conversas/app/v2/**/*.py` - segunda fonte independente para provar que
    os arquivos excluidos da decisao 15 nao poderiam bater neste glob nem em
    tese, sem depender de eles existirem hoje no disco."""
    prefixo = "conversas/app/v2/"
    return caminho_rel_posix.startswith(prefixo) and caminho_rel_posix.endswith(".py")


def _bate_glob_router_direto(caminho_rel_posix, base):
    """Replica MANUALMENTE `<base>/v2_*.py`: arquivo direto no diretorio
    `base` (sem subpasta), prefixo `v2_`, sufixo `.py`."""
    prefixo = base + "/"
    if not caminho_rel_posix.startswith(prefixo):
        return False
    resto = caminho_rel_posix[len(prefixo):]
    return "/" not in resto and resto.startswith("v2_") and resto.endswith(".py")


def _no_escopo(caminho_rel_posix):
    return (
        _bate_glob_v2_recursivo(caminho_rel_posix)
        or _bate_glob_router_direto(caminho_rel_posix, "conversas/app/routers")
        or _bate_glob_router_direto(caminho_rel_posix, "app/routers")
    )


# Os tres arquivos que a decisao 15 nomeia como fora do escopo, e o motivo e
# ESTRUTURAL (nem prefixo app/v2/, nem prefixo v2_) - por isso o predicado
# acima os rejeita independente de eles existirem hoje no disco.
_ARQUIVOS_EXCLUIDOS_NOMEADOS = [
    "conversas/app/models/evento.py",
    "conversas/app/models/handoff_op.py",
    "conversas/app/logging_config.py",
]
for _i, _rel in enumerate(_ARQUIVOS_EXCLUIDOS_NOMEADOS, start=1):
    check(
        not _no_escopo(_rel),
        f"E{_i:02d} {_rel}: fora do escopo dos 3 globs, estruturalmente "
        f"(independente de existir hoje no disco)",
    )

# Resolucao REAL, via pathlib.Path.glob, no momento da execucao - e isto,
# nao uma lista de nomes, que a decisao 15 chama de "escopo derivado".
_ARQUIVOS_V2 = sorted(CONVERSAS_DIR.glob("app/v2/**/*.py"))
_ARQUIVOS_ROUTERS_CONVERSAS = sorted(CONVERSAS_DIR.glob("app/routers/v2_*.py"))
_ARQUIVOS_ROUTERS_CRM = sorted(ROOT.glob("app/routers/v2_*.py"))
_ARQUIVOS_ESCOPO = _ARQUIVOS_V2 + _ARQUIVOS_ROUTERS_CONVERSAS + _ARQUIVOS_ROUTERS_CRM

print()
print("Escopo resolvido HOJE (glob 1: conversas/app/v2/**/*.py):")
for _p in _ARQUIVOS_V2:
    print("   ", _p.relative_to(ROOT).as_posix())
print("Escopo resolvido HOJE (glob 2: conversas/app/routers/v2_*.py):")
for _p in _ARQUIVOS_ROUTERS_CONVERSAS:
    print("   ", _p.relative_to(ROOT).as_posix())
if not _ARQUIVOS_ROUTERS_CONVERSAS:
    print("    (nenhum ainda - normal, routers v2_* sao de fases futuras)")
print("Escopo resolvido HOJE (glob 3: app/routers/v2_*.py):")
for _p in _ARQUIVOS_ROUTERS_CRM:
    print("   ", _p.relative_to(ROOT).as_posix())
if not _ARQUIVOS_ROUTERS_CRM:
    print("    (nenhum ainda - normal, routers v2_* sao de fases futuras)")
print()

check(len(_ARQUIVOS_V2) >= 1, f"E10 glob 1 resolve pelo menos 1 arquivo hoje (achados={len(_ARQUIVOS_V2)})")

_NOMES_V2_HOJE = {p.name for p in _ARQUIVOS_V2}
for _nome_esperado in (
    "contratos.py", "prefiltro.py", "triagem.py", "eventos.py",
    "eventos_validacao.py", "__init__.py",
):
    check(
        _nome_esperado in _NOMES_V2_HOJE,
        f"E11 {_nome_esperado} esta no escopo resolvido hoje pelo glob 1 "
        f"(achados={sorted(_NOMES_V2_HOJE)})",
    )

# Cross-check: todo arquivo que o Path.glob() REAL resolve tambem bate no
# predicado manual independente (as duas fontes concordam).
for _p in _ARQUIVOS_ESCOPO:
    _rel = _p.relative_to(ROOT).as_posix()
    check(
        _no_escopo(_rel),
        f"E20 {_rel}: resolvido por Path.glob() TAMBEM bate no predicado "
        f"manual independente (as duas fontes concordam)",
    )

# Os tres excluidos nomeados nao aparecem na resolucao real de hoje.
_RESOLVIDOS_POSIX = {p.relative_to(ROOT).as_posix() for p in _ARQUIVOS_ESCOPO}
for _i, _rel in enumerate(_ARQUIVOS_EXCLUIDOS_NOMEADOS, start=1):
    check(
        _rel not in _RESOLVIDOS_POSIX,
        f"E3{_i} {_rel}: NAO esta na resolucao real de hoje (Path.glob)",
    )


# === GRUPO R - varredura REAL do repositorio (a guarda em producao) =======
# Isto e a guarda de fato: roda contra os arquivos reais resolvidos acima e
# falha se algum tiver uso decisorio de intent. Hoje (Fase 4, antes do
# primeiro consumidor real de AIInterpretationResult.intent) o esperado e
# ZERO violacoes nos 3 globs.
_TOTAL_VIOLACOES_REAIS = 0
_ARQUIVOS_COM_ERRO_PARSE = []
for _i, _arquivo in enumerate(_ARQUIVOS_ESCOPO, start=1):
    _rel = _arquivo.relative_to(ROOT).as_posix()
    try:
        _codigo = _arquivo.read_text(encoding="utf-8")
        _arvore = ast.parse(_codigo, filename=_rel)
    except (SyntaxError, OSError) as exc:
        # Defensivo: outros agentes editam concorrentemente arquivos deste
        # escopo (ver contexto da tarefa) - um erro de leitura/parse aqui e
        # relatado como falha clara, nunca mascarado nem deixado estourar cru.
        _ARQUIVOS_COM_ERRO_PARSE.append(_rel)
        check(False, f"R{_i:02d} {_rel}: falha ao ler/parsear ({type(exc).__name__}: {exc})")
        continue
    _viol = _varrer_arvore(_arvore)
    _TOTAL_VIOLACOES_REAIS += len(_viol)
    check(_viol == [], f"R{_i:02d} {_rel}: sem uso decisorio de intent (violacoes={_viol})")

check(
    _ARQUIVOS_COM_ERRO_PARSE == [],
    f"R98 nenhum arquivo do escopo falhou o parse ({len(_ARQUIVOS_COM_ERRO_PARSE)} com erro)",
)
check(
    _TOTAL_VIOLACOES_REAIS == 0,
    f"R99 nenhuma violacao de R-INTENT em nenhum dos {len(_ARQUIVOS_ESCOPO)} "
    f"arquivos varridos hoje (total={_TOTAL_VIOLACOES_REAIS})",
)


print()
print(f"{len(executados)} verificacoes executadas.")
if failures:
    print(f"{len(failures)} verificacao(oes) falharam.")
    sys.exit(1)
print("Todas as verificacoes passaram.")
