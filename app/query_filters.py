# -*- coding: utf-8 -*-
"""
Predicados de filtro compartilhados entre routers.

Existe por um motivo so: o filtro de campo personalizado validado no PR #28
(segments.py) precisa valer identico em leads.py. Copiar as duas dezenas de
linhas de SQL dialect-aware seria garantir que as duas copias divergissem.

Mantenha este modulo pequeno e sem dependencia de router. So predicados puros.
"""
from sqlalchemy import String, case, cast, func, literal, select
from sqlalchemy.dialects.postgresql import JSON

from app.database import IS_SQLITE

# AUDIT-2026-08-W0 — este conjunto PRECISA bater com o que str.strip() remove.
#
# Antes eram so os seis ASCII (" \t\n\r\v\f") sob um comentario afirmando ser "o
# mesmo conjunto que str.strip() remove". Nao era: str.strip() remove todo
# caractere com isspace() True, o que inclui NBSP (\xa0), \x1c-\x1f, \x85 e os
# separadores Unicode do bloco U+2000. A assimetria era invisivel ate alguem
# colar uma chave do Excel/Word/WhatsApp: o TERMO DE BUSCA perdia o NBSP no
# .strip() do Python, a CHAVE ARMAZENADA nao perdia no trim() do SQL, e a
# comparacao " origem" == "origem" dava False — aquela chave virava
# permanentemente impossivel de filtrar, sem erro nenhum.
#
# NBSP na borda e exatamente o que um paste humano produz, entao este era o
# caso comum, nao o exotico. trim()/btrim() operam sobre CARACTERES (nao bytes)
# nos dois dialetos, entao os codepoints multibyte abaixo funcionam nos dois.
_ESPACOS = (
    " \t\n\r\v\f"
    "\x1c\x1d\x1e\x1f\x85\xa0"
    "            "
    "    　"
)


# AUDIT-2026-08-WF2 — pecas do guard de conversibilidade (ver o ramo PostgreSQL).
#
# `_ESCAPE_BARRA` sao os DOIS caracteres com que o texto JSON guarda uma barra
# literal; `_ESCAPE_U` sao os DOIS que abrem qualquer escape de codepoint.
# Montados com chr(92) porque escrever a barra literal aqui num .py e exatamente
# a ambiguidade que ja produziu duas correcoes erradas neste arquivo.
_BARRA = chr(92)
_ESCAPE_BARRA = _BARRA * 2
_ESCAPE_U = _BARRA + "u"

# Os escapes de codepoint que o PostgreSQL SABE converter para `text` e que
# portanto precisam sobreviver ao guard. Sao expressoes regulares POSIX (ARE)
# enviadas como parametro — a barra dobrada e a do REGEX, nao a de SQL.
#   1. par substituto valido (D800-DBFF seguido de DC00-DFFF): e assim que o
#      json.dumps do Python grava emoji fora do BMP, e converte sem problema.
#   2. qualquer outro escape de 4 digitos que nao seja 0000 nem metade solta
#      de par substituto.
_RE_PAR_SUBSTITUTO = (
    r"\\u[dD][89abAB][0-9a-fA-F]{2}"
    r"\\u[dD][c-fC-F][0-9a-fA-F]{2}"
)
_RE_ESCAPE_CONVERSIVEL = r"\\u(?!0000)(?![dD][89a-fA-F])[0-9a-fA-F]{4}"


