# POSTGRES_VALIDATION.md

Redução do segundo grande gap da auditoria: **toda a suíte roda em SQLite e
produção é PostgreSQL**.

## O que foi possível fazer, e o que não foi

**Não há Docker nesta máquina** — `docker info` expira (daemon parado), e não há
`psql` nem `pg_dump` instalados. Verificado, não suposto. Portanto **não subi
PostgreSQL, não criei banco de teste e não executei migration contra
PostgreSQL**.

O que dá para fazer sem servidor, e foi feito: `psycopg2` está instalado, e o
SQLAlchemy **compila** contra o dialeto `postgresql` sem conexão. Cada query
relevante foi compilada contra os **dois** dialetos e o SQL gerado foi comparado.
Onde o mapeamento de erro importava, usei `psycopg2.errors.lookup(SQLSTATE)`, que
resolve a classe de exceção a partir do código, também sem conexão.

Isso prova **forma de SQL e classe de exceção**. Não prova comportamento de
runtime que depende de configuração do servidor (collation, `TimeZone`, DDL
efetivo). Esses estão listados no fim, com o comando exato para fechar cada um.

## Teste criado

`tests/test_postgres_dialect_divergence.py` — **78 checks**, exit 0, idempotente.
Não sobe nem conecta a PostgreSQL: compila e afirma sobre o SQL gerado. **Zero
SKIP** — falta de pré-requisito (psycopg2, dialeto, SQLite < 3.30) **reprova**
com mensagem dizendo o que falta. Cai no job `crm` do CI, que é o único com as
dependências do CRM.

## Divergências encontradas

Severidade: **ALTA** = perda de dado, 500 ou resultado silenciosamente errado em
produção *enquanto a suíte SQLite passa verde*.

### A1 — ALTA — classificação de erro decidia retry da Meta, e era dialeto-dependente · **CORRIGIDO**

`conversas/app/routers/webhook.py`, `_INFRA_ERRORS`.

| | SQLite (suíte) | PostgreSQL (produção) |
|---|---|---|
| coluna/tabela/função inexistente | `OperationalError` | `ProgrammingError` (42703/42P01/42883) |
| valor fora do enum | `OperationalError` | `DataError` (22P02) |
| resultado | na lista → **503, a Meta reentrega** | fora → **200, mensagem PERDIDA** |

Qualquer drift de schema em produção descartava mensagem de cliente em
definitivo, e a suíte demonstrava o comportamento **oposto**. `ProgrammingError`
e `DataError` entraram na lista. `IntegrityError` segue fora de propósito — dado
inválido não se resolve com reentrega.

### A2 — ALTA — filtro de destino é case-insensitive só no SQLite · **DOCUMENTADO**

```
[PG]   CAST(leads.destinos AS JSONB) @> '["Atacama"]'
[LITE] lower(CAST(leads.destinos AS VARCHAR)) LIKE lower('%"Atacama"%')
```

`destino=atacama` devolve leads no ambiente de teste e **zero** em produção. Sem
erro — só lista vazia. O ramo PostgreSQL está correto; a divergência é
**semântica**. Não corrigido: decidir se a busca deve ser case-insensitive é
regra de negócio, e a correção (normalizar na escrita ou usar `jsonb_path_exists`
com `like_regex`) mexe em dado existente. Comando para medir o risco real está
no fim.

### A3 — ALTA — `SELECT ... INTO` passava por todos os guards da IA · **CORRIGIDO**

`app/services/ai_tools.py`. `SELECT * INTO copia FROM leads` começa com `select`,
não tem `;`, não casava a denylist e não cita `users`. No SQLite é erro de
sintaxe — inofensivo. No PostgreSQL é **DDL: cria tabela**. A denylist bloqueava
`pragma` e `attach`, palavras que **só existem no SQLite** — ela protegia o
dialeto de desenvolvimento e deixava o de produção aberto. Entraram `into`,
`copy`, `grant`, `revoke`.

A ferramenta continua sendo "somente leitura" principalmente por causa do GRANT
do `crm_readonly`. Revogar `CREATE` no schema continua sendo ação de operador.

### A4 — MÉDIA-ALTA — `ILIKE`/`lower()` com acento · **DOCUMENTADO**

