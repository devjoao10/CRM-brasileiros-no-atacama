# Arquitetura Modular / Clean — BNA CRM

Cruzado com docs oficiais 108 (arquitetura-alvo), 109 (plano RM-01..12), 110 (protocolo de features).

## 1. Estado atual
- **Camadas:** Router → (lógica de negócio + queries ORM no próprio router) → Models. **Falta a camada de services/repositories** na maior parte.
- **God routers:** `leads.py` (~659), `ai.py` (~521), `webhook.py` (436, conversas), `pipeline.py` (~429), `segments.py` (~360).
- **Duplicação:** `_build_lead_response` / `_json_list_contains` em `leads.py` e `segments.py`; `esc()` em vários templates.
- **Estado global mutável:** `ai_tools.py` `_current_user_api_key` (risco em async — ARCH-04 / RM-01).
- **Operacional:** já isolado em `app/{models,routers,services,repositories}/operational*` — **bom**: tem services e repositories. Serve de referência de modularização para o Comercial.

## 2. Separação de domínios (alvo)
| Domínio | Hoje | Alvo |
|---|---|---|
| **Comercial** | leads, tags, pipeline, segments, tasks, analytics, ai | manter; extrair services (RM-02..05) |
| **Operacional** | `operational_*` (models/services/repos/routers) já separados | manter; full-board é WP-OP-UI |
| **Gestão Interna** | **não existe** como domínio (só `operational_pending`) | criar `internal_tasks` em WP-GI (SDD próprio) |
| **Apresentação** | `base.html` + partials (unificado) | sidebars contextuais por setor = WP-UX-03 |

Acoplamentos a vigiar: notificações hoje acopladas ao operacional (`operational_notifications`); CRM↔Conversas via SQL direto (`crm.py`) → `CRMBridgeService` (RM-09).

## 3. Plano incremental (alinhado a docs 108/109)
Ordem recomendada (cada um = WP, branch própria, sem big-bang):
1. **WP-ARCH-01 / RM-01** — `AIToolsContextManager` (remove global mutável; segurança).
2. **WP-ARCH-02 / RM-02** — `LeadQueryService` (elimina duplicação leads/segments).
3. **RM-03/04/05** — SegmentService, LeadImportService, PipelineService.
4. **RM-06..09** — services do Conversas (debounce, auto-reply, conversation, CRMBridge).
5. **RM-10** — AIOrchestrationService.
6. **RM-12** — Alembic (substitui migrations inline; ver DATA-02).

## 4. Princípios a preservar (doc 108)
- Não adicionar lógica em router > 300 linhas sem extrair service antes.
- Preservar comportamento externo (responses JSON idênticos) nas refatorações.
- Não tocar guards `require_admin`, HMAC, seeds durante refatoração.
- Reuso de model existente antes de criar tabela nova (doc 110).

## 5. Fronteiras de apresentação (UX)
- `base.html` é a fonte única do shell. WP-UX-03 introduz `sector` no `_sidebar.html` para renderizar só a seção do setor — separação Comercial/Operacional/Gestão sem duplicar sidebar.
