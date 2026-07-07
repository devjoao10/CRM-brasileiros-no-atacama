# Checklist de Segurança para Produção — BNA CRM

Estado: ✅ ok · ⚠️ parcial/atenção · ❌ pendente (WP)

## Autenticação / Sessão
- ✅ Senhas com bcrypt (`passlib[bcrypt]`).
- ✅ Cookie JWT `httponly`, `secure` (prod), `samesite=lax`, 8h, `path=/`.
- ✅ `require_admin` aplicado (36 ocorrências em routers).
- ✅ API Keys (n8n) armazenadas como SHA-256 (nunca em claro).
- ⚠️ **SEC-AUTH-01**: JWT também em `localStorage` → exfiltrável por XSS. Migrar para cookie-only + CSRF (WP-SEC-01).
- ✅ **SEC-RL-01 corrigido (WP-SEC-03, `d065628`)**: instância única em `app/limiter.py` usada por `main.py` e `auth.py`; `SlowAPIMiddleware` adicionado (aplica o teto global `200/min` por IP); login `5/minute` preservado. **Provado** com TestClient (`tests/test_rate_limit.py`): login válido=200 e 429 após exceder 5/min. Nota de produção: o teto global de 200/min/IP passa a valer para todas as rotas — monitorar e ajustar se o frontend gerar muitas chamadas por minuto.
- ⚠️ `email_verified` comentado no login (`auth.py:42-46`) — intencional? documentar.

## XSS / Frontend
- ✅ **SEC-XSS-01 corrigido**: chat IA sanitizado com DOMPurify (user + markdown).
- ✅ **SEC-XSS-02 corrigido**: `esc()`/`escapeHtml()` em 10 templates passam a escapar `'` e `"` (fecha quebra de literal em `onclick`).
- ❌ **SEC-XSS-03**: `marked.min.js` sem versão fixa + sem SRI — WP-SEC-02.
- ❌ **SEC-CSP-01**: sem `Content-Security-Policy` (amplifica XSS) — WP-SEC-02 (cuidado com inline JS).
- ✅ Jinja2 autoescape ativo no SSR.

## Headers / Transporte
- ✅ `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`, `Permissions-Policy`.
- ✅ HSTS em produção.
- ✅ CORS restrito em produção (`crm.crmbrasileirosnoatacama.cloud`).
- ✅ TLS via Traefik (letsencrypt) para crm/n8n/conversas.

## API / Autorização
- ✅ Auth unificada (JWT header/cookie ou X-API-Key).
- ⚠️ Verificar IDOR em endpoints de leitura/edição por id (ownership) — WP-SEC/WP-API.
- ⚠️ `/docs` (OpenAPI) só em `ENVIRONMENT=development` (✅ em `main.py`) — confirmar prod com `ENVIRONMENT=production`.

## Webhook / Integrações
- ✅ HMAC no webhook (`conversas/.../webhook.py`: `compare_digest`, `_is_signature_required`, `META_APP_SECRET`).
- ⚠️ **DEVOPS-03**: n8n exposto publicamente via Traefik (DEC-04) — confirmar auth nativa do n8n.

## Infra / Docker
- ✅ Postgres sem porta externa (`expose: 5432`), healthcheck, hardening init.
- ✅ User Postgres read-only dedicado à IA (apenas SELECT).
- ✅ Segredos via env obrigatório (`${VAR:?...}`); `.env` não versionado.
- ❌ **DEVOPS-01**: CRM publica `8000:8000` no host (bypassa TLS do Traefik) — remover (WP-OPS-01).
- ⚠️ **DEVOPS-02**: `N8N_RESTRICT_ENVIRONMENT_VARIABLES_ACCESS=false`.

## Banco / Backup
- ❌ Backup automático não confirmado no repo (DB-03) — WP-OPS-01 (cron `pg_dump` + offsite, doc 37).
- ⚠️ **DATA-01**: schema drift via `app/main.py` — backup obrigatório antes de deploy (ver WP-DATA-01).
- ⚠️ Migrations inline no startup (sem Alembic) — WP-DATA-02.

## Gate de deploy (obrigatório antes de subir)
1. Backup fresco verificado (`pg_dump` + checksum + caminho registrado).
2. `git grep require_admin` ≥ 13; `compare_digest` ≥ 1; `admin123|create_test_user` = 0; `localhost:5678` = 0 (templates).
3. `ENVIRONMENT=production` confirmado; `/docs` fechado; CORS restrito.
4. Render smoke + login smoke em staging.
5. Plano de rollback + aprovação humana.
