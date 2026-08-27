#!/usr/bin/env bash
#
# Backup do PostgreSQL — BNA CRM (WP-OPS-01 / doc 37)
#
# Script idempotente de backup lógico (pg_dump) com compressão, checksum,
# retenção e log. NÃO é executado por agentes de IA — é ação humana/cron.
#
# Uso (na VPS, como humano):
#   POSTGRES_CONTAINER=crm_postgres POSTGRES_DB=crm_atacama POSTGRES_USER=crm_user \
#     ./scripts/backup_postgres.sh
#
# Cron sugerido (03:00 diário):
#   0 3 * * * /opt/bna/scripts/backup_postgres.sh >> /var/log/bna_backup.log 2>&1
#
# ⛔ NÃO imprime credenciais. NÃO faz restore. NÃO toca produção sem operador.
#
# ─── AUDIT-2026-08-W0 — por que este script mudou ────────────────────────
# O backup estava CORROMPENDO todo dump silenciosamente, e a retenção apagava
# os bons em cima disso. Quatro defeitos, todos corrigidos aqui:
#
#   1. `docker exec -t` alocava um pseudo-TTY. Um TTY fica em modo canônico
#      com ONLCR ligado, então CADA LF do pg_dump virava CRLF ANTES do gzip.
#      O formato plain emite os dados como blocos `COPY ... FROM stdin`, onde
#      a linha termina o registro — logo todo valor da última coluna ganhava um
#      \r no fim. O restore FUNCIONA e corrompe os dados em silêncio; só se
#      descobre num incidente, tarde demais. `docker exec` sem -t não traduz
#      nada. Esta é a razão de o -t ter sumido.
#   2. A única verificação era um piso de 100 bytes. Um gzip válido de um dump
#      vazio passa folgado — e a retenção (que roda logo abaixo) apagava os
#      backups bons confiando nessa "prova". Agora o gzip é testado de fato e
#      o conteúdo precisa conter as tabelas centrais.
#   3. Sem `umask`, o diretório saía 0755 e o dump 0644 — legível por qualquer
#      conta local, contendo TODO lead, `users.hashed_password` e as API keys.
#   4. Com `set -e`, uma falha no meio do pg_dump abortava o script ANTES do
#      `rm -f`, deixando um .sql.gz truncado indistinguível de um bom para quem
#      pega "o arquivo mais recente". Agora escreve em .tmp e só promove depois
#      de verificar, com trap limpando o lixo em qualquer saída anormal.
#
# NÃO corrigido aqui (ação de operador, fora do escopo desta auditoria):
#   • upload offsite — hoje o backup mora no MESMO host do volume do banco,
#     então perda de host, ransomware ou `docker volume rm` levam os dois.
#   • agendamento — o cron acima é comentário; não há crontab, systemd timer
#     nem sidecar versionado no repositório que prove que isto já rodou.
#   • teste de restore periódico — um backup nunca restaurado não é um backup.
#
# ─── AUDIT-2026-08-W0 (2a passada) — o que a EXECUÇÃO achou ──────────────
# A 1a passada corrigiu o script lendo-o. Ninguém o RODOU. Ao exercitá-lo de
# ponta a ponta com um `docker` falso (tests/test_backup_restore_e2e.py), as
# próprias verificações se mostraram quebradas — ver os comentários adiante:
#   5. `pipefail` + `grep -q`/`head -c`: quem para de ler cedo mata o gzip com
#      SIGPIPE, o pipeline vira 141 e a guarda inverte. A de tabela reprovava
#      TODO dump real; o backup nunca completava.
#   6. `grep -q $'\r'` não enxerga CR nenhum no grep da família Cygwin/MSYS —
#      a guarda de regressão do defeito 1 era decoração fora do Linux.
# Rode o teste antes de mexer aqui:  python tests/test_backup_restore_e2e.py
set -euo pipefail

# Dump com PII e hash de senha: ninguém além do dono lê. Vem antes do mkdir.
umask 077

POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-crm_postgres}"
POSTGRES_DB="${POSTGRES_DB:-crm_atacama}"
POSTGRES_USER="${POSTGRES_USER:-crm_user}"
BACKUP_DIR="${BACKUP_DIR:-/opt/backups/bna-postgres}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

ts="$(date +%Y%m%d_%H%M%S)"
mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}"
out="${BACKUP_DIR}/bna_postgres_${ts}.sql.gz"
tmp="${out}.tmp"

# Qualquer saída anormal remove o parcial. Sem isto, `set -e` deixava um
# truncado no diretório e o próximo operador pegaria ele como "o mais recente".
cleanup() { rm -f "${tmp}"; }
trap cleanup EXIT

