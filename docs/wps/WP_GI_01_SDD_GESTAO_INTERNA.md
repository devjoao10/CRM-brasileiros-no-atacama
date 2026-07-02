# WP-GI-01 — SDD: Domínio de Gestão Interna (Pendências Internas)

| Campo | Valor |
|---|---|
| Branch | `wp-ux-01-base-layout` |
| Regime | **Toca banco (tabela nova)** → migração versionada + backup obrigatório antes de aplicar em produção |
| Escopo | GI-01 (SDD) + GI-02 (modelo/API) + GI-03 (UI) + GI-04 (sino) — implementação **local** |

## 1. Problema
"Minhas Pendências" hoje é só uma lista de **cards operacionais atribuídos**
(`operational_pending`, 29 linhas). Não existe domínio para tarefas internas do
dia a dia (recorrentes ou pontuais) por responsável — a visão de Gestão Interna
do produto. Regra do SDD mestre: **não misturar** pendências internas com cards
operacionais (§8.7).

## 2. Modelo de dados (tabela nova: `internal_tasks`)
| Coluna | Tipo | Regra |
|---|---|---|
| id | PK | |
| title | VARCHAR(200) NOT NULL | |
| description | TEXT NULL | texto grande (§8.4) |
| assignee_id | FK users.id, SET NULL | responsável (obrigatório na criação) |
| created_by | FK users.id, SET NULL | criador |
| task_type | VARCHAR(20) NOT NULL default `pontual` | `pontual` \| `recorrente` |
| recurrence | VARCHAR(20) NULL | `diaria` \| `semanal` \| `mensal` (só se recorrente) |
| due_date | DATE NULL | data da pendência |
| priority | VARCHAR(10) NULL | `baixa` \| `media` \| `alta` (opcional §8.4) |
| status | VARCHAR(20) NOT NULL default `pendente` | **armazenado**: `pendente` \| `concluida` |
| last_completed_at | DATETIME NULL | última conclusão (recorrentes) |
| is_archived | BOOLEAN NOT NULL default false | |
| created_at / updated_at | DATETIME | |

**Decisão §8.6 (coerência de status):** `atrasada` **não é armazenada** — é
**derivada no backend** (`effective_status`): `concluida` se status=concluida;
senão `atrasada` se `due_date < hoje`; senão `pendente`. Uma única fonte de
verdade; UI nunca calcula sozinha.

**Decisão §8.5 (recorrência MVP — "rolling task"):** pendência recorrente é
**uma linha só**. Ao concluir: grava `last_completed_at` e **avança `due_date`**
para a próxima ocorrência (diária +1d, semanal +7d, mensal +1 mês calendário),
mantendo `pendente`. Evita gerador em background e duplicidade por construção.
Limitação aceita: histórico por ocorrência fica para evolução futura
(`internal_task_occurrences`).

**Decisão GI-04 (notificações):** **reuso** de `operational_notifications`
(`card_id` é nullable e `event_type` é livre) com `event_type='internal_task'`.
O sino global (WP-UX-04) já lê essa tabela → notificação ao responsável sem
tabela nova nem mudança no frontend do sino.

## 3. API (router novo `/api/internal/tasks`)
| Método | Rota | Permissão | Descrição |
|---|---|---|---|
| GET | `` | autenticado | lista (filtros: `assignee_id`, `include_archived`); responde `effective_status` |
| POST | `` | autenticado (**qualquer um cria para qualquer pessoa** §8.4) | cria; notifica o responsável no sino se ≠ criador |
| PATCH | `/{id}` | criador, responsável ou admin | edita campos |
| POST | `/{id}/complete` | criador, responsável ou admin | conclui (pontual) ou avança ocorrência (recorrente) |
| POST | `/{id}/archive` | criador ou admin | arquiva |

Responsáveis dinâmicos: **reuso** de `GET /api/users/for-select` (já usado por
segmentação/tarefas) — novos usuários aparecem sem editar HTML (§8.3).

## 4. UI (GI-03) — `/gestao/pendencias`
- Página nova `templates/gestao/pendencias.html` (extends base, `sector=gestao`).
- **Navegação horizontal por responsável** (tabs dinâmicas de `/api/users/for-select` + "Todos").
- Cards com **borda por estado** (pendente=âmbar, atrasada=vermelha, concluída=verde),
  chip de recorrência/prioridade, contadores, destaque de atrasadas, empty-state,
  responsivo. Modal de criação com os campos mínimos do §8.4.
- Sidebar gestão: novo item **"Pendências Internas"**; o item antigo vira
  **"Cards Operacionais"** (rota `/operational/my-pending` preservada — nenhum
  redirecionamento removido).
- Rota de página em `pages.py` com o mesmo gate de cookie.

## 5. Migração
- Bancos novos: `create_all()` (model importado no `main.py`).
- Bancos existentes: `migrations/m002_internal_tasks.py` (CREATE TABLE
  idempotente, padrão do m001). **Aplicar em produção só com backup verificado +
  aprovação humana** (gate do `migrations/README.md`).

## 6. Riscos
- Tabela nova em prod sem migração aplicada → endpoints 500. Mitigação: gate de
  deploy já exige rodar migrations antes do up.
- Notificação reusa tabela operacional → se o WP futuro separar o domínio de
  notificações, `event_type='internal_task'` facilita o split.
- Recorrência rolling não guarda histórico por ocorrência (documentado).

## 7. Critérios de aceite
1. CRUD/complete/archive funcionando com permissões acima (provado via TestClient).
2. `effective_status` derivado corretamente (pendente/atrasada/concluida).
3. Concluir recorrente avança a data e mantém pendente; pontual conclui.
4. Criar pendência para outro usuário gera notificação `internal_task` no sino.
5. Página renderiza no setor gestão com tabs por responsável; rotas antigas intactas.
6. Regressão completa verde; sem tocar produção.
