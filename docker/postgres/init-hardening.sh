#!/bin/bash
# ============================================================
# Entrypoint customizado: executa o init.sql com variáveis de senha
# Rodado pelo PostgreSQL na PRIMEIRA inicialização.
# ============================================================
set -e

echo "🔒 Executando hardening do banco de dados..."

# Executar o init.sql passando a senha via variável psql
psql -v ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    -v CRM_READONLY_PASSWORD="'${CRM_READONLY_PASSWORD:-crm_readonly_secret}'" \
    -f /docker-entrypoint-initdb.d/01-init.sql

echo "✅ Hardening concluído!"
