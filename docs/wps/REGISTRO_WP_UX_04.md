# Registro — WP-UX-04 (Sino Global de Notificações)

| Campo | Valor |
|---|---|
| Branch | `wp-ux-01-base-layout` |
| Commit | `4842c96` |
| Modo | Implementação local. **Sem push/PR/deploy/VPS/banco. Zero backend novo.** |

## Objetivo
Sino de notificações global no topo direito (antes do avatar), funcionando em
Comercial, Operacional, Gestão e Hub — comportamento inspirado no Trello
(badge de não-lidas, dropdown, marcar tudo como lido, fechar ao clicar fora).

## Decisão de arquitetura
**Reuso integral da API existente** `/api/operational/notifications`
(GET lista com `only_unread`, POST `/{id}/read`, POST `/read-all`; service já
valida que o usuário só altera as próprias notificações). Nenhum endpoint,
model ou migração criados. A generalização para eventos de Gestão Interna
(pendências internas) fica para o WP-GI — o frontend do sino já está pronto
para consumi-la quando existir.

## Entregue
- `partials/_topbar.html`: bell + badge + dropdown no slot reservado desde o
  WP-UX-01 (`header-right`, antes do `.user-info`), com `aria-label`/`aria-expanded`.
- `static/js/notifications.js` (novo): fetch via `Auth.apiRequest`; polling 60s
  + refresh ao abrir; clique em item não-lido marca como lida; "Marcar tudo
  como lido"; fecha ao clicar fora e com `Esc`. **XSS-safe por construção**:
  itens montados com `createElement`/`textContent` — a mensagem nunca passa
  por `innerHTML` (padrão da auditoria SEC-XSS). Defensivo: páginas sem o
  sino (login) são no-op; erros de rede não quebram a página.
- `static/css/layout.css`: estilos do sino/dropdown com tokens existentes.
- `templates/base.html`: `notifications.js` carregado após `auth.js`/`layout.js`.

## Provas (testes locais)
- `tests/test_notifications_ui.py`:
  - sino/dropdown/markall presentes em `dashboard`, `hub` e `operational/boards`;
    ordem de scripts correta;
  - **contrato da API provado** com TestClient: `401` sem token; `200` na lista
    (JSON list) e no `read-all` com JWT válido gerado em processo
    (`create_access_token`) — sem consumir o rate limit do login.
- Regressão completa verde: render/setores, hub, rate-limit, security-greps,
  `node --check`, CSS balanceado.

## Riscos/observações
- Polling de 60s/usuário soma ao teto global de 200 req/min/IP (SEC-RL) — ok
  para o tamanho da equipe; reavaliar se o time crescer (SSE/websocket futuro).
- Notificações hoje só nascem de eventos operacionais (mention/movement/
  assignee). Eventos de pendências internas → WP-GI-04.
- Smoke visual em browser antes de deploy (gate do PR) continua obrigatório.

## Próximo WP recomendado
**WP-OP-UI-01/02** — redesign da lista de quadros e board operacional
full-screen (recolher sidebar no kanban via `{% block sidebar %}` já preparado),
ou **WP-GI-01** (SDD do domínio de Gestão Interna). Ambos exigem SDD próprio.
