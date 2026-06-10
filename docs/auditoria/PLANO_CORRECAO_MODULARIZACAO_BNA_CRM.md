# Plano de Correção e Modularização — BNA CRM

Derivado da auditoria severa. Cada WP = branch própria, commits pequenos, **sem deploy sem backup/aprovação**.

## Já executado nesta sessão (local)
| WP | Descrição | Commit |
|---|---|---|
| WP-AUDIT-00 | Estabilizar Git (gitignore `scratch/`, EOF, segregar `app/main.py`) | `7fd8cc5` (UX-01.1) + ajustes |
| SEC-XSS-01 | DOMPurify no chat da IA | (commit desta auditoria) |
| WP-QA-01 (parcial) | `tests/test_render_templates.py` rastreado | (commit desta auditoria) |
| Documentação | 6 relatórios em `docs/auditoria/` | (commit desta auditoria) |

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
