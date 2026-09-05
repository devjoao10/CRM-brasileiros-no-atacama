# Prova empírica — arquitetura de fechamento de R-B

**HEAD:** `54f160f`. Nenhum arquivo do repositório foi tocado por esta
prova — todos os scripts ficaram fora do diff rastreado, no scratchpad da
sessão, apagados ao final.

## Ambiente

- **PostgreSQL**: `postgres:16-alpine`, container efêmero `rb_pg16_proof`
  (nome usado nesta rodada — distinto do `bia_v2_r12_proof` da rodada do
  gate R12 e do container da rodada de verificação do checkpoint v2→v3,
  ambos já removidos em rodadas anteriores). Criado com
  `docker run -d --name rb_pg16_proof -e POSTGRES_PASSWORD=proof -e
  POSTGRES_USER=proof -e POSTGRES_DB=rbproof -p <porta-efêmera>:5432
  postgres:16-alpine`. **Removido ao final desta rodada**
  (`docker stop rb_pg16_proof && docker rm rb_pg16_proof`), confirmado.
- **SQLAlchemy**: 2.0.35 (mesma versão do `venv_crm` usado durante toda a
  sessão).
- Modelos usados nos scripts de prova: réplica fiel dos models reais
  (`lead_handoff_operation.py`, `lead.py`, `handoff_op.py`,
  `conversation.py`) mais as colunas novas propostas nesta rodada
  (`previous_responsavel_id`, `expected_responsavel_id`, `attempt_count`,
  `next_attempt_at`, `reconciled_at`) — criadas via `Base.metadata.create_all()`
  no banco efêmero, nunca via migration real.

## Schema temporário usado (DDL efetivamente criado no container efêmero)

```sql
-- lado CRM (simulado)
CREATE TABLE leads (id SERIAL PRIMARY KEY, responsavel_id INTEGER NULL);
CREATE TABLE lead_handoff_operations (
  id SERIAL PRIMARY KEY,
  event_id UUID NOT NULL UNIQUE,
  lead_id INTEGER NOT NULL,
  target_user_id INTEGER NOT NULL,
  previous_responsavel_id INTEGER NULL,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE lead_history (id SERIAL PRIMARY KEY, lead_id INTEGER NOT NULL,
  responsavel_id INTEGER NULL, changed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp());

-- lado Conversas (simulado)
CREATE TABLE conversations (id SERIAL PRIMARY KEY, atendente_id INTEGER NULL,
  responsavel_id INTEGER NULL, state VARCHAR(32) NOT NULL DEFAULT 'AI_TRIAGE');
CREATE TABLE handoff_operations (
  id SERIAL PRIMARY KEY,
  event_id UUID NOT NULL UNIQUE,
  conversation_id INTEGER NOT NULL,
  target_user_id INTEGER NOT NULL,
  expected_responsavel_id INTEGER NULL,
  status VARCHAR(16) NOT NULL,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NULL,
  reconciled_at TIMESTAMPTZ NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX ix_handoff_operations_pendentes ON handoff_operations (next_attempt_at)
  WHERE status IN ('REQUESTED','FAILED');
```

**Nenhum índice único parcial em `lead_handoff_operations(lead_id)`** — a
hipótese que o motivava foi refutada (seção 1 do desenho v4) antes de
qualquer prova P1-P8 ter sido rodada com ele em definitivo.

## Cenários P1-P8 — resultado observado

