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
set -euo pipefail

POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-crm_postgres}"
POSTGRES_DB="${POSTGRES_DB:-crm_atacama}"
POSTGRES_USER="${POSTGRES_USER:-crm_user}"
BACKUP_DIR="${BACKUP_DIR:-/opt/backups/bna-postgres}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

ts="$(date +%Y%m%d_%H%M%S)"
mkdir -p "${BACKUP_DIR}"
out="${BACKUP_DIR}/bna_postgres_${ts}.sql.gz"

echo "[backup] iniciando ${ts} -> ${out}"
docker exec -t "${POSTGRES_CONTAINER}" \
  pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" | gzip > "${out}"

size=$(stat -c%s "${out}" 2>/dev/null || stat -f%z "${out}")
if [ "${size}" -lt 100 ]; then
  echo "[backup][ERRO] arquivo muito pequeno (${size} bytes) — abortando" >&2
  rm -f "${out}"
  exit 1
fi

sha256sum "${out}" > "${out}.sha256"
echo "[backup] OK tamanho=${size}B checksum=$(cut -d' ' -f1 "${out}.sha256")"

# Retenção
find "${BACKUP_DIR}" -name 'bna_postgres_*.sql.gz' -mtime +"${RETENTION_DAYS}" -delete 2>/dev/null || true
find "${BACKUP_DIR}" -name 'bna_postgres_*.sql.gz.sha256' -mtime +"${RETENTION_DAYS}" -delete 2>/dev/null || true

echo "[backup] concluído. Lembre: validar integridade + upload offsite (B2/S3) antes de confiar."
