# Registro — WP-OP-UI (Board Full-Screen + Botões Condicionais)

| Campo | Valor |
|---|---|
| Branch | `wp-ux-01-base-layout` |
| Commit | `3600085` |
| Modo | Implementação local. **Sem push/PR/deploy/VPS/banco. Zero backend novo.** |

## Inventário prévio (regra 7.4 do SDD)
`kanban.html` (OP-12 + migração UX-01) já tinha: header próprio com "Voltar
para Quadros", colunas com contador, drag & drop, modal rico (descrição,
checklists, comentários com menção, responsáveis, campos customizados,
histórico), criação de card/coluna (coluna = admin-only). Faltavam: full-screen
real (sidebar ainda ocupava 240px), regra condicional de criação e destaque de
prazo. **Nenhum campo novo inventado** — só UI sobre a API existente.

## Entregue
- **OP-UI-02 — Full-screen:** `{% block sidebar %}` vazio (preparado desde o
  Passo 0 do WP-UX-01) + `margin-left: 0`. Board ocupa toda a largura; listas
  lado a lado com rolagem horizontal (já existia); volta pelo botão do header.
- **OP-UI-04 — Botões condicionais (regra obrigatória §7.6):**
  - `+ Criar Card` **desabilitado** (`disabled` + tooltip) quando não há colunas;
  - guard no clique com a mensagem exata: **"Crie uma coluna antes de criar um card."**;
  - estado recalculado após `fetchLists` (init e pós-criar coluna);
  - **empty-state** no board sem colunas (mensagem + orientação distinta para
    admin × usuário comum);
  - `+ Nova Coluna` e `Voltar para Quadros` já funcionais (verificado).
- **OP-UI-03 (mínimo seguro):** prazo do card em vermelho quando vencido
  (`.kanban-due.overdue`). Labels/etiquetas **não** entram aqui — exigem tabela
  nova (WP-OP-LABELS-01, regime de migração+backup).
- **Bônus de segurança:** nome da coluna escapado (`esc(l.name)`) no select do
  modal de criar card (XSS latente fechado).

## Provas
- `tests/test_kanban_ui.py`: full-screen (0 sidebars, margin-left:0, botão de
  volta), contrato condicional (mensagem, `updateCreateCardState`,
  `btn.disabled`, empty-state, escape do select) e chip de prazo.
- `tests/test_render_templates.py`: exceção `FULLSCREEN` para o kanban
  (sem sidebar/active/hub-link; demais 11 páginas inalteradas).
- Regressão completa verde: render/setores, hub, sino, rate-limit, greps.

## Pendências (WPs futuros)
- **WP-OP-UI-01**: redesign visual da lista de quadros (`boards.html`).
- **WP-OP-UI-03 completo**: cards com avatar de responsável/progresso de
  checklist no próprio card (exige incluir esses dados na resposta da listagem
  de cards ou chamadas extras — decisão de API, SDD próprio).
- **WP-OP-LABELS-01**: etiquetas coloridas (tabela nova → migração + backup).
- Smoke visual em browser antes de deploy (gate do PR) — obrigatório,
  especialmente para o full-screen.

## Próximo WP recomendado
**WP-GI-01** — SDD do domínio de Gestão Interna (`internal_tasks`, pendências
por responsável, recorrência, integração com o sino). Toca banco → regime
completo: SDD → modelagem → migração versionada → backup → aprovação.