echo "[backup] iniciando ${ts} -> ${out}"

# SEM -t: ver defeito 1 no cabeçalho. NÃO reintroduza o -t para "ver o
# progresso" — ele corrompe o dump.
docker exec "${POSTGRES_CONTAINER}" \
  pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" | gzip > "${tmp}"

size=$(stat -c%s "${tmp}" 2>/dev/null || stat -f%z "${tmp}")
if [ "${size}" -lt 1024 ]; then
  echo "[backup][ERRO] arquivo muito pequeno (${size} bytes) — abortando" >&2
  exit 1
fi

# Verificação 1: o gzip descomprime inteiro (pega truncamento e corrupção).
if ! gzip -t "${tmp}"; then
  echo "[backup][ERRO] gzip invalido — abortando" >&2
  exit 1
fi

# ─── AUDIT-2026-08-W0 (2a passada) — por que o pipefail sai daqui ───────
# As verificações 2 e 3 leem o dump com consumidores que param CEDO de
# propósito: `grep -q` sai no primeiro casamento, `head -c` sai em 1 MB.
# Quem para de ler mata o `gzip -dc` a montante com SIGPIPE, e o gzip sai 141.
# Com `pipefail` LIGADO o status do PIPELINE vira 141 mesmo quando a checagem
# deu certo — e as duas guardas invertem de sentido:
#   • a de tabela (`if ! ...`) reprovava TODO dump maior que o buffer do pipe
#     (~64 KB), ou seja, todo dump real: o backup nunca completava, dizendo
#     "tabela 'users' ausente" sobre um dump que continha `COPY public.users`;
#   • a de CR passaria batido justamente no caso que ela existe para pegar.
# O `pipefail` protege o pipeline do DUMP (docker | gzip), onde uma falha do
# pg_dump PRECISA derrubar o script. Aqui ele só atrapalha. Reativado logo
# depois das leituras.
set +o pipefail

# Verificação 2: o dump contém as tabelas centrais. Um dump sintaticamente
# valido de um banco VAZIO passa em tamanho e em gzip -t; esta é a checagem
# que distingue "backup" de "arquivo".
for tabela in users leads conversations messages; do
  if ! gzip -dc "${tmp}" | grep -qE "(CREATE TABLE|COPY) (public\.)?${tabela}\b"; then
    echo "[backup][ERRO] tabela '${tabela}' ausente no dump — abortando" >&2
    exit 1
  fi
done

# Verificação 3: nenhum CR no conteúdo. Guarda de regressão do defeito 1 —
# se alguem reintroduzir o -t, o backup falha ALTO em vez de corromper calado.
# NÃO use `grep -q $'\r'` aqui: o grep da família Cygwin/MSYS descarta o CR
# final de cada linha antes de casar, então ele NUNCA vê justamente o CR que
# o pseudo-TTY produz — a guarda vira decoração em qualquer host que não seja
# Linux. `tr -dc '\r'` conta bytes, sem noção de linha, e vale nos dois.
# o `tr -dc '0-9'` final e por causa do wc do BSD/macOS, que alinha com espacos
crs=$(gzip -dc "${tmp}" | head -c 1048576 | tr -dc '\r' | wc -c | tr -dc '0-9')
if [ "${crs}" -gt 0 ]; then
  echo "[backup][ERRO] CR encontrado no dump (${crs} em 1MB; pseudo-TTY? veja o cabecalho) — abortando" >&2
  exit 1
fi

set -o pipefail

# Só agora vira um backup de verdade.
mv "${tmp}" "${out}"
chmod 600 "${out}"

# Checksum com caminho RELATIVO: com o caminho absoluto, `sha256sum -c` so
# funcionava a partir de / — a verificacao que o arquivo existe para permitir
# era inutilizavel na pratica.
( cd "${BACKUP_DIR}" && sha256sum "$(basename "${out}")" > "$(basename "${out}").sha256" )
chmod 600 "${out}.sha256"
echo "[backup] OK tamanho=${size}B checksum=$(cut -d' ' -f1 "${out}.sha256")"

# Retenção — só poda se sobrar pelo menos um backup VERIFICADO (o desta
# execução já é, porque chegamos até aqui). Antes, a poda rodava mesmo depois
# de um dump ruim ter passado no piso de 100 bytes.
find "${BACKUP_DIR}" -name 'bna_postgres_*.sql.gz' -mtime +"${RETENTION_DAYS}" -delete 2>/dev/null || true
find "${BACKUP_DIR}" -name 'bna_postgres_*.sql.gz.sha256' -mtime +"${RETENTION_DAYS}" -delete 2>/dev/null || true

echo "[backup] concluido. PENDENTE (operador): upload offsite e teste de restore."
