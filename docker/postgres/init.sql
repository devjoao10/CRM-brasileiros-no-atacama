-- ============================================================
-- PostgreSQL Init Script — Hardening do Banco de Dados
-- Executado automaticamente na PRIMEIRA vez que o container sobe.
-- ============================================================

-- ===========================================
-- 1. Usuário read-only (para IA / analytics)
--    Pode: APENAS SELECT
--    NÃO pode: nenhuma escrita
-- ===========================================
CREATE USER crm_readonly WITH PASSWORD :'CRM_READONLY_PASSWORD';

GRANT CONNECT ON DATABASE crm_atacama TO crm_readonly;
GRANT USAGE ON SCHEMA public TO crm_readonly;

-- Conceder SELECT em tabelas futuras criadas pelo owner (crm_user)
ALTER DEFAULT PRIVILEGES FOR USER crm_user IN SCHEMA public
    GRANT SELECT ON TABLES TO crm_readonly;

-- Aplicar retroativamente caso tabelas já existam
GRANT SELECT ON ALL TABLES IN SCHEMA public TO crm_readonly;

-- ===========================================
-- 2. Restrições de segurança
-- ===========================================

-- Limitar conexões simultâneas
ALTER USER crm_user CONNECTION LIMIT 30;
ALTER USER crm_readonly CONNECTION LIMIT 5;

-- Revogar permissão de criar objetos para público
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT ALL ON SCHEMA public TO crm_user;
GRANT USAGE ON SCHEMA public TO crm_readonly;

-- Revogar acesso a sequences do readonly
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM crm_readonly;

-- ===========================================
-- 3. Timeouts e proteções por usuário
-- ===========================================

-- Owner (app): queries morrem após 30s
ALTER USER crm_user SET statement_timeout = '30s';

-- Read-only (IA): queries morrem após 10s
ALTER USER crm_readonly SET statement_timeout = '10s';

-- Logging de auditoria
ALTER USER crm_user SET log_statement = 'mod';      -- Loga INSERT/UPDATE/DELETE
ALTER USER crm_readonly SET log_statement = 'all';   -- Loga tudo (auditoria da IA)

-- ===========================================
-- 4. Confirmação
-- ===========================================
DO $$
BEGIN
    RAISE NOTICE '============================================';
    RAISE NOTICE '✅ Hardening do PostgreSQL concluído!';
    RAISE NOTICE '   → crm_user: owner, DML+DDL, max 30 conn, timeout 30s';
    RAISE NOTICE '   → crm_readonly: SELECT only, max 5 conn, timeout 10s';
    RAISE NOTICE '   → PUBLIC: sem permissões no schema public';
    RAISE NOTICE '============================================';
END
$$;
