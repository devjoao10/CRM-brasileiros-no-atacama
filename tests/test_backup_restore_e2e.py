# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-W0 — backup/restore ponta a ponta, EXECUTADO.

A auditoria anterior parou em "o script foi corrigido, mas nenhum restore foi
verificado". Este teste transforma isso em evidencia rodada.

Nao ha PostgreSQL nem Docker nesta maquina, e nao precisa haver: o script e um
programa bash cuja UNICA entrada e a saida de `docker exec ... pg_dump`. Entao
o teste poe um `docker` FALSO na frente do PATH, que emite um dump plain-format
realista (blocos `COPY public.<t> ... FROM stdin;` com dados, ~1.6 MB), e
exercita o script INTEIRO — compressao, piso de tamanho, `gzip -t`, presenca de
tabela, guarda de CR, promocao do .tmp, checksum, permissoes, retencao e trap.

O que isto prova: a logica do script. O que NAO prova: que o pg_dump real
produz o que o shim produz, e que o `psql -f` real reconstroi o banco. Isso
continua sendo o teste de restore do operador (ver LIMITACOES no relatorio).

Cenarios: 1 feliz, 2 restore, 3 regressao do -t (CRLF), 4 banco vazio,
5 gzip truncado, 6 retencao, 7 falha no meio do dump.

