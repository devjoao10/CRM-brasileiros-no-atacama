# Auditoria Frontend / UX — BNA CRM

## Estado positivo (pós WP-UX-01 / 01.1)
- ✅ Layout centralizado em `base.html` + `partials/_sidebar.html` + `partials/_topbar.html`.
- ✅ 12 templates migrados; **1 fonte única** de sidebar/topbar (antes 12 cópias).
- ✅ `layout.css` (shell) + `layout.js` (auth/logout/toggle mobile) compartilhados.
- ✅ `components.css`: sistema global de chips (`.chip`, `.chip-tag`, status/priority/destination, 29 definições).
- ✅ Viewport-lock opcional (`viewport_locked`) — aditivo, default inalterado.
- ✅ Isolamento de scroll horizontal do board (pipeline) e `overflow-x:hidden` global no body.
- ✅ Render smoke 12/12 OK.

## Achados
| ID | Sev | Item | Recomendação |
|---|---|---|---|
| FE-XSS (=SEC-XSS-02) | ALTO | `esc()` não escapa aspas simples em `onclick` (leads/tags/segmentacao) | Escapar `'` ou trocar `onclick` por `addEventListener`/`data-*` (WP-SEC-02) |
| FE-03a | MÉDIO | Botões-ícone (editar/excluir/✕) sem `aria-label` | Adicionar `aria-label`/`title` consistentes |
| FE-03b | MÉDIO | Modais sem focus-trap nem `Esc` para fechar (varia por página) | Helper de modal acessível (WP-FE-02/03) |
| FE-03c | MÉDIO | Contraste de chips/labels não auditado (WCAG AA) | Auditar tokens de cor (WP-FE-03) |
| FE-04 | BAIXO | CSS/JS específico de página ainda inline em `{% block %}` | Aceitável; extrair só se crescer |
| FE-05 | BAIXO | `esc()` duplicado em vários templates | Centralizar em util JS compartilhado (WP-FE-02) |

## Consistência visual
- Chips: migrar usos remanescentes de classes locais (ex. já feito em `tags.html`) para `.chip` global (WP-FE-02 — varrer leads/pipeline/segmentacao/operacional).
- Sidebar única ainda mistura Comercial + Operacional + Sistema → separação por setor é **WP-UX-03** (sidebars contextuais), não regressão.

## Próximos WPs de frontend
- **WP-FE-02** — design system: centralizar `esc()`, padronizar chips/botões/modais.
- **WP-FE-03** — acessibilidade: aria-labels, focus-trap, contraste WCAG AA, navegação por teclado.
