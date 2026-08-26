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

---

# Rodada 2026-08-26 — validações executadas contra PostgreSQL 16 real

Container `bna-postgres-audit` (PostgreSQL 16.14, porta 55432, banco
`bna_app_audit`, descartável). **Nenhum outro container foi tocado.**

## 1. `m012` — coluna `primeira_resposta_humana_at`

```
[m012] alvo (conversas): 127.0.0.1:55432/bna_app_audit
[m012]   primeira_resposta_humana_at:added (TIMESTAMP WITH TIME ZONE)
[m012]   ix_conversations_primeira_resposta_humana_at:ensured
[m012]   backfill-em-atendimento:0
[m012]   primeira_resposta_humana_at:verificado
[m012] OK — coluna aplicada (idempotente)

(2ª execução)
[m012]   primeira_resposta_humana_at:already-present
[m012] OK — NO-OP (já estava aplicada)
```

Backfill conferido nas **seis** combinações de estado, com linhas reais:

| conversa | status | bot | atendente | outbound | marcada? | esperado |
|---|---|---|---|---|---|---|
| 55WA1 | aberta | off | 7 | sim | sim | sim |
| 55WA2 | aberta | off | — | sim | não | não (está na fila) |
| 55WA3 | aberta | off | 7 | não | não | não (nunca falou) |
| 55WA4 | aberta | **on** | — | sim | não | não (está com a Bia) |
| 55WA5 | encerrada | off | 7 | sim | não | não |
| 55WA6 | aguardando | off | 7 | sim | sim | sim (status legado aberto) |

Segunda execução não alterou nenhum valor. Linhas de teste removidas ao final.

## 2. F-341 — `FunnelEntry` sob concorrência

Duas threads, duas conexões, disputando o MESMO `(lead_id, funnel_id)`:
nenhuma exceção, as duas convergem para a mesma entry, **uma** linha ao final.
É o caminho `IntegrityError`-como-fluxo-normal apoiado em
`uq_funnel_entries_lead_funnel` — o SQLite não o exercita, porque nele a
violação de UNIQUE sob concorrência não acontece.

## 3. F-043 — escape de NUL derruba o filtro de campo personalizado

**Reproduzido, e não por leitura de código.** Com
`{"origem":"\u0000instagram"}` numa linha:

```
  OK    json aceita o escape: True
  FALHA jsonb aceita o escape?: UntranslatableCharacter: unsupported Unicode escape sequence
  -- como o filtro montava a query (cast FORA do CASE) --
  FALHA filtro atual: UntranslatableCharacter
  -- com o guard de TEXTO antes do cast --
  OK    com guard: 1
```

Uma única linha legada envenenada derrubava a consulta **inteira** — o filtro de
campo personalizado e todo segmento que o usasse viravam 500 permanente para
TODOS os leads, não só para o lead envenenado.

Depois da correção, executando o predicado REAL (`campo_personalizado_match`)
via SQLAlchemy contra o banco, com cinco linhas (uma envenenada, duas boas, uma
lista e um `null`):

```
  PASS: a consulta EXECUTA com a linha envenenada presente (ids=[3])
  PASS: devolve só a linha boa que casa (esperado [3], obtido [3])
  PASS: presença da chave: esperado [2, 3], obtido [2, 3]
  PASS: a linha envenenada fica invisível ao filtro (não derruba a query)
  PASS: JSON que não é objeto continua ignorado
```

A linha envenenada some **daquele filtro**, em vez de a funcionalidade sumir
para todo mundo. Dado novo não entra assim: `_rejeita_nul` em
`app/schemas/lead.py` já recusa na borda; a correção é para o legado.

A prova de comportamento vive aqui, e não na suíte: o SQLite não tem `jsonb` e
**nunca** reproduz este defeito. Na suíte ficou o travamento da FORMA
(`tests/test_postgres_dialect_divergence.py`, seção 10), que falha se o cast
voltar para fora do `CASE` — que era exatamente o defeito.

## 4. Smoke CRM ↔ Conversas de ponta a ponta

**Os dois serviços de verdade, em processos separados, contra o mesmo
PostgreSQL 16** (os pacotes se chamam `app` nos dois e não podem coexistir num
processo só). Prova a cadeia inteira da Wave 1 a partir do **único sinal
determinístico que o repositório recebe no handoff** — a rota que o
`Tool Alterar Responsavel` do Gerenciador já chama:

```
PUT  crm:8100/api/leads/{id}/responsavel?responsavel_id=<humano>
  → ponte HTTP (app/services/conversas_bridge.py)
POST conversas:8101/api/conversations/by-lead/{id}/handoff
```

```
2 — estado inicial: com a Bia, fora da fila
  PASS: conversa comeca com a Bia ligada
  PASS: conversa NAO esta na fila
  PASS: conversa sem atendente
  PASS: a conversa NAO aparece na FILA DE ESPERA ainda

3 — o unico sinal que o n8n manda: PUT /api/leads/{id}/responsavel
  PASS: CRM aceita a troca de responsavel (got 200)
  PASS: o lead ficou com a Julia

4 — a PONTE moveu a conversa, sem ninguem chamar o Conversas
  PASS: a Bia foi desligada
  PASS: a conversa ENTROU na fila de espera
  PASS: o atendente elegivel foi resolvido (got 3, esperado 3)
  PASS: ninguem respondeu ainda — ela CONTINUA esperando
  PASS: o nome do atendente aparece (got 'Julia Smoke')
  PASS: a conversa APARECE na FILA DE ESPERA
  PASS: ATRIBUIDA nao e ATENDIDA: nao aparece em 'meus atendimentos'
  PASS: o badge 'aguardando humano' conta (got 2)

5 — abrir a conversa NAO tira da fila
  PASS: abrir preserva a posicao na fila
  PASS: abrir NAO conta como atendimento

6 — retry do handoff e idempotente
  PASS: retry NAO manda a conversa para o fim da fila
```

```
7 — cliente ENCERRA, volta a escrever, e o handoff acontece DE NOVO
  PASS: cliente que volta cai de novo com a Bia
  PASS: e fora da fila
  PASS: segundo handoff aceito (got 200)
  PASS: SEGUNDO handoff com o MESMO responsavel tambem desliga a Bia
  PASS: e a conversa entra na fila DE NOVO — sem isto o cliente que volta fica preso
```

22/22, zero falhas. Usuários, lead e conversa do smoke removidos ao final.

A seção 7 foi acrescentada depois da revisão de código, que apontou um defeito
real na primeira versão da ponte: ela só disparava quando `responsavel_id`
**mudava**. Como o n8n manda o id FIXO e nada devolve `lead.responsavel_id` a
NULL no encerramento, o segundo handoff do mesmo lead era pulado — e o
Conversas, que reseta a conversa para a Bia quando um cliente encerrado volta a
escrever, deixava esse cliente preso ali. RED confirmado: restaurando o guard
antigo, as duas últimas asserções falham.

É a prova de que o defeito principal desta rodada acabou: antes, a Bia dizia ao
cliente que ele estava na fila e **nada** acontecia do lado do inbox, porque
nenhum nó do n8n alcança a porta 8001. Agora o sinal que o n8n **já manda** move
a conversa — e ela fica na fila, com dono, até alguém responder de verdade.

Este smoke depende de `CONVERSAS_API_KEY` estar definida no ambiente do CRM.
Sem ela a ponte é no-op silencioso (é o comportamento de hoje, e nada regride) —
ver M7 em `N8N_MANUAL_CHANGES.md`.