Rodar:  python tests/test_backup_restore_e2e.py     (exit 0 = passou)
"""
import gzip as gzlib
import hashlib
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "backup_postgres.sh"
SCRATCH = ROOT / "scratch" / "backup_e2e"

falhas = []


def check(cond, msg):
    if cond:
        print(f"  PASS: {msg}")
    else:
        print(f"  FAIL: {msg}")
        falhas.append(msg)


def morrer(msg):
    """Sem ambiente para rodar, o teste REPROVA. Nunca faz skip silencioso."""
    print(f"  FAIL: {msg}")
    falhas.append(msg)
    print(f"\n{len(falhas)} FALHA(S)")
    sys.exit(1)


# --- 0. pre-requisitos: falta de ferramenta REPROVA, nao pula -------------
print("0) pre-requisitos do ambiente")

BASH = shutil.which("bash")
check(BASH is not None, "bash encontrado no PATH")
if BASH is None:
    morrer(
        "bash ausente - este teste exercita um script bash e NAO pode se "
        "auto-desligar. Instale o Git for Windows/WSL ou rode em Linux."
    )
check(SCRIPT.is_file(), "scripts/backup_postgres.sh existe")
if not SCRIPT.is_file():
    morrer("script de backup ausente")

_falta = subprocess.run(
    [BASH, "-c",
     'for c in gzip sha256sum find stat tr cut head; do '
     'command -v "$c" >/dev/null || echo "$c"; done'],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
).stdout.split()
check(not _falta, f"ferramentas POSIX disponiveis (faltando: {_falta})")
if _falta:
    morrer(f"ferramentas ausentes no bash: {_falta}")

REAL_GZIP = subprocess.run(
    [BASH, "-c", "command -v gzip"], capture_output=True, text=True,
    encoding="utf-8", errors="replace",
).stdout.strip()


# --- scratch limpo a cada execucao ----------------------------------------
if SCRATCH.exists():
    shutil.rmtree(SCRATCH, ignore_errors=True)
SCRATCH.mkdir(parents=True, exist_ok=True)
BIN = SCRATCH / "bin"
BIN.mkdir()


# --- gerador de dump plain-format realista --------------------------------
TABELAS = {
    "users": (["id", "email", "hashed_password", "nome", "ativo"], 25),
    "leads": (["id", "nome", "telefone", "destino", "observacao"], 500),
    "conversations": (["id", "lead_id", "status", "canal"], 300),
    "messages": (["id", "conversation_id", "direcao", "corpo"], 8000),
}

_PALAVRAS = [
    "Atacama", "excursao", "Sao Pedro", "geiser", "salar", "reserva",
    "confirmacao", "orcamento", "traslado", "hospedagem", "cambio", "altitude",
]


def _valor(tabela, coluna, i):
    if coluna == "id":
        return str(i)
    if coluna.endswith("_id"):
        return str(1 + (i % 97))
    if coluna == "email":
        return f"pessoa{i}@exemplo.com.br"
    if coluna == "hashed_password":
        return "$2b$12$" + hashlib.sha256(f"{tabela}{i}".encode()).hexdigest()[:31]
    if coluna == "telefone":
        return f"+5511{900000000 + i}"
    if coluna == "ativo":
        return "t" if i % 3 else "f"
    if coluna == "status":
        return ["aberta", "pendente", "fechada"][i % 3]
    if coluna == "canal":
        return ["whatsapp", "instagram", "site"][i % 3]
    if coluna == "direcao":
        return "in" if i % 2 else "out"
    if coluna == "destino":
        return ["Atacama", "Uyuni", "Cusco"][i % 3]
    if coluna == "nome":
        return f"Cliente {i} Da Silva"
    # texto livre com acento - prova que UTF-8 atravessa o gzip intacto
    p = " ".join(_PALAVRAS[(i + k) % len(_PALAVRAS)] for k in range(9))
    return f"Registro {i} \u2014 {p} (sem traducao de linha aqui)"


def gerar_dump():
    partes = [
        "--\n-- PostgreSQL database dump\n--\n\n",
        "-- Dumped from database version 15.6\n",
        "-- Dumped by pg_dump version 15.6\n\n",
        "SET statement_timeout = 0;\nSET client_encoding = 'UTF8';\n",
        "SET standard_conforming_strings = on;\nSET search_path = public;\n\n",
    ]
    contagens = {}
    for tabela, (cols, n) in TABELAS.items():
        partes.append(f"CREATE TABLE public.{tabela} (\n")
        partes.append(",\n".join(f"    {c} text" for c in cols))
        partes.append("\n);\n\n")
        partes.append(f"COPY public.{tabela} ({', '.join(cols)}) FROM stdin;\n")
        for i in range(1, n + 1):
            partes.append("\t".join(_valor(tabela, c, i) for c in cols) + "\n")
        partes.append("\\.\n\n")
        contagens[tabela] = n
    partes.append("--\n-- PostgreSQL database dump complete\n--\n")
    return "".join(partes), contagens


DUMP_TXT, CONTAGENS_ORIGEM = gerar_dump()
DUMP_BYTES = DUMP_TXT.encode("utf-8")
(SCRATCH / "dump_ok.sql").write_bytes(DUMP_BYTES)

# O que o `docker exec -t` produzia: LF->CRLF ANTES do gzip.
(SCRATCH / "dump_crlf.sql").write_bytes(DUMP_BYTES.replace(b"\n", b"\r\n"))

# Banco vazio: SQL valido, grande, mas sem as tabelas centrais.
_vazio = ["--\n-- PostgreSQL database dump\n--\n\nSET client_encoding = 'UTF8';\n\n",
          "CREATE TABLE public.alembic_version (\n    version_num text\n);\n\n",
          "COPY public.alembic_version (version_num) FROM stdin;\n"]
for _i in range(30000):
    _vazio.append(f"m{_i:06d}_migracao_de_teste_com_texto_para_ocupar_espaco\n")
_vazio.append("\\.\n\n--\n-- PostgreSQL database dump complete\n--\n")
(SCRATCH / "dump_vazio.sql").write_bytes("".join(_vazio).encode("utf-8"))

print(f"\n  dump de referencia: {len(DUMP_BYTES)} bytes, "
      f"tabelas={CONTAGENS_ORIGEM}")


# --- shims ----------------------------------------------------------------
(BIN / "docker").write_text(
    "#!/usr/bin/env bash\n"
    "# shim: ignora os argumentos (exec <container> pg_dump ...) e emite o dump.\n"
    'case "${SHIM_MODE:-ok}" in\n'
    '  ok)    cat "$SHIM_DIR/dump_ok.sql" ;;\n'
    '  crlf)  cat "$SHIM_DIR/dump_crlf.sql" ;;\n'
    '  vazio) cat "$SHIM_DIR/dump_vazio.sql" ;;\n'
    '  meio)  head -c 400000 "$SHIM_DIR/dump_ok.sql"; exit 3 ;;\n'
    '  *)     echo "shim: modo desconhecido" >&2; exit 99 ;;\n'
    "esac\n",
    encoding="utf-8", newline="\n",
)
# gzip que trunca a propria saida (disco cheio / escrita interrompida).
# -t e -d sao delegados ao gzip de verdade: o script precisa deles para testar.
(BIN / "gzip").write_text(
    "#!/usr/bin/env bash\n"
    f'REAL="{REAL_GZIP}"\n'
    'for a in "$@"; do case "$a" in -*t*|-*d*) exec "$REAL" "$@";; esac; done\n'
    'f="$(mktemp)"\n'
    '"$REAL" > "$f"\n'
    'n=$(stat -c%s "$f")\n'
    'head -c $(( n - 300 )) "$f"\n'
    'rm -f "$f"\n',
    encoding="utf-8", newline="\n",
)
for _s in ("docker", "gzip"):
    os.chmod(BIN / _s, 0o755)


# --- runner ---------------------------------------------------------------
_PREP = (
    'if command -v cygpath >/dev/null 2>&1; then\n'
    '  export SHIM_DIR="$(cygpath -u "$SHIM_DIR")"\n'
    '  export BACKUP_DIR="$(cygpath -u "$BACKUP_DIR")"\n'
    '  P="$(cygpath -u "$SHIM_BIN")"\n'
    'else P="$SHIM_BIN"; fi\n'
    'export PATH="$P:$PATH"\n'
    'exec bash "$SCRIPT_SH"\n'
)


def rodar(nome, modo="ok", com_gzip_falso=False, retencao="14"):
    destino = SCRATCH / "out" / nome
    # so o PAI e criado aqui: o BACKUP_DIR tem que ser criado pelo proprio
    # script, senao o `chmod 700` dele nao e o que decide a permissao final.
    destino.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "SHIM_MODE": modo,
        "SHIM_DIR": str(SCRATCH),
        "SHIM_BIN": str(BIN),
        "SCRIPT_SH": str(SCRIPT).replace("\\", "/"),
        "BACKUP_DIR": str(destino),
        "RETENTION_DAYS": retencao,
        "POSTGRES_CONTAINER": "container_falso",
        "POSTGRES_DB": "crm_atacama",
        "POSTGRES_USER": "crm_user",
    })
    if not com_gzip_falso:
        # o gzip falso so existe para o cenario 5; nos outros ele sai da frente
        (BIN / "gzip").rename(BIN / "gzip.off")
    try:
        r = subprocess.run(
            [BASH, "-c", _PREP], env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=300,
        )
    finally:
        if not com_gzip_falso:
            (BIN / "gzip.off").rename(BIN / "gzip")
    return r, destino


def listar(d, padrao):
    return sorted(p.name for p in d.glob(padrao))


def perms(caminho):
    r = subprocess.run(
        [BASH, "-c",
         'if command -v cygpath >/dev/null 2>&1; then p="$(cygpath -u "$1")"; '
         'else p="$1"; fi; stat -c%a "$p"', "_", str(caminho)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return r.stdout.strip()


def suporta_permissao():
    """O sistema de arquivos do BACKUP_DIR PERSISTE permissao POSIX?

    No Windows nao necessariamente: com o drive montado `noacl` (padrao do Git
    Bash), `chmod` retorna 0 e o `stat` do MESMO processo devolve 700/600 — mas
    outro processo le 755/644, porque nada foi gravado. Por isso a sonda grava
    num processo e LE EM OUTRO, igual ao que a assercao de verdade faz. Sondar
    dentro do mesmo processo mede a ilusao, nao o disco.
    """
    alvo = SCRATCH / "perm"
    subprocess.run(
        [BASH, "-c",
         'if command -v cygpath >/dev/null 2>&1; then d="$(cygpath -u "$1")"; '
         'else d="$1"; fi; umask 077; rm -rf "$d"; mkdir -p "$d/dir"; '
         ': > "$d/f"; chmod 700 "$d/dir"; chmod 600 "$d/f"', "_", str(alvo)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    lidos = [perms(alvo / "dir"), perms(alvo / "f")]
    return lidos == ["700", "600"], lidos


PERM_OK, PERM_SONDA = suporta_permissao()
print(f"  sonda de permissao (grava num processo, le em outro): {PERM_SONDA} -> "
      f"{'chmod persiste aqui' if PERM_OK else 'chmod NAO persiste aqui (mount noacl)'}")


def saida(r, n=4):
    linhas = [l for l in (r.stdout + r.stderr).splitlines() if l.strip()]
    return " | ".join(linhas[-n:])


# --- 1. caminho feliz -----------------------------------------------------
print("\n1) caminho feliz - dump valido com as 4 tabelas centrais")
r1, dir1 = rodar("feliz")
print(f"     rc={r1.returncode}  saida: {saida(r1)}")
check(r1.returncode == 0, "exit 0")
gzs = listar(dir1, "*.sql.gz")
check(len(gzs) == 1, f"exatamente um .sql.gz criado (achado: {gzs})")
check(not listar(dir1, "*.tmp"), "nenhum .tmp deixado para tras")
check("[backup] OK" in r1.stdout, "imprime a linha de sucesso com tamanho e checksum")

BKP = dir1 / gzs[0] if gzs else None
if BKP:
    print(f"     backup: {BKP.name}  {BKP.stat().st_size} bytes")
    sha = dir1 / (BKP.name + ".sha256")
    check(sha.is_file(), ".sha256 gravado ao lado do dump")
    if sha.is_file():
        conteudo = sha.read_text(encoding="utf-8").strip()
        digest, _, caminho = conteudo.partition(" ")
        caminho = caminho.strip().lstrip("*")
        check(caminho == BKP.name,
              f"checksum usa caminho RELATIVO ('{caminho}', nao absoluto)")
        check(digest == hashlib.sha256(BKP.read_bytes()).hexdigest(),
              "digest do .sha256 confere com o arquivo")
        # a prova operacional: `sha256sum -c` roda de dentro do diretorio
        v = subprocess.run(
            [BASH, "-c",
             'if command -v cygpath >/dev/null 2>&1; then d="$(cygpath -u "$1")"; '
             'else d="$1"; fi; cd "$d" && sha256sum -c "$2"',
             "_", str(dir1), sha.name],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        check(v.returncode == 0,
              "`sha256sum -c` verifica de dentro do diretorio "
              f"({(v.stdout + v.stderr).strip()[:80]})")
        if PERM_OK:
            check(perms(sha) == "600",
                  f"checksum com permissao 0600 (obtido: {perms(sha)})")
    if PERM_OK:
        check(perms(BKP) == "600", f"dump com permissao 0600 (obtido: {perms(BKP)})")
        check(perms(dir1) == "700",
              f"diretorio com permissao 0700 (obtido: {perms(dir1)})")
    else:
        # Aqui o teto e da plataforma, nao do script: a sonda acima provou que
        # nem um `chmod 600` explicito gruda neste mount. Entao verifica-se que
        # o script CONTINUA mandando fechar — e o dump nao esta mais aberto que
        # o proprio arquivo de controle da sonda.
        fonte = SCRIPT.read_text(encoding="utf-8")
        check("umask 077" in fonte
              and 'chmod 600 "${out}"' in fonte
              and 'chmod 600 "${out}.sha256"' in fonte
              and 'chmod 700 "${BACKUP_DIR}"' in fonte,
              "script mantem umask 077 + chmod 600/700 (permissao real NAO "
              f"verificavel neste mount: sonda deu {PERM_SONDA}, esperado "
              "['700', '600'] — na VPS Linux ela vale)")
        check(perms(BKP) == (PERM_SONDA[1] if len(PERM_SONDA) > 1 else "?"),
              f"dump tao fechado quanto o teto da plataforma permite "
              f"(dump={perms(BKP)}, teto={PERM_SONDA})")


# --- 2. restore -----------------------------------------------------------
print("\n2) restore - descompressao byte a byte + carga em SQLite")
if BKP:
    restaurado = gzlib.decompress(BKP.read_bytes())
    check(restaurado == DUMP_BYTES,
          f"bytes restaurados identicos ao que o pg_dump emitiu "
          f"({len(restaurado)} vs {len(DUMP_BYTES)} bytes)")
    _crs = restaurado.count(b"\r")
    check(_crs == 0, f"nenhum CR introduzido no caminho (achados: {_crs})")

    # "restore" de verdade: interpreta os blocos COPY e materializa em SQLite
    con = sqlite3.connect(":memory:")
    contagens, amostra = {}, {}
    linhas = restaurado.decode("utf-8").split("\n")
    i = 0
    while i < len(linhas):
        if linhas[i].startswith("COPY "):
            cab = linhas[i]
            tabela = cab.split()[1].split(".")[-1]
            cols = [c.strip() for c in cab[cab.index("(") + 1:cab.index(")")].split(",")]
            colunas_sql = ", ".join('"' + c + '"' for c in cols)
            con.execute(f'CREATE TABLE "{tabela}" ({colunas_sql})')
            i += 1
            n = 0
            while i < len(linhas) and linhas[i] != "\\.":
                vals = linhas[i].split("\t")
                if len(vals) != len(cols):
                    check(False, f"{tabela}: linha {n + 1} com {len(vals)} colunas, "
                                 f"esperado {len(cols)}")
                    break
                con.execute(
                    f'INSERT INTO "{tabela}" VALUES ({", ".join("?" * len(cols))})', vals
                )
                if n == 0:
                    amostra[tabela] = vals
                n += 1
                i += 1
            contagens[tabela] = n
        i += 1

    for tabela, esperado in CONTAGENS_ORIGEM.items():
        obtido = con.execute(f'SELECT count(*) FROM "{tabela}"').fetchone()[0]
        check(obtido == esperado and contagens.get(tabela) == esperado,
              f"{tabela}: {obtido} linhas restauradas, esperado {esperado}")

    sujas = 0
    for tabela, (cols, _n) in TABELAS.items():
        for c in cols:
            sujas += con.execute(
                f'SELECT count(*) FROM "{tabela}" WHERE "{c}" LIKE ?', ("%\r%",)
            ).fetchone()[0]
    check(sujas == 0, f"nenhum valor com CR apos o restore ({sujas} encontrados)")

    # a ULTIMA coluna de cada linha e a que o defeito original sujava
    ult = con.execute('SELECT corpo FROM "messages" WHERE id = ?', ("1",)).fetchone()
    check(ult is not None and ult[0] == _valor("messages", "corpo", 1),
          "ultima coluna da 1a mensagem intacta (era ela que ganhava o CR)")
    check(amostra.get("users", [None, None])[0] == "1"
          and amostra["users"][1] == "pessoa1@exemplo.com.br",
          "conteudo real conferido linha a linha (users.id=1)")
else:
    check(False, "sem backup do cenario 1, restore nao pode ser verificado")


# --- 3. regressao do defeito original -------------------------------------
print("\n3) regressao do -t - dump com CRLF DEVE abortar")
r3, dir3 = rodar("crlf", modo="crlf")
print(f"     rc={r3.returncode}  saida: {saida(r3)}")
check(r3.returncode != 0, f"aborta com exit != 0 (obtido: {r3.returncode})")
# "CR" solto casaria com o "CRM" do caminho do repositorio — e ja casou numa
# rodada em que o script abortou pelo motivo ERRADO. Exige a frase inteira.
check("CR encontrado" in (r3.stderr + r3.stdout),
      "aborta pela guarda de CR, nao por outro motivo qualquer")
check(not listar(dir3, "*.sql.gz"),
      f"NENHUM .sql.gz corrompido promovido (achado: {listar(dir3, '*.sql.gz')})")
check(not listar(dir3, "*.tmp"), "trap removeu o parcial")


# --- 4. banco vazio -------------------------------------------------------
print("\n4) dump sem as tabelas centrais - DEVE abortar")
r4, dir4 = rodar("vazio", modo="vazio")
print(f"     rc={r4.returncode}  saida: {saida(r4)}")
check(r4.returncode != 0, f"aborta com exit != 0 (obtido: {r4.returncode})")
check("tabela" in (r4.stderr + r4.stdout),
      "a mensagem de erro nomeia a tabela ausente")
check(not listar(dir4, "*.sql.gz"), "nenhum arquivo promovido a backup")
check(not listar(dir4, "*.tmp"), "trap removeu o parcial")


# --- 5. gzip truncado -----------------------------------------------------
print("\n5) gzip truncado - DEVE abortar em `gzip -t`")
r5, dir5 = rodar("gztrunc", com_gzip_falso=True)
print(f"     rc={r5.returncode}  saida: {saida(r5)}")
check(r5.returncode != 0, f"aborta com exit != 0 (obtido: {r5.returncode})")
check("gzip" in (r5.stderr + r5.stdout).lower(),
      "a mensagem de erro aponta o gzip invalido")
check(not listar(dir5, "*.sql.gz"), "nenhum truncado promovido a backup")
check(not listar(dir5, "*.tmp"), "trap removeu o parcial")


# --- 6. retencao ----------------------------------------------------------
print("\n6) retencao - poda so depois de um backup verificado")
VELHO = time.time() - 30 * 86400
RECENTE = time.time() - 2 * 86400


def semear(destino):
    destino.mkdir(parents=True, exist_ok=True)
    for nome, quando in (("bna_postgres_20260701_030000.sql.gz", VELHO),
                         ("bna_postgres_20260701_030000.sql.gz.sha256", VELHO),
                         ("bna_postgres_20260822_030000.sql.gz", RECENTE),
                         ("bna_postgres_20260822_030000.sql.gz.sha256", RECENTE)):
        p = destino / nome
        p.write_bytes(b"placeholder de auditoria\n")
        os.utime(p, (quando, quando))


semear(SCRATCH / "out" / "retencao")
r6, dir6 = rodar("retencao")
restantes = listar(dir6, "*")
print(f"     rc={r6.returncode}  restantes: {restantes}")
check(r6.returncode == 0, "backup novo concluiu")
check("bna_postgres_20260701_030000.sql.gz" not in restantes,
      "backup de 30 dias podado (RETENTION_DAYS=14)")
check("bna_postgres_20260701_030000.sql.gz.sha256" not in restantes,
      "checksum antigo tambem podado")
check("bna_postgres_20260822_030000.sql.gz" in restantes,
      "backup de 2 dias PRESERVADO")
novos = [n for n in restantes
         if n.endswith(".sql.gz") and n != "bna_postgres_20260822_030000.sql.gz"]
check(len(novos) == 1 and (dir6 / novos[0]).stat().st_size > 1024,
      f"o backup recem-criado NAO foi apagado pela poda ({novos})")

semear(SCRATCH / "out" / "retencao_abortada")
r6b, dir6b = rodar("retencao_abortada", modo="vazio")
sobrou = listar(dir6b, "*")
print(f"     rc(abortado)={r6b.returncode}  restantes: {sobrou}")
check(r6b.returncode != 0, "execucao com dump ruim aborta")
check("bna_postgres_20260701_030000.sql.gz" in sobrou,
      "backup antigo NAO foi podado quando o dump falhou "
      "(a poda so roda depois de um backup verificado)")


# --- 7. falha no meio do dump ---------------------------------------------
print("\n7) pg_dump falha no meio - nenhum parcial fica no diretorio")
r7, dir7 = rodar("meio", modo="meio")
print(f"     rc={r7.returncode}  saida: {saida(r7)}")
check(r7.returncode != 0, f"o script propaga a falha (obtido: {r7.returncode})")
check(not listar(dir7, "*.sql.gz"),
      f"nenhum .sql.gz parcial (achado: {listar(dir7, '*.sql.gz')})")
check(not listar(dir7, "*.tmp"), "trap limpou o .tmp")
check(not listar(dir7, "*.sha256"), "nenhum checksum de arquivo inexistente")


print()
if falhas:
    print(f"{len(falhas)} FALHA(S):")
    for f in falhas:
        print(f"  - {f}")
    sys.exit(1)
print("OK: backup gerado, restaurado e validado; 7 cenarios exercitados")