def campo_personalizado_match(coluna, chave: str, valor: str):
    """
    EXISTS sobre os pares chave/valor de um campo JSON de objeto, no banco.

    Antes disto o filtro rodava em Python: carregava todo Lead que passasse nos
    demais criterios e varria o dict. Com 19 mil leads eram 19 mil objetos ORM.

    Espelha exatamente a comparacao que era feita em Python:
      chave  -> strip + lower dos DOIS lados (a chave crua pode vir " Origem ")
      valor  -> substring case-insensitive; vazio = exige so a presenca da chave

    O CASE blinda contra JSON legado que nao seja um objeto: json_each_text
    estoura em lista/escalar e derrubaria a listagem inteira com 500. Sem
    objeto nao ha chave — resultado vazio, como antes.
    """
    chave_norm = (chave or "").strip().lower()
    valor_norm = (valor or "").strip().lower()

    if IS_SQLITE:
        seguro = case((func.json_type(coluna) == "object", coluna), else_=literal("{}"))
        pares = func.json_each(seguro).table_valued("key", "value", "type")
        chave_col = func.lower(func.trim(pares.c.key, _ESPACOS))
        # json_each devolve 1/0 para booleano JSON, enquanto o Python via
        # "True"/"False". A coluna `type` traz 'true'/'false' e recupera a
        # paridade — senao um campo booleano deixaria de ser encontrado.
        valor_col = case((pares.c["type"] == "true", literal("true")),
                         (pares.c["type"] == "false", literal("false")),
                         else_=pares.c.value)
    else:
        # @> nao serve aqui: a chave e comparada normalizada, nao literal.
        # json_each_text expande os pares e devolve o valor ja como texto —
        # "Atacama", 25 e true viram 'Atacama', '25' e 'true', igual ao str()
        # que o Python fazia.
        #
        # AUDIT-2026-08-WF2 — SEM CAST PARA jsonb, e guard que FALHA FECHANDO.
        #
        # Historico: a coluna e `json` (validada so na sintaxe) e o filtro
        # castava CADA linha para `jsonb`. Uma unica linha que nao castasse
        # derrubava a query INTEIRA, para TODOS os leads (F-043). As duas
        # rodadas anteriores atacaram isso ENUMERANDO o texto que fazia o cast
        # falhar — primeiro com LIKE, depois com strpos atras do escape de NUL.
        # Medido contra PostgreSQL 16.14, enumerar perde: `{"a": 1e1000000}` e
        # `json` valido, passa por qualquer guard de NUL e faz o cast estourar
        # com NumericValueOutOfRange. E um escape de substituto solto e uma
        # terceira causa, igualmente armazenavel.
        #
        # Duas mudancas, nesta ordem:
        #
        # 1. O CAST SUMIU. `json_typeof` e `json_each_text` operam direto na
        #    coluna `json`. Medido: json_each_text devolve 1e1000000 como texto,
        #    sem converter para numeric — a classe inteira de falha de conversao
        #    numerica deixa de existir, nao por reconhece-la, mas porque a
        #    operacao que falhava nao e mais executada.
        #
        # 2. O QUE SOBRA e sempre a mesma coisa: virar um escape de codepoint em
        #    `text`. Medido, so `::text` e `json_typeof` sobrevivem a tudo; TODA
        #    funcao que olha o conteudo (->, ->>, json_each, json_object_keys,
        #    json_each_text) des-escapa avidamente e estoura igual. E como a ORM
        #    grava com json.dumps/ensure_ascii, TODO acento do banco ja e um
        #    escape desses — barrar "escape" em bloco sumiria com meio CRM.
        #    Entao o guard e um ALLOWLIST: remove do texto os escapes que
        #    sabemos converter (par substituto valido, e qualquer outro de 4
        #    digitos que nao seja 0000 nem metade solta de par) e exige que NAO
        #    SOBRE nenhum. O que ele nao reconhecer fica, e a linha e descartada.
        #
        # A diferenca para enumerar: aqui o caso desconhecido FALHA FECHANDO. Se
        # uma versao futura do PostgreSQL recusar um escape que hoje converte, o
        # pior que acontece e a linha sumir do filtro — nunca 500 para todo
        # mundo. Enumerar defeito a defeito falha ABRINDO, que e o que produziu
        # o F-043 e as duas correcoes incompletas.
        #
        # `strpos`, e nao LIKE: no PostgreSQL a barra e o caractere de escape
        # DEFAULT do LIKE, e foi assim que a revisao 1 acabou barrando
        # `ref-u0000-alpha`, que nunca teve barra nenhuma. `strpos` procura a
        # substring literal, sem semantica de escape para errar.
        #
        # Validado contra PostgreSQL 16.14 real, corpus adversarial (NUL em
        # chave/valor/aninhado, 1 a 4 barras antes do escape, substituto solto e
        # par valido, hex maiusculo, caminho do Windows, overflow numerico,
        # array/null/string no topo): zero linha insegura, zero falso positivo.
        texto = cast(coluna, String)
        sem_barra_escapada = func.replace(texto, _ESCAPE_BARRA, "")
        sem_par = func.regexp_replace(sem_barra_escapada, _RE_PAR_SUBSTITUTO, "", "g")
        so_estranho = func.regexp_replace(sem_par, _RE_ESCAPE_CONVERSIVEL, "", "g")
        conversivel = func.strpos(so_estranho, _ESCAPE_U) == 0

        # Sem o cast para jsonb os DOIS lados do AND sao totais (nenhum estoura),
        # entao a ordem que o planner escolher e indiferente e o CASE ANINHADO de
        # que a revisao anterior precisou deixou de ser necessario. O CASE ainda
        # blinda json_each_text contra JSON legado que nao seja objeto: ele
        # estoura em lista/escalar (medido: InvalidParameterValue) e derrubaria a
        # listagem inteira com 500. Sem objeto nao ha chave — resultado vazio.
        vazio = cast(literal("{}"), JSON)
        seguro = case(
            (conversivel & (func.json_typeof(coluna) == "object"), coluna),
            else_=vazio,
        )
        pares = func.json_each_text(seguro).table_valued("key", "value")
        chave_col = func.lower(func.btrim(pares.c.key, _ESPACOS))
        valor_col = pares.c.value

    condicoes = [chave_col == chave_norm]
    if valor_norm:
        # autoescape: % e _ digitados pelo usuario sao literais, nao curinga
        condicoes.append(func.lower(valor_col).contains(valor_norm, autoescape=True))
    return select(literal(1)).select_from(pares).where(*condicoes).exists()