| # | Cenário | Comando/mecanismo | Resultado observado |
|---|---|---|---|
| **P1** | 2 threads, mesmo `lead_id`, `event_id` A/B, target 10/11, requests simultâneos (barreira `threading.Barrier`) | `SELECT...FOR UPDATE` + CAS numa transação por thread | 1 aplicou (`200`), 1 recebeu `409 LEAD_ALREADY_ASSIGNED`. **Stress: 60 rodadas, 0 falhas**, distribuição de vencedores {11: 34, 10: 26} — confirma que não há viés estrutural para "sempre o primeiro" ou "sempre o segundo" |
| **P2** | Mesmo `event_id`, mesmo payload, em paralelo | Caminho de idempotência + re-checagem sob lock | 2× `200`, exatamente 1 linha no livro-razão, exatamente 1 linha em `lead_history` — confirma que a concorrência no MESMO `event_id` não duplica efeito |
| **P3** | Mesmo `event_id`, target diferente | `_conferir_parametros` (já existente) | `409 IDEMPOTENCY_PAYLOAD_CONFLICT` em ambas as ordens (sequencial e concorrente); dono nunca muda |
| **P4** | A vence no CRM; processo "Conversas" simulado crasha antes de aplicar localmente; worker retoma depois | `handoff_to_human` chamado de novo pelo worker com a mesma identidade | `CRM.responsavel_id=10`; `Conversas: (state='WAITING_HUMAN', atendente_id=10, responsavel_id=10)`; operação `COMPLETED` — convergiu sem intervenção manual |
| **P5** | A vence no CRM; falha LOCAL permanente (simulada: outro dono já presente localmente) | Política híbrida (seção 4 do desenho v4), direção escolhida pelo tipo de falha | Divergência inicial `CRM=10 / local=7` → worker de reconciliação roda → `CRM=7 = local=7`. Rodado uma segunda vez: **estável** (idempotente, não reaplica) |
| **P6** | Timeout ambíguo depois que o CRM já commitou; retry sob o mesmo `event_id` | Caminho de replay do CRM | Retry reusa o resultado canônico (`replayed=true`), 1 única linha no livro-razão; retry com TARGET DIFERENTE sob o mesmo `event_id` → `HandoffInconsistente` (já existente do lado Conversas), dono continua 10 |
| **P7** | Segunda operação depois de um release EXPLÍCITO e legítimo | CAS contra o baseline (`None`, pós-release) | Aceita, dono passa a 11. **Contraprova no mesmo script**: sem o release, a mesma segunda operação recebe `409`, dono permanece 10 — confirma que a aceitação vem da regra explícita (baseline), não de acaso |
| **P8** | 3 processos/threads concorrentes para o MESMO `lead_id` (não apenas 2) | Mesmo mecanismo de P1 | Exatamente 1 vencedor lógico em todas as execuções. **3 PROCESSOS REAIS** (não threads do mesmo processo Python) × 10 rodadas: **10/10 com exatamente 1 vencedor, 0 falhas** |

## Provas adicionais, não solicitadas explicitamente mas executadas pela mesma disciplina de verificação

| Prova extra | Resultado |
|---|---|
| R-B fim-a-fim, 25 rodadas (pipeline completo simulado, não só o endpoint CRM isolado) | 0 divergências CRM×Conversas |
| Deadlock: CAS concorrente com um writer V1 simulado (`UPDATE` direto sem lock) | 30 rodadas, 0 deadlocks — confirmado que a ordem de aquisição de lock não cria ciclo |
| 2 instâncias do worker de retomada rodando ao mesmo tempo | Exatamente 1 das 2 reclama cada linha pendente (via `FOR UPDATE SKIP LOCKED`) — sem duplicação de trabalho, sem exceção |
| Crash simulado DURANTE a correção de reconciliação (R15) | `operation_id` derivado deterministicamente permite que o próximo ciclo repita a correção com segurança; aplicada exatamente uma vez, confirmado pelo estado final |
| CAS recusando uma correção de reconciliação já obsoleta (um terceiro writer mudou o estado entre a detecção da divergência e a tentativa de correção) | Recusada corretamente (`RECONCILIADO_JA_DIVERGIU`), não sobrescreve o writer mais recente |

## Refutação da hipótese original (índice único parcial) — evidência

Ver seção 1 e 2 do `bia-v2-r15-r17-design-v4.md` para o texto completo;
aqui, só o resultado bruto das duas variantes testadas:

```
=== H1 (claim nunca liberado) — CASO 6 / P7 ===
[release explicito] dono agora: None
B(target=11) -> (409, LEAD_ALREADY_RESERVED_BY_OTHER_OPERATION)
ESTADO FINAL: lead.responsavel_id=None, ledger=[(..., 10, 'ACTIVE')]
>>> nova operacao apos release: RECUSADA (permanentemente, sem TTL nem
    edição manual permitidos pelo escopo desta rodada)

=== H2 (claim liberado na mesma transação) — P1 ===
A -> 200 replayed=False | B -> 200 replayed=False
>>> aplicaram de verdade: 2  (nenhuma exclusão real)

=== H2 — CASO 5 ===
>>> dono final: 11 (SOBRESCRITA SILENCIOSA)
```

## Planos de execução medidos (custo, não só correção)

A 200k linhas sintéticas em `handoff_operations`/`lead_handoff_operations`:

- Consulta do worker (`next_attempt_at` + `status IN (...)`), com o índice
  parcial novo: `Index Scan using ix_handoff_operations_pendentes`,
  **0,124 ms**.
- Lock do lead (`SELECT ... FOR UPDATE` por PK): `Index Scan using
  leads_pkey` + `LockRows`, **0,069 ms**.
- Consulta de reconciliação (join `handoff_operations` ×
  `lead_handoff_operations` × `leads`, escopo de colunas restrito): **0,037 ms**.

## Confirmação de limpeza (rodada de prova original, P1-P8)

```bash
docker stop rb_pg16_proof
docker rm rb_pg16_proof
```

