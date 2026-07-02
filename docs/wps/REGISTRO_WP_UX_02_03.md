# Registro — WP-UX-02 (Hub de Setores) + WP-UX-03 (Sidebars Contextuais)

| Campo | Valor |
|---|---|
| Branch | `wp-ux-01-base-layout` |
| Commits | `c89092d` (UX-02) · `88af2dd` (UX-03) |
| Modo | Implementação local. **Sem push/PR/deploy/VPS/banco.** |
| Base | Protocolo SDD estabelecido (docs 108/109/110 + auditoria em `docs/auditoria/`) |

## WP-UX-02 — Hub de Setores (porta de entrada pós-login)

**Objetivo:** primeira tela após login deixa de ser o dashboard e passa a ser um hub
com 3 cards de setor, sem remover nenhuma rota antiga.

**Entregue:**
- `templates/hub.html` — estende `base.html` com `{% block sidebar %}` vazio (sem
  sidebar), topbar preservada, 3 cards (Comercial → `/dashboard`; Operacional →
  `/operational/boards`; Gestão Interna → `/operational/my-pending`, com chip
  "Em evolução" — o domínio próprio de GI é WP futuro), saudação com nome do
  usuário e link de logout (`id="logoutBtn"`, wired pelo `layout.js`).
- `app/routers/pages.py` — rota `/hub` com o **mesmo** gate de cookie
  (`_require_cookie`) das demais páginas. Única mudança de router; padrão
  idêntico; auth intocada.
- `static/js/login.js` — fallback do redirect pós-login `/dashboard` → `/hub`
  (o parâmetro `?next=` continua tendo prioridade). `/dashboard` permanece
  acessível (nenhuma rota removida).

## WP-UX-03 — Sidebars contextuais por setor

**Objetivo:** acabar com a sidebar-união (commit `3686a45`) que misturava
Comercial + Operacional + Administração no mesmo menu.

**Entregue:** `partials/_sidebar.html` renderiza somente a navegação do setor:
| Setor | Itens |
|---|---|
| comercial | Hub, Dashboard, Assistente IA, Leads, Tags, Pipeline, Segmentação, Tarefas, Relatórios |
| operacional | Hub, Esteira de Processos |
| gestao | Hub, Minhas Pendências, Equipe e Usuários, API Docs, Servidor n8n |

Cada template declara `{% set sector = "..." %}` (12 páginas; fallback seguro
`comercial` se ausente). Mapeamento: 8 páginas comerciais; boards/kanban →
operacional; pending/equipes → gestao (pendências saem conceitualmente do
Operacional, conforme visão de produto).

## Provas (testes locais)
- `tests/test_hub.py`: render sem sidebar + 3 cards + logout; **gate de rota
  provado** com TestClient (302 sem cookie → `/login`; 200 com cookie).
  Não consome o rate limit do login (usa cookie dummy — o gate de página só
  verifica presença, comportamento real testado).
- `tests/test_render_templates.py` + `test_sector_sidebar_isolation`: os 12
  templates continuam com item ativo único e volta ao `/hub`; links de um setor
  **não vazam** para outro.
- Regressão: rate-limit test e security-greps continuam verdes; `py_compile`
  em `pages.py`; `node --check` em `login.js`.

## Riscos/observações
- O gate de `/hub` (presença de cookie) é o padrão herdado das demais páginas —
  a validação real fica na API (`layout.js` → `Auth.requireAuth`). Endurecer
  isso é escopo do WP-SEC-01 (cookie-only/CSRF), não deste.
- Smoke visual em browser continua obrigatório antes de deploy (gate do PR).
- Links "API Docs"/"n8n" hoje aparecem para todo usuário do setor gestao;
  restringir a admin é candidato ao WP-UX-04/SEC (visibilidade por role).

## Próximo WP recomendado
**WP-UX-04 — Topbar global com sino de notificações**, consumindo a API
existente `/api/operational/notifications` (badge de não-lidas, dropdown,
marcar como lido). Exige SDD curto próprio por integrar API real.
