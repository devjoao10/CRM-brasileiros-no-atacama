# Auditoria Severa — BNA CRM / Operacional

| Campo | Valor |
|---|---|
| Branch | `wp-ux-01-base-layout` |
| Base | `feature/operational-mvp-foundation` @ `67f2f29` |
| Modo | Read-only + correções locais de baixo risco. **Sem deploy/push/PR/VPS/banco prod.** |
| Cruzamento | código vivo + docs `H:\BNA_SYSTEM_AUDIT_READONLY` (12, 13, 25, 26, 37, 108, 109, 110, 114) + git |

## 1. Resumo executivo

O sistema está **funcional e com bom hardening de base** (bcrypt, cookie httponly+secure+samesite, `require_admin` em 36 pontos, rate limiter global, security headers, HMAC no webhook, Postgres sem porta externa, user read-only para a IA, segredos via env obrigatório). O WP-UX-01 centralizou o layout em `base.html` (12 templates, −~1.600 linhas de duplicação) e o WP-UX-01.1 adicionou viewport-lock + sistema global de chips.

Os riscos remanescentes concentram-se em: **(a) XSS no frontend** (um corrigido nesta auditoria, dois herdados pendentes), **(b) rate-limit de login possivelmente não efetivo**, **(c) schema-drift em `app/main.py` (DATA-01)**, **(d) exposição direta da porta 8000**, **(e) ausência de camada de services (god routers)** e **(f) ausência de suíte de testes**. Nenhum é bloqueante para o estado **local**; vários exigem WP próprio antes de deploy.

## 2. Achados por severidade

### CRÍTICO
_(nenhum achado de severidade crítica não-mitigada no estado local atual)_

### ALTO
| ID | Área | Arquivo | Evidência | Impacto | Status |
|---|---|---|---|---|---|
| **SEC-XSS-01** | Segurança/FE | `templates/ai.html:769` | chat renderizava `marked.parse(content)`/user via `innerHTML` **sem DOMPurify** | prompt-injection → stored XSS → roubo de JWT | ✅ **CORRIGIDO** (DOMPurify user+IA) |
| **SEC-XSS-02** | Segurança/FE | `leads.html`, `tags.html`, `segmentacao.html` | `esc()` não escapa aspas simples em handlers `onclick` (FRONT-03 do doc 12) | stored XSS via nome/cor | WP-SEC-02 (não corrigir junto do UX) |
| **SEC-RL-01** | Segurança | `app/routers/auth.py:19` | `Limiter` próprio, **separado** do `app.state.limiter` de `main.py` | `5/minute` no login pode **não** ser aplicado → brute force | WP-SEC-03 (verificar + unificar) |
| **SEC-AUTH-01** | Segurança | `static/js/auth.js` | JWT em `localStorage` **além** do cookie httponly (DEC-01) | qualquer XSS exfiltra token (8h) | WP-SEC-01 (migrar p/ cookie-only) |
| **ARCH-01** | Arquitetura | `routers/leads.py` (~659), `ai.py` (~521), `pipeline.py` (~429) | regra de negócio + queries nos routers (god routers) | manutenção/teste difíceis | WP-ARCH-01/02 (services) |

### MÉDIO
| ID | Área | Evidência | Status |
|---|---|---|---|
| **SEC-CSP-01** | Segurança | sem `Content-Security-Policy` em `main.py` (FRONT-06) | WP-SEC-02 (cuidado: muito inline JS) |
| **SEC-XSS-03** | Segurança | `ai.html:7` `marked.min.js` sem versão fixa nem SRI (FRONT-05) | WP-SEC-02 (pin + SRI) |
| **DEVOPS-01** | DevOps | `docker-compose.yml:39` CRM publica `8000:8000` no host **além** do Traefik | WP-OPS-01 (remover port mapping; só Traefik) |
| **DATA-02** | Banco | migrations inline no `lifespan` (sem Alembic, RM-12) | WP-DATA-02 |
| **ARCH-02** | Arquitetura | `_build_lead_response`/`_json_list_contains` duplicados em `leads.py`+`segments.py` | WP-ARCH-02 (RM-02 LeadQueryService) |
| **ARCH-04** | Segurança/Arch | `ai_tools.py` global mutável `_current_user_api_key` (RM-01) | WP-ARCH-02 (context var) |
| **TEST-01** | QA | sem suíte de testes; só render smoke | ✅ parcial: render test rastreado adicionado; WP-QA-01 p/ auth/API |

### BAIXO
| ID | Área | Evidência | Status |
|---|---|---|---|
| **DEVOPS-02** | DevOps | `n8n N8N_RESTRICT_ENVIRONMENT_VARIABLES_ACCESS=false` | WP-OPS-01 |
| **DATA-03** | Banco | soft-delete inconsistente (`is_active` Lead vs `is_archived` operacional; sem `deleted_at`) | DEC-08 / WP-DATA-02 |
| **FE-03** | Frontend | botões-ícone sem `aria-label`, modais sem focus-trap, contraste não auditado | WP-FE-03 |
| **F-02/F-03** | Repo | `scratch/` não ignorado; EOF blank lines | ✅ CORRIGIDO (WP-AUDIT-00) |

### DATA-01 (especial)
| ID | Área | Evidência | Status |
|---|---|---|---|
| **DATA-01** | Banco/DevOps | `app/main.py` +15: `ALTER TABLE leads ADD COLUMN` (5 colunas) no startup | Schema-drift remediation. **Segregado**, não commitado com UX. Ver `WP_DATA_01_SCHEMA_LEADS.md` |

## 3. Correções aplicadas nesta auditoria (local, sem deploy)
1. **SEC-XSS-01** — DOMPurify no chat da IA (`ai.html`), sanitizando ramo do usuário (`ALLOWED_TAGS:['br']`) e ramo da IA (`marked.parse`). Render test verde.
2. **WP-AUDIT-00** — estabilização do Git: `scratch/` gitignorado, EOF corrigido, WP-UX-01.1 frontend commitado isolado (`7fd8cc5`), `app/main.py` segregado.
3. **TEST-01 (parcial)** — `tests/test_render_templates.py` rastreado (render smoke + assert de sanitização do chat).

## 4. Não corrigido (e por quê)
- **SEC-XSS-02 / SEC-RL-01 / SEC-AUTH-01 / SEC-CSP-01**: mexem em auth/segurança de forma sensível ou exigem mudança coordenada (cookie-only, unificar limiter, CSP vs inline JS) → WP-SEC com teste antes.
- **ARCH-01/02/04**: refatoração de routers para services = mudança ampla → WPs RM-01/RM-02 (docs 108/109).
- **DEVOPS-01/02 / DATA-01/02**: tocam infra/banco → exigem backup + aprovação + deploy controlado.

## 5. Estado final do Git
- Branch `wp-ux-01-base-layout`; WP-UX-01 (8 commits) + WP-UX-01.1 (`7fd8cc5`) fechados.
- Esta auditoria adiciona: fix de segurança (ai.html), teste rastreado e docs.
- `app/main.py` permanece **unstaged** (DATA-01), aguardando WP-DATA.
- **Nenhum push, deploy, PR, acesso a VPS ou banco de produção.**

## 6. Pronto para PR vs. não-deployável
- **Pronto para PR (local):** WP-UX-01 + WP-UX-01.1 + fix SEC-XSS-01 + testes + docs.
- **NÃO pode ir a deploy sem WP:** `app/main.py` (DATA-01 — exige backup), e qualquer correção de SEC-RL/AUTH/CSP/DEVOPS.
