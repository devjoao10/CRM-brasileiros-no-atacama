# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-W0 — regressoes de tres defeitos achados na auditoria global.

1. app/query_filters.py `_ESPACOS` afirmava, em comentario, ser "o mesmo
   conjunto que str.strip() remove". Nao era: faltavam NBSP, \\x1c-\\x1f, \\x85
   e o bloco U+2000. O lado Python normalizava com .strip() e o lado SQL com
   trim()/btrim(_ESPACOS), entao uma chave colada do Excel/Word/WhatsApp com
   NBSP na borda ficava PERMANENTEMENTE impossivel de filtrar — sem erro.

2. app/schemas/lead.py aceitava \\u0000 dentro dos campos JSON do lead. A coluna
   e `Column(JSON)` (tipo `json` no PostgreSQL, que aceita NUL), mas
   query_filters faz cast(coluna, JSONB) em toda linha — e `jsonb` REJEITA NUL.
   Uma unica linha envenenada derrubava com 500 a listagem de leads e todo
   segmento com campo personalizado, para todos os usuarios.

3. scripts/backup_postgres.sh usava `docker exec -t`. O pseudo-TTY traduz
   LF->CRLF ANTES do gzip, entao TODO dump ja feito esta byte-corrompido: o
   restore funciona e suja em silencio a ultima coluna de cada linha COPY.

Rodar:  python tests/test_filter_normalization_and_backup.py
"""
import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

falhas = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        falhas.append(msg)


# ─── 1. _ESPACOS bate exatamente com str.strip() ─────────────────────
print("1) app/query_filters.py — simetria de normalizacao de espaco")

fonte = (ROOT / "app" / "query_filters.py").read_text(encoding="utf-8")
m = re.search(r"_ESPACOS = \((.*?)\n\)", fonte, re.S)
check(m is not None, "_ESPACOS continua declarado como literal inspecionavel")

if m:
    literal = ast.literal_eval("(" + m.group(1) + "\n)")
    if isinstance(literal, tuple):
        literal = "".join(literal)
    declarado = set(literal)
    # Exatamente o conjunto que str.strip() remove das bordas.
    esperado = {chr(c) for c in range(0x11000) if chr(c).isspace()}

    faltando = sorted(esperado - declarado)
    check(
        not faltando,
        "_ESPACOS cobre todo caractere que str.strip() remove "
        f"(faltando: {[hex(ord(c)) for c in faltando]})",
    )
    sobrando = sorted(declarado - esperado)
    check(
        not sobrando,
        "_ESPACOS nao remove nada que str.strip() preserve "
        f"(extra: {[hex(ord(c)) for c in sobrando]})",
    )
    # O caso concreto que motivou o finding.
    check("\xa0" in declarado, "NBSP (\\xa0) — o que um paste humano produz — e removido")

# O comentario nao pode voltar a mentir sem que alguem note.
check(
    "str.strip()" in fonte and "NBSP" in fonte,
    "o comentario documenta a simetria e cita o NBSP",
)


# ─── 2. NUL rejeitado nos campos JSON do lead ────────────────────────
print("\n2) app/schemas/lead.py — NUL nao entra nos campos JSON")

import os

os.environ.setdefault("ENVIRONMENT", "development")
from app.schemas.lead import LeadBase, LeadUpdate  # noqa: E402

NUL = "\x00"

benigno = LeadBase(nome="Teste", campos_personalizados={"origem": "Indicacao"})
check(
    benigno.campos_personalizados == {"origem": "Indicacao"},
    "dict benigno continua aceito (a guarda nao bloqueia o caminho normal)",
)

for modelo in (LeadBase, LeadUpdate):
    nome = modelo.__name__
    for descricao, payload in (
        ("valor", {"a": f"b{NUL}c"}),
        ("chave", {f"k{NUL}": "v"}),
        ("aninhado", {"a": {"b": [f"x{NUL}"]}}),
    ):
        try:
            modelo(nome="Teste", campos_personalizados=payload)
            check(False, f"{nome}: NUL no {descricao} deve ser recusado")
        except Exception:
            check(True, f"{nome}: NUL no {descricao} recusado")

# datas_destinos e dias_por_destino compartilham o mesmo validator.
try:
    LeadBase(nome="Teste", datas_destinos={"Atacama": {"chegada": f"2026-01-01{NUL}"}})
    check(False, "datas_destinos: NUL recusado")
except Exception:
    check(True, "datas_destinos: NUL recusado")


# ─── 3. o backup nao pode voltar a corromper o dump ──────────────────
print("\n3) scripts/backup_postgres.sh — integridade do dump")

sh = (ROOT / "scripts" / "backup_postgres.sh").read_text(encoding="utf-8")

# A regressao literal: `docker exec -t` traduz LF->CRLF no pipe.
check(
    not re.search(r"docker exec\s+(-\w*t\w*\s)", sh),
    "docker exec NAO usa -t (pseudo-TTY corrompe o dump com CRLF)",
)
check("docker exec " in sh and "pg_dump" in sh, "o dump continua sendo feito via docker exec")

check("umask 077" in sh, "umask 077 antes de criar o diretorio (dump tem PII e hash de senha)")
check("chmod 600" in sh, "o dump e o checksum ficam 0600")
check("gzip -t" in sh, "o gzip e testado de fato, nao so medido")
check(
    re.search(r"for tabela in .*users.*leads", sh) is not None,
    "o conteudo do dump e verificado (um dump de banco vazio nao passa mais)",
)
check("trap cleanup EXIT" in sh, "trap remove o parcial em qualquer saida anormal")
check('mv "${tmp}" "${out}"' in sh, "so vira backup depois de verificado (escreve em .tmp)")
check(
    "cd \"${BACKUP_DIR}\" && sha256sum" in sh,
    "checksum gravado com caminho relativo (sha256sum -c utilizavel)",
)
# A poda so pode rodar depois das verificacoes acima.
pos_mv = sh.find('mv "${tmp}" "${out}"')
pos_find = sh.find("find \"${BACKUP_DIR}\"")
check(
    pos_mv != -1 and pos_find != -1 and pos_mv < pos_find,
    "a retencao so poda DEPOIS de um backup verificado existir",
)


print()
if falhas:
    print(f"{len(falhas)} FALHA(S)")
    sys.exit(1)
print("OK: normalizacao de filtro, guarda de NUL e integridade de backup")