Executado e confirmado ao final da rodada de investigação. Nenhum
container, volume, ou arquivo de prova permanece. `git status --short`
antes e depois da rodada mostrou o mesmo conjunto de arquivos untracked
pré-existentes — nenhum arquivo rastreado foi tocado.

---

## Re-verificação dedicada — correção do achado CRITICAL (F1) da revisão

A rodada de prova P1-P8 acima usou, do início, uma única query de lock por
operação — por ser um script isolado, sem um passo de pré-carga separado
para checagem de 404. Por isso ela nunca exercitou o modo de falha
específico que a revisão de concorrência encontrou: o handler REAL
(`_carregar_lead`, `app/routers/v2_lead_domain.py:116-120`) precisa
carregar o lead ANTES do lock só para decidir 404, e é exatamente essa
pré-carga, combinada com uma segunda query de lock sem
`.populate_existing()`, que faz o SQLAlchemy devolver o objeto obsoleto do
identity map. Esta seção prova, isoladamente, que a forma corrigida
(seção 2.3 do desenho) fecha esse modo de falha específico — não repete
P1-P8, complementa.

### Ambiente

| Item | Valor |
|---|---|
| PostgreSQL | `16.15` (`postgres:16-alpine`), container `rb_refix_verify`, porta efêmera `127.0.0.1:32768` |
| Isolamento | `read committed` (default; confirmado via `SHOW default_transaction_isolation`) |
| SQLAlchemy | `2.0.35` — exatamente o pin de `requirements.txt` |
| psycopg2 | `2.9.12` · Python `3.12.10` |

Ressalva de fidelidade, registrada sem suavizar: não existe diretório
`venv_crm` (nem qualquer `venv*`) neste repositório — o Python usado foi o
do PATH do executor, que já tinha o SQLAlchemy `2.0.35` correto instalado
(a versão certa, ainda que não o venv nomeado).

### Comandos exatos

```bash
docker run -d --name rb_refix_verify -e POSTGRES_PASSWORD=rbverify -e POSTGRES_USER=rbverify \
  -e POSTGRES_DB=rbverify -p 127.0.0.1:0:5432 postgres:16-alpine
docker port rb_refix_verify 5432                    # -> 127.0.0.1:32768
export RB_DSN="postgresql+psycopg2://rbverify:rbverify@127.0.0.1:32768/rbverify"
python rb_setup.py                                  # schema leads + handoff_ops (+ barrier_slots)
python rb_all.py threads 40 2                       # 2 threads, 40 rodadas x 3 variantes
python rb_all.py procs   10 3                       # 3 processos reais, 10 rodadas x 3 variantes
python rb_control.py                                # controle determinístico, sem corrida
docker stop rb_refix_verify && docker rm rb_refix_verify
```

Três variantes, mesmo container, mesmo schema, mesma barreira, mesmo
CAS/UPDATE/INSERT/commit — a ÚNICA diferença é a forma da query que toma
o lock:

- **`broken`** — pré-carga sem lock + 2ª query `.with_for_update().first()`
  na mesma sessão, sem `.populate_existing()` (a forma que a v4 original,
  antes da revisão, não excluía explicitamente).
- **`fixed_populate`** — query única
  `.with_for_update().populate_existing().first()`, sem pré-carga
  separada (seção 2.3, primeira forma).
- **`fixed_refresh`** — pré-carga + `db.refresh(lead, with_for_update=True)`
  (seção 2.3, segunda forma — o idioma já usado em `handoff.py:485`).

### Resultado — Cenário A: 2 threads, `threading.Barrier`, 40 rodadas por variante

| Variante | Rodadas OK | Rodadas FALHA | Sucessos totais (esperado 40) | Rodadas c/ >1 sucesso | Ledger c/ >1 linha | Leituras stale | CAS passou c/ valor stale | Erros |
|---|---|---|---|---|---|---|---|---|
| **broken** | 0 / 40 | **40 / 40** | 80 | 40 | 40 | 40 | 40 | 0 |
| **fixed_populate** | **40 / 40** | 0 / 40 | 40 | 0 | 0 | **0** | **0** | 0 |
| **fixed_refresh** | **40 / 40** | 0 / 40 | 40 | 0 | 0 | **0** | **0** | 0 |

### Resultado — Cenário B: 3 processos Python reais (interpretadores separados), barreira via banco em conexão dedicada, 10 rodadas por variante

| Variante | Rodadas OK | Rodadas FALHA | Sucessos totais (esperado 10) | Rodadas c/ >1 sucesso | Ledger c/ >1 linha | Leituras stale | CAS passou c/ valor stale | Erros |
|---|---|---|---|---|---|---|---|---|
| **broken** | 0 / 10 | **10 / 10** | 30 | 10 | 10 | 20 | 20 | 0 |
| **fixed_populate** | **10 / 10** | 0 / 10 | 10 | 0 | 0 | **0** | **0** | 0 |
| **fixed_refresh** | **10 / 10** | 0 / 10 | 10 | 0 | 0 | **0** | **0** | 0 |