```
[PG]   leads.nome ILIKE '%JOÃO%'
[LITE] lower(leads.nome) LIKE lower('%JOÃO%')
```

`lower('JOÃO')` no SQLite devolve `'joÃo'` (ASCII-only). A direção é invertida —
o SQLite acha **menos** — mas com armadilha: um teste local que "confirme" que a
busca não acha estaria fixando o comportamento errado. Depende de collation do
servidor; comando no fim.

### A5 — MÉDIA — fila legada ordenava coluna anulável sem `NULLS` · **CORRIGIDO**

`conversas/app/routers/conversations.py`. SQL **idêntico** nos dois dialetos, sem
cláusula de NULL — e os defaults são opostos: SQLite põe NULL primeiro,
PostgreSQL por último. Conversa sem inbound do cliente **abria** a fila em teste
e **fechava** a fila em produção. O caminho novo (`_inbox_order`) já usava
`.nullslast()`; sobrou o legado. Corrigido com `.nullslast()` explícito.

### A6 — MÉDIA — `created_at`/`updated_at` são anuláveis e são a chave de ordenação · **DOCUMENTADO**

`server_default` não implica `NOT NULL`. Em PostgreSQL, `ORDER BY created_at
DESC` sem `NULLS` é NULLS FIRST: uma linha com `created_at NULL` (INSERT direto
por psql, n8n ou `COPY`) fica **fixa no topo** e some da página 2, porque o
keyset `(created_at, id) < (...)` é NULL para ela. Comando para medir no fim.

### A7 — MÉDIA — enum `users.role` · **DOCUMENTADO**

```
[PG]   role userrole      <- TIPO ENUM NATIVO, validado pelo banco
[LITE] role VARCHAR(5)    <- sem CHECK
```

O SQLite aceita `'admin'` minúsculo e texto maior que `VARCHAR(5)`; o mesmo
INSERT em produção levanta `DataError` e **aborta a transação**. O caminho ORM
está protegido por Pydantic; o risco é escrita fora da ORM. A corrida de
`create_all` entre os dois serviços já foi fechada na Fase 1.

### A8 — MÉDIA — SQL cru só-PostgreSQL dentro de `except` mudo · **DOCUMENTADO**

`conversas/app/services/crm.py` usa `NOW()`, `::jsonb` e `RETURNING id`. O SQLite
**rejeita** `NOW()` e `::jsonb`, então `auto_create_lead_in_crm` e o INSERT de
histórico são **código que a suíte SQLite não pode executar nunca**. Como cada
caminho está dentro de `except Exception` que devolve `None`/`False`, um bug real
no ramo PostgreSQL tem assinatura idêntica ao cenário esperado "CRM inacessível
em dev isolado". Esta é a razão de a cobertura desse boundary ser efetivamente
zero.

### A9 — BAIXA-MÉDIA — naive vs aware contra `TIMESTAMPTZ` · **DOCUMENTADO**

`app/routers/analytics.py` compara com literal **sem** offset; `tasks.py` e
`pipeline.py` com offset. No PostgreSQL, literal sem offset é interpretado no
fuso da **sessão**. No SQLite as duas convenções colapsam na mesma string — a
diferença é invisível na suíte.

## Falsos alarmes (checados e descartados)

- **`SELECT DISTINCT` + `ORDER BY` fora da lista do SELECT — não existe no código.**
  Zero ocorrências de `distinct` em `app/` e `conversas/`. `segments.py` deduplica
  **em Python**. (A §6c de `RELEASE_READINESS.md` descreve esse SQL como o que
  uma *correção proposta* produziria, não como código existente — as duas coisas
  seguem verdadeiras.)
- `with_for_update()` — zero ocorrências.
- `json_type`/`jsonb_typeof` — os dois ramos existem e já estão travados por teste.
- `nullslast` em `tasks.py` — já correto.
- `func.trim` 1-arg, `extract`, `func.date`, `true()` — compilam equivalente.
- **Violação de UNIQUE** — 23505 → `IntegrityError`, a **mesma** classe do SQLite.
  Os `except IntegrityError` do CRM valem nos dois dialetos.

