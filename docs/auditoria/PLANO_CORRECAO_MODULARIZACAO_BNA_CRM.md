# Plano de Correção e Modularização — BNA CRM

Derivado da auditoria severa. Cada WP = branch própria, commits pequenos, **sem deploy sem backup/aprovação**.

## Já executado (local, sem deploy/push)
| WP | Descrição | Commit |
|---|---|---|
| WP-AUDIT-00 | Estabilizar Git (gitignore `scratch/`, EOF, segregar `app/main.py`) | `7fd8cc5` |
| SEC-XSS-01 | DOMPurify no chat da IA | `d0db79c` |
| SEC-XSS-02 | `esc()` escapa aspas em 10 templates | `cb13514` |
| DATA-01 | ALTER inline → migration idempotente `migrations/m001` | `8064782` |
| SEC-RL-01 (WP-SEC-03) | limiter unificado + `SlowAPIMiddleware`; 429 provado | `d065628` |
| WP-OPS-01 | compose só-Traefik + healthcheck; `scripts/backup_postgres.sh` | `6b6bd08` |
| SEC-XSS-03 (parcial) | `marked@12.0.0` fixado | `9f3b9d0` |
| WP-QA-01 | 3 testes rastreados (render, rate-limit, security-greps) + README | `f7c08ba`, `bb2e2da` |
| Documentação | relatórios em `docs/auditoria/` | `c9a7797`, `7f5c97d`, `3774aba`, este |

## Ordem recomendada dos próximos WPs
1. **WP-SEC-02 follow-up** — SRI do `marked` (hash em CI) + avaliar CSP com nonces.
2. **WP-SEC-01** — JWT cookie-only + CSRF (auth profundo → exige smoke + aprovação).
3. **WP-OPS-02** — Dockerfile non-root; backup automático (cron + offsite B2/S3); healthcheck `conversas`.
4. **WP-DATA-01 (deploy)** — aplicar `m001` em prod **com backup** (gate humano).
5. **WP-ARCH-01/02** — RM-01 (AIToolsContextManager) + RM-02 (LeadQueryService).
6. **WP-FE-02/03** — design system (centralizar `esc()`, chips) + acessibilidade (com smoke visual).
7. **WP-QA-01 fase 2** — testes de auth/IDOR/HMAC.

## WPs recomendados (prioridade)

### Segurança
- **WP-SEC-01** (ALTO) — JWT cookie-only (remover `localStorage`) + proteção CSRF. *Toca auth → testar; não fazer sem WP.*
- **WP-SEC-02** (ALTO/MÉDIO) — `esc()` escapar aspas simples; pin+SRI do `marked.js`; introduzir CSP compatível com inline JS.
- **WP-SEC-03** (MÉDIO) — unificar `Limiter` (login realmente rate-limited); auditar brute force.

### DevOps
- **WP-OPS-01** (MÉDIO) — remover `ports: 8000:8000` (só Traefik); confirmar auth do n8n; backup automático `pg_dump` + offsite (doc 37); rollback documentado.

### Dados
- **WP-DATA-01** (ALTO) — schema-drift `leads` (`app/main.py`) com backup + plano (ver doc dedicado).
- **WP-DATA-02** (MÉDIO) — Alembic (substituir migrations inline); padronizar soft-delete (`deleted_at`).

### Arquitetura
- **WP-ARCH-01** — RM-01 `AIToolsContextManager` (segurança/async).
- **WP-ARCH-02** — RM-02 `LeadQueryService` (duplicação leads/segments) + services de domínio.
- **WP-ARCH-03** — separação de domínios na apresentação (WP-UX-03 sidebars contextuais) e criação do domínio Gestão Interna (`internal_tasks`, SDD próprio).

### Frontend
- **WP-FE-02** — design system (chips/botões/modais; centralizar `esc()`).
- **WP-FE-03** — acessibilidade (aria, focus-trap, contraste WCAG AA).

### QA
- **WP-QA-01** — suíte pytest: render (feito), auth (login/logout/401), IDOR, HMAC; doc de como rodar.

## Sequência sugerida
1. WP-DATA-01 (destrava feature de leads, exige backup) →
2. WP-SEC-02/03 (XSS/rate-limit, baixo acoplamento) →
3. WP-OPS-01 (porta/backup) →
4. WP-ARCH-01/02 (services) →
5. WP-UX-02/03 (hub + sidebars contextuais) →
6. WP-FE-02/03, WP-QA-01 (contínuos).