Total: 150 rodadas, 330 operações de worker, **0 erros/timeouts em
qualquer variante** (a diferença entre `broken` e `fixed_*` é 100%
lógica — sobrescrita silenciosa — nunca uma exceção).

### Falhas concretas (amostras brutas, forma `broken`)

```
2-threads / broken — lead_id=1, sucessos=2, ledger=[200, 201], dono_final=201
  worker0: OK  obj_value=100  db_value=100  novo=200
  worker1: OK  obj_value=100  db_value=200  novo=201     <-- objeto mentiu

3-processos / broken — lead_id=121, sucessos=3, ledger=[201, 200, 202], dono_final=202
  worker0: OK  obj_value=100  db_value=201  novo=200     <-- objeto mentiu
  worker1: OK  obj_value=100  db_value=100  novo=201
  worker2: OK  obj_value=100  db_value=200  novo=202     <-- objeto mentiu
```

`db_value` é um `SELECT` cru na MESMA conexão, sob o MESMO lock, no MESMO
instante do CAS. A assinatura da corrupção é `obj_value=100` (o que o CAS
leu) contra `db_value=201` (o que o banco já tinha commitado). O
livro-razão fica com 2-3 linhas para um lead que só pode ter um dono: as
N-1 primeiras registram handoffs que foram silenciosamente sobrescritos —
lost update com trilha de auditoria falsa.

### Controle determinístico (sem corrida) — isola o mecanismo causal

Sessão A pré-carrega; sessão B commita `responsavel_id=999`; sessão A
executa só a etapa de lock:

| Forma | SQL emitido | `obj is obj_precarga` | Valor lido pelo CAS | `SELECT` cru na mesma conexão | Veredito |
|---|---|---|---|---|---|
| `.with_for_update().first()` | `... LIMIT %(param_1)s FOR UPDATE` | `True` | **100** | **999** | **STALE** |
| `.with_for_update().populate_existing().first()` | `... LIMIT %(param_1)s FOR UPDATE` | `True` | 999 | 999 | consistente |
| `db.refresh(lead, with_for_update=True)` | `... WHERE leads.id = %(pk_1)s FOR UPDATE` | — | 999 | 999 | consistente |

Isto fecha as duas metades separadamente: o `FOR UPDATE` **é** emitido na
forma quebrada — o lock é tomado de verdade, não é caso de lock ausente —
e AINDA ASSIM o SQLAlchemy devolve o objeto do identity map com o valor
obsoleto. `.populate_existing()` (ou o `refresh()` equivalente) é
exatamente o que muda o desfecho, isolado de qualquer outro fator.

### Divergência em relação à expectativa informal, registrada sem suavizar

A expectativa usada para orientar o despacho desta verificação (baseada na
descrição textual anterior, não numérica-mente re-verificada, do achado
original da revisão) era ~39/40 na forma quebrada com 2 threads e 8/8 com
3 processos. O resultado observado foi **40/40 (100%)** com 2 threads e
**10/10 (com 3 sucessos por rodada, não 2)** com 3 processos — uma falha
MAIS consistente, não menos. Causa identificada, não um erro de medição:
a barreira desta rodada foi posicionada exatamente entre a pré-carga e a
etapa de lock, garantindo que todos os workers já tenham o snapshot
obsoleto antes de disputar o lock — uma janela de colisão estrutural mais
larga que a de qualquer reprodução anterior. Nenhum outro desvio: a forma
corrigida deu zero falhas em ambas as formas de concorrência, nas duas
variantes equivalentes, de forma consistente com o que a correção prediz.

### Limpeza desta rodada

```bash
docker stop rb_refix_verify && docker rm rb_refix_verify
```

Confirmado: `docker ps -a --filter name=rb_refix_verify` retorna vazio.
Os scripts ficaram só em um diretório temporário fora do repositório e
foram apagados ao final (diretório confirmado inexistente depois).
`git status --short` antes e depois desta rodada é IDÊNTICO — mesmas
entradas untracked pré-existentes, nenhuma nova. `HEAD` inalterado em
`54f160f`; `git diff --stat HEAD` vazio. Nenhum arquivo rastreado tocado,
nenhum arquivo escrito dentro do repositório.

**Conclusão**: a correção especificada na seção 2.3 do desenho é
confirmada empiricamente. Ambas as formas equivalentes —
`.with_for_update().populate_existing().first()` como query única, e
`db.refresh(lead, with_for_update=True)` a partir da pré-carga — eliminam
a falha por completo (0/50 rodadas com falha cada, contra 50/50 de falha
na forma quebrada), nas mesmas condições e no mesmo container.