## m011 — veredito do ramo PostgreSQL: **APROVADO**

Statements reais capturados por listener sobre SQLite descartável, com o nome do
dialeto forçado para `postgresql` só para atravessar o gate — o SQLite rejeita o
`ALTER` **depois** de já tê-lo emitido, que é a prova de que esse ramo nunca foi
exercitado pela suíte.

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_conversations_whatsapp ON conversations (whatsapp)
ALTER TABLE leads    ALTER COLUMN campos_personalizados SET DEFAULT '{}'
ALTER TABLE leads    ALTER COLUMN status_venda          SET DEFAULT 'em_negociacao'
ALTER TABLE leads    ALTER COLUMN is_active             SET DEFAULT true
ALTER TABLE messages ALTER COLUMN send_attempts         SET DEFAULT 0
```

Sintaxe válida. `IF NOT EXISTS` para índice existe desde o PostgreSQL 9.5. Sem
`CONCURRENTLY` — correto, ele não roda em bloco transacional. `true` (não `1`).
Nenhum `ALTER ... TYPE`, nenhuma linha reescrita. A detecção de duplicata usa
`EXISTS` correlacionado, que é idêntico nos dois dialetos — executado de verdade
contra SQLite com duas conversas do mesmo número: achou as duas.

**Pré-condições operacionais** (não são defeitos):
1. `_index_present` casa por **nome**. Se a unicidade já existir sob outro nome,
   cria um índice duplicado — inofensivo, custa espaço.
2. `ALTER TABLE ... SET DEFAULT` exige **ownership**. Se o usuário da
   `DATABASE_URL` não for o dono, sai `must be owner of table` → exit 1 com
   relatório. Falha ruidosa, que é o comportamento certo.

**A m011 continua NÃO EXECUTADA em lugar nenhum.** E antes de executá-la, aplique
a correção da corrida de primeiro contato que entrou nesta fase
(`conversas/app/routers/webhook.py`): sem ela, o índice único transforma conversa
duplicada — defeito visível — em **mensagem de cliente perdida**, que é pior.

## O que só um PostgreSQL real fecha

| Item | Comando |
|---|---|
| `ILIKE` é locale-aware? (decide o A4) | `SELECT datname, datcollate, datctype FROM pg_database WHERE datname = current_database();` e `SELECT 'João' ILIKE '%JOÃO%';` |
| Fuso da sessão (decide o A9) | `SHOW TimeZone;` |
| Ordenação de NULL confirmada | `SELECT x FROM (VALUES (NULL),('a'),('b')) v(x) ORDER BY x ASC;` — esperado `a,b,NULL` |
| `users.role` é enum nativo aqui? | `\d+ users` e `SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid=e.enumtypid WHERE t.typname='userrole';` |
| Há role fora de `{ADMIN,USER}`? | `SELECT role, count(*) FROM users GROUP BY role;` |
| Há `created_at` NULL? (dispara o A6) | `SELECT count(*) FROM leads WHERE created_at IS NULL;` |
| Há destino com caixa divergente? (dispara o A2) | `SELECT DISTINCT jsonb_array_elements_text(destinos::jsonb) FROM leads WHERE destinos IS NOT NULL ORDER BY 1;` |
| `crm_readonly` pode criar tabela? (fecha o A3 de vez) | `SELECT has_schema_privilege('crm_readonly','public','CREATE');` — se `t`: `REVOKE CREATE ON SCHEMA public FROM crm_readonly;` |
| Ownership para a m011 | `SELECT tablename, tableowner FROM pg_tables WHERE tablename IN ('leads','messages');` |
| Duplicatas antes da m011 | `DATABASE_URL=postgresql://... python migrations/m011_audit_unique_constraints.py` **num clone do backup**, nunca em produção — ele aborta com exit 2 e lista os ids |

## Limitação que permanece

Nenhum teste desta suíte executa contra PostgreSQL. Reduzi o gap de "invisível"
para "conhecido e travado na forma do SQL", que é bem melhor que antes e **não é
o mesmo** que ter testes de integração no dialeto real. Subir um PostgreSQL de
teste no CI continua sendo o passo que fecha isso de verdade.
