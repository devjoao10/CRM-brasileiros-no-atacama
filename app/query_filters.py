# -*- coding: utf-8 -*-
"""
Predicados de filtro compartilhados entre routers.

Existe por um motivo so: o filtro de campo personalizado validado no PR #28
(segments.py) precisa valer identico em leads.py. Copiar as duas dezenas de
linhas de SQL dialect-aware seria garantir que as duas copias divergissem.

Mantenha este modulo pequeno e sem dependencia de router. So predicados puros.
"""
from sqlalchemy import String, case, cast, func, literal, select
from sqlalchemy.dialects.postgresql import JSONB

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


# AUDIT-2026-08-WG (F-043) — a sequencia de escape de NUL, montada em runtime.
# Escrever `\u0000` literal num .py produziria um byte NUL de verdade no
# fonte; o que precisamos e dos SEIS caracteres que o texto JSON guarda.
_ESCAPE_NUL = chr(92) + "u0000"
_LIKE_ESCAPE_NUL = "%" + _ESCAPE_NUL + "%"


def campo_personalizado_match(coluna, chave: str, valor: str):
    """
    EXISTS sobre os pares chave/valor de um campo JSON de objeto, no banco.

    Antes disto o filtro rodava em Python: carregava todo Lead que passasse nos
    demais criterios e varria o dict. Com 19 mil leads eram 19 mil objetos ORM.

    Espelha exatamente a comparacao que era feita em Python:
      chave  -> strip + lower dos DOIS lados (a chave crua pode vir " Origem ")
      valor  -> substring case-insensitive; vazio = exige so a presenca da chave

    O CASE blinda contra JSON legado que nao seja um objeto: jsonb_each_text
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
        # jsonb_each_text expande os pares e devolve o valor ja como texto —
        # "Atacama", 25 e true viram 'Atacama', '25' e 'true', igual ao str()
        # que o Python fazia.
        #
        # AUDIT-2026-08-WG (F-043) — a ORDEM aqui e o defeito, nao o CASE.
        #
        # `cast(coluna, JSONB)` estava FORA do CASE, entao era avaliado para
        # TODA linha antes de qualquer guard. A coluna e `json` (texto), que
        # aceita a sequencia de escape de NUL; `jsonb` NAO aceita. Uma unica
        # linha legada com esse escape fazia a query inteira estourar
        # `UntranslatableCharacter: unsupported Unicode escape sequence` — e o
        # filtro de campo personalizado, mais todo segmento que o use, virava
        # 500 permanente para TODOS os leads, nao so para o envenenado.
        #
        # Reproduzido no PostgreSQL 16 real: `'{"origem":"\\u0000x"}'::json` passa,
        # `::json::jsonb` falha, e o mesmo dado numa linha derruba a consulta.
        # Com o guard de TEXTO antes do cast, a consulta volta a responder e
        # devolve as linhas boas.
        #
        # A linha envenenada fica invisivel para o filtro (vira `{}`). E a troca
        # certa: uma linha some de um filtro, em vez de a funcionalidade sumir
        # para todo mundo. Dado NOVO nao entra assim — `_rejeita_nul` em
        # `app/schemas/lead.py` ja recusa na borda; isto e para o legado.
        texto = cast(coluna, String)
        sem_nul = texto.notlike(_LIKE_ESCAPE_NUL)
        jb = cast(coluna, JSONB)
        seguro = case((sem_nul & (func.jsonb_typeof(jb) == "object"), jb),
                      else_=cast(literal("{}"), JSONB))
        pares = func.jsonb_each_text(seguro).table_valued("key", "value")
        chave_col = func.lower(func.btrim(pares.c.key, _ESPACOS))
        valor_col = pares.c.value

    condicoes = [chave_col == chave_norm]
    if valor_norm:
        # autoescape: % e _ digitados pelo usuario sao literais, nao curinga
        condicoes.append(func.lower(valor_col).contains(valor_norm, autoescape=True))
    return select(literal(1)).select_from(pares).where(*condicoes).exists()
