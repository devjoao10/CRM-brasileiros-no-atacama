# RELEASE_READINESS.md

Estado do sistema BnA ao fim da auditoria + estabilização global.

**Branch:** `audit/full-system-stabilization-2026-08-24`
**Base:** `d4831486b767988ed2b91518167d8c50fbeb636e` (HEAD de `main`)
**Commits:** 20 · `126 files changed, 11450 insertions(+), 796 deletions(-)`

> **Nenhum deploy foi feito. Nenhum dado de produção foi tocado. Nenhuma
> migration foi executada. Nenhum merge foi feito.** Este documento existe para
> que a decisão de liberar seja de quem tem autoridade para tomá-la, com os
> números reais na mão.

---

---

# FASE 2 — reconciliação com o n8n real (2026-08-25)

> Esta seção tem **precedência** sobre o que vem depois dela neste documento.
> Onde divergirem, vale a Fase 2: ela foi escrita com os três workflows de
> produção na mão, coisa que a Fase 1 não tinha.

**Entrada nova:** os exports dos três workflows n8n **realmente em produção**,
fornecidos como evidência externa. Versionados em
`n8n/workflows/live_exports/20260825_fase2/` (sem segredo — só referência de
credencial por ID). Detalhe completo em
`docs/audit/N8N_CURRENT_STATE_RECONCILIATION.md`.

## CURRENT N8N WORKFLOWS RECONCILED

| Workflow | webhook | nós | situação |
|---|---|--:|---|
| WF-01 Agente Bia | `POST /webhook/agent-bia` | 14 | reconciliado |
| Agente Gerenciador de Leads — BnA | `POST /webhook/gerenciador-leads` | 18 | reconciliado |
| Formulário do Site → CRM BnA | `POST /webhook/formulario-site` | 16 | reconciliado — **nunca auditado antes** |

## NOTIFIER REMOVED FROM CURRENT ARCHITECTURE

**Sim, e a arquitetura documentada foi corrigida.** O Notificador não está em
produção e não deve ser recriado. *Colocar na fila humana* ≠ *notificar
atendente* — o próprio system message da Bia já codifica essa separação, em
quinze formulações.

**Mas a dependência morta continua viva no Gerenciador:** o nó
`Tool Acionar Notificador` está lá, conectado como ferramenta, apontando para
`/webhook/notificacao`. Removê-lo é o item **M2** de `N8N_MANUAL_CHANGES.md`.

## N8N CHANGES REQUIRED / DEPLOYED

**Requeridas: 5 mudanças de campo (M1–M5) + 7 decisões (D1–D7).
Aplicadas em produção: 0.** Nenhuma podia ser aplicada daqui — não tenho acesso
ao n8n e não tentei obtê-lo. Instruções campo a campo, com valor antigo, valor
novo, teste manual e rollback, em `docs/audit/N8N_MANUAL_CHANGES.md`. JSONs de
referência em `docs/audit/proposed_n8n/`, marcados
`PROPOSED ONLY — NOT DEPLOYED`.

As duas que mais importam:

**M1 — o sinal de entrada na fila humana sai como `"=true"`.** O campo
`pronto_para_humano` começa com **dois** sinais de igual. No n8n o primeiro `=`
marca a expressão e o resto é template, então o `=` sobrando vira texto. O
consumidor compara literalmente com `"true"`/`"false"` e **nenhum ramo casa**.
Não é falha determinística — é ambiguidade determinística na transição de estado
mais importante do sistema. Correção: apagar um caractere.

**M2 — remover `Tool Acionar Notificador`.** Alcançável sempre que o modelo lê
`pronto_para_humano` como verdadeiro (ou seja, na decisão já ambígua de M1),
devolve 404, e o agente do Gerenciador **não tem ramo de erro** — pode deixar o
lead criado sem tag e sem responsável.

## Findings anteriores INVALIDADOS pela evidência nova

| Antes | Agora |
|---|---|
| **"Um webhook entrega método E URL à escolha de um LLM"** (F-022, F-023, CRITICAL) | **OBSOLETE** — isso vivia em `Gerente_Autonomo_de_Tarefas_IA`, que **não está em produção**. Nos três atuais, toda URL de tool é fixa. |
| F-021 — prompt do Gerente Autônomo montado a partir de tarefa do CRM | **OBSOLETE** — mesmo workflow ausente |
| F-025 — parâmetro `whatsapp` escolhido livremente pelo LLM | **FALSE_POSITIVE** — no export atual é `fieldValue` vindo do webhook do Conversas |
| F-019/F-020 — "14 tools de escrita" | **UPDATE** — são 13 de CRM + 1 morta |
| F-024 — prompt sem defesa de injeção | **HIGH**, não CRITICAL — a Bia atual tem 3 ferramentas e ganhou validação de saída; a injeção envenena dado, não executa ação arbitrária |

## O que a Fase 2 corrigiu no repositório

Cinco defeitos, todos com teste que exercita comportamento:

1. **`PUT /api/leads/{id}` devolvia 422 em toda atualização sem nome novo.** A
   `Tool Atualizar Lead` tem corpo fixo e manda `""` no que não coletou. O dado
   que a Bia acabara de coletar era descartado em silêncio. Pré-existente.
2. **Reação de emoji recebia pedido de desculpas por instabilidade.** Silêncio
   deliberado da Bia era tratado como degradação. Metade feita aqui; a outra é M3.
3. **`_INFRA_ERRORS` era dialeto-dependente** (§ PostgreSQL abaixo).
4. **`SELECT ... INTO` passava por todos os guards da ferramenta de IA.**
5. **Corrida de primeiro contato**, tratada **antes** de a m011 rodar.

E um risco introduzido pela Fase 1 foi revertido: o `pattern` de slug em
`StageSchema.id` quebraria qualquer funil de produção cuja etapa tenha espaço —
e o próprio Gerenciador chama a etapa de "Sem Contato".

## POSTGRESQL TEST ENVIRONMENT

**Não foi possível subir.** `docker info` expira (daemon parado) e não há `psql`
nem `pg_dump` instalados — verificado, não suposto.

O que foi feito no lugar, e é real: `psycopg2` está instalado e o SQLAlchemy
**compila** contra o dialeto `postgresql` sem conexão. Cada query relevante foi
compilada contra os **dois** dialetos e o SQL comparado; o mapeamento de erro foi
resolvido por `psycopg2.errors.lookup(SQLSTATE)`. Resultado: **9 divergências**,
3 corrigidas, 6 documentadas com o comando exato que o operador roda para fechar.
Detalhe em `docs/audit/POSTGRES_VALIDATION.md`.

- **POSTGRESQL INTEGRATION TESTS:** `tests/test_postgres_dialect_divergence.py`,
  78 checks, sem servidor. Isso **não é** teste de integração no dialeto real —
  é travamento da forma do SQL. Subir um PostgreSQL no CI continua pendente.
- **POSTGRESQL MIGRATIONS:** não executadas contra PostgreSQL.
- **M011:** ramo PostgreSQL **inspecionado e aprovado** (SQL capturado e lido);
  **não executado** fora de SQLite descartável. Duas pré-condições operacionais:
  ownership das tabelas, e aplicar antes a correção da corrida de primeiro
  contato — sem ela, o índice único troca "conversa duplicada" por **mensagem de
  cliente perdida**.

## BACKUP / RESTORE

**O achado mais importante desta fase é sobre o trabalho da fase anterior.**

O script de backup que a Fase 1 dava por corrigido **abortaria todo backup
real**. `grep -q` sai no primeiro casamento e mata o `gzip` a montante com
SIGPIPE; com `set -o pipefail`, o status do pipeline vira 141 e o `if !` inverte
a guarda. Reproduzido de forma independente:

```
gzip -dc ok.gz | grep -qE "COPY (public\.)?users\b"
  pipefail LIGADO  : 141      <- guarda invertida
  pipefail DESLIGADO:   0
```

E a guarda anti-CR era decoração fora do Linux: o `grep` da família Cygwin/MSYS
descarta o CR final de cada linha antes de casar — justamente o CR que o
pseudo-TTY produz.

**Por que a Fase 1 não pegou:** o teste dela verificava o **texto** do script
(`"gzip -t" in sh`) e nunca o **executava**. É a classe de defeito que esta
auditoria mais encontrou — e estava no meu próprio trabalho.

- **BACKUP GENERATED:** sim — script executado ponta a ponta com um `docker`
  falso emitindo dump plain-format de 1.196.760 bytes.
- **BACKUP RESTORED:** sim, por dois métodos — igualdade byte a byte (zero CR) e
  restore de verdade num SQLite, com as quatro tabelas conferidas por contagem
  **e** por valor. `sha256sum -c` executado com sucesso.
- 7 cenários exercitados, incluindo o de regressão do defeito original.
- `tests/test_backup_restore_e2e.py`, 45 checks. Contra o script anterior dava
  **9 falhas**.
- **Não provado sem PostgreSQL real:** que `pg_dump` produz este formato e que
  `psql -f` reconstrói constraints, índices, sequences e FKs. Comando do operador
  em `docs/audit/BACKUP_RESTORE_VALIDATION.md`.
- **Backups históricos continuam suspeitos**, agora por dois motivos.

## FINDINGS — estado após a Fase 2

606 findings (588 da Fase 1 + 18 novos).

| Estado | Qtd |
|---|---:|
| OPEN | 307 |
| ADDRESSED_UNVERIFIED | 130 |
| RESOLVED | 94 |
| BLOCKED_OPERATOR | 66 |
| PROPOSED_FIX (n8n, aguardando o operador) | 4 |
| OBSOLETE (invalidados pela evidência nova) | 3 |
| RESOLVED_PARCIAL (metade feita, metade é n8n) | 1 |
| FALSE_POSITIVE | 1 |

Não resolvidos nem bloqueados, por severidade:
**CRITICAL 0** · HIGH 108 · MEDIUM 234 · LOW 95.

**ADDRESSED_UNVERIFIED caiu de 151 para 130.** A queda não é mecânica: 34
findings do raio de impacto dos workflows foram adjudicados **lendo o código de
hoje**, com evidência citada por finding — 21 viraram RESOLVED, 4
BLOCKED_OPERATOR, e **14 continuaram OPEN** porque a região do código mudou mas o
defeito descrito não foi tocado. Esses 14 são o resultado mais útil da
adjudicação, e os quatro de maior consequência são:

- **F-341** — leads criados pelo n8n via `POST /api/leads` continuam **sem
  `FunnelEntry`**. É o único finding do escopo que quebra diretamente o fluxo de
  produção das automações, e ninguém mexeu nele.
- **F-098 + F-099** — a corrida e a exaustão de pool do debounce estão como
  estavam; a Fase 1 corrigiu o *corte do lote*, não o *reentrante*.
- **F-106** — o boundary CRM↔Conversas continua com **zero cobertura
  executável**: `auto_link_conversation` está substituído por um no-op em 10
  arquivos de teste. Nenhuma das correções de `crm.py` tem teste que as trave.

Uma observação de qualidade sobre o próprio CSV: os 34 findings de `webhook.py`
correspondem a ~20 defeitos distintos (F-092≡F-093, F-325≡F-326, F-327≡F-535,
F-330≡F-331, F-333≡F-536, F-324≡F-532, F-538≡F-539, F-101≡F-336≡F-537). Qualquer
contagem por severidade derivada do CSV bruto está inflada em ~40% nesse arquivo.

## TEST SUITE / REGRESSION

| | Baseline | Fase 1 | Fase 2 |
|---|---|---|---|
| Arquivos de teste | 51 | 64 | **68** |
| Resultado | 51/51 | 64/64 | **68/68** |

Novos nesta fase: `test_n8n_contract_lead_update.py` (bate na **rota**, com o
corpo reconstruído do export de produção), `test_conversas_agent_silence.py`,
`test_postgres_dialect_divergence.py` (78 checks), `test_backup_restore_e2e.py`
(45 checks).

## Um erro meu, corrigido dentro da própria fase

Registro porque o processo que o pegou vale mais que o defeito.

A **primeira** versão da correção do `PUT /api/leads` convertia string vazia em
`None` no schema e validava com `model_dump(exclude_none=True)`. **O router usa
`exclude_unset`**, que remove o que não foi *enviado* — e a ferramenta envia
tudo. O efeito real seria `setattr(lead, "nome", None)` contra uma coluna
`NOT NULL`: **500 com transação abortada, pior que o 422 original**. E o teste
passava verde sobre um comportamento que não existe em produção.

Um revisor independente derrubou isso, eu verifiquei executando, e a correção
certa é outra: string vazia faz a **chave ser descartada**, então o campo deixa
de estar em `model_fields_set` e o `exclude_unset` o ignora sozinho. `null`
explícito continua limpando o campo — que é o que a interface manda. Os dois
consumidores já usavam a distinção corretamente; o schema é que a destruía.

O teste passou a bater na **rota**, não no schema. Era exatamente aí que eu tinha
errado.

## VEREDITO DA FASE 2

| Campo | Estado |
|---|---|
| **CRITICAL** | 29 (Fase 1) + 2 novos = 31 · **0 abertos e não bloqueados** |
| **HIGH** | 108 não resolvidos nem bloqueados |
| **MEDIUM** | 234 |
| **LOW** | 95 |
| RESOLVED | 94 |
| OPEN | 307 |
| BLOCKED_OPERATOR | 66 |
| PROPOSED_FIX (n8n) | 4 |
| OBSOLETE | 3 |
| FALSE_POSITIVE | 1 |
| ADDRESSED_UNVERIFIED restantes | **130** (era 151) |
| **CURRENT N8N WORKFLOWS RECONCILED** | **3 de 3** |
| **N8N CHANGES REQUIRED** | 5 de campo (M1–M5) + 7 decisões (D1–D7) |
| **N8N CHANGES DEPLOYED** | **0** — nenhuma podia ser aplicada daqui |
| **NOTIFIER REMOVED FROM CURRENT ARCHITECTURE** | sim na documentação e nos findings; **o nó morto continua no Gerenciador** (M2) |
| **POSTGRESQL TEST ENVIRONMENT** | **não** — sem Docker, sem psql (verificado) |
| **POSTGRESQL MIGRATIONS** | não executadas |
| **POSTGRESQL INTEGRATION TESTS** | não. 78 checks de **compilação de dialeto**, que é outra coisa |
| **M011** | ramo PostgreSQL inspecionado e **aprovado**; **não executado** |
| **BACKUP GENERATED** | **sim**, ponta a ponta |
| **BACKUP RESTORED** | **sim**, por dois métodos, com dados conferidos |
| **CREDENTIAL ROTATION** | **não** — e a Fase 2 descobriu que ela derruba os 3 workflows se o n8n não for atualizado junto (D5) |
| **SECURITY REVIEW** | sim — trust boundary reavaliado por webhook, um finding CRITICAL invalidado, dois novos |
| **AIA-HARNESS REVIEW** | `doctor` + `scan` executados. Achado: o harness **nunca foi aplicado** neste projeto |
| **CAVEMAN REVIEW** | sim — 3 críticas, 1 aceita, 2 rejeitadas com razão técnica |
| **PONYTAIL REVIEW** | **não executado** — ver limitação abaixo |
| **CODE REVIEW** | sim — e derrubou a primeira versão de uma correção minha |
| **TEST SUITE** | **68/68** (baseline 51/51 → Fase 1 64/64) |
| **REGRESSION** | sem regressão |
| **GRAPHIFY / STRUCTURAL IMPACT** | Graphify indisponível; medida própria por AST, 0 violações de fronteira |

### EXTERNAL BLOCKERS

1. **Credencial de produção viva e exposta no histórico do git.** Agravante novo:
   rotacionar sem atualizar a credencial `CRM Brasileiros API` no n8n **derruba
   os três workflows juntos** (D5).
2. **Dois webhooks service-to-service abertos na internet** (D1, D2), um deles
   interpolando corpo anônimo no prompt de um agente com 13 ferramentas de
   escrita no CRM.
3. **O sinal de entrada na fila humana está ambíguo em produção** (M1) e a
   ferramenta morta do Notificador continua conectada (M2).
4. **Nenhum backup histórico é confiável**, e nenhum restore contra PostgreSQL
   real foi feito.
5. **Nenhum teste roda em PostgreSQL.** Reduzi de "invisível" para "conhecido e
   travado na forma do SQL" — não é o mesmo que testar no dialeto real.
6. **`models/gemini-3.5-flash-lite` não foi verificado** (D4). Se não existir, os
   dois agentes falham sempre e o fallback mascara.

### VERDICT

**NOT READY FOR RELEASE VALIDATION**

Justificativa, sem rodeio:

O **código** melhorou de novo e de forma verificável: 68/68, zero CRITICAL
aberto e não bloqueado, cinco defeitos reais fechados nesta fase, cada um com
teste que exercita comportamento em vez de conferir string.

O que impede não é o código:

1. **Nada do n8n foi aplicado.** Cinco mudanças de campo estão descritas com
   valor antigo, valor novo, teste e rollback — e **zero** foram feitas. Duas
   delas (M1, M2) estão no caminho da decisão mais importante do sistema.
2. **A credencial continua viva**, e agora sabemos que rotacioná-la sem
   coordenar com o n8n para os três workflows.
3. **Backup: melhorou muito e ainda não fecha.** O script foi executado,
   restaurado e validado — mas contra um `pg_dump` falso, e a descoberta de que
   a versão da Fase 1 abortaria todo backup real deveria calibrar a confiança em
   qualquer "corrigido" que não tenha sido executado.
4. **PostgreSQL continua sem teste de integração.**

**O caminho para READY,** e continua não dependendo de mais programação:
aplicar M1–M5 · decidir D1–D7 · rotacionar a chave na ordem de D5 · rodar a m011
contra um clone do backup · subir um PostgreSQL de teste e rodar a suíte nele ·
gerar um backup e **restaurá-lo** de verdade.

### Limitação declarada: Ponytail

O plugin Ponytail está instalado, mas eu **não o executei**. Caveman entrou como
crítico independente do plano e produziu três críticas concretas; o segundo
crítico independente desta fase acabou sendo o revisor de findings, que derrubou
a primeira versão de uma correção minha. Registro a ausência em vez de alegar
uma revisão que não houve.

---

---

# FASE 1 — registro original (mantido)

## 1. O veredito, em uma frase

O repositório está em condição **melhor e mensurável** — 0 CRITICAL fixáveis em
código continuam abertos, a suíte inteira passa, e cada correção tem teste —
mas **três coisas que só um operador pode fazer continuam pendentes, e uma delas
é uma credencial viva exposta**. Enquanto elas não forem feitas, liberar não é
uma decisão técnica de código.

> **Nota da Fase 2:** este parágrafo e a seção 9 abaixo são o registro da Fase 1
> e foram mantidos como estavam. O veredito que vale é o do fim da seção da
> Fase 2, acima — ele incorpora a evidência dos workflows reais e dois defeitos
> graves descobertos no próprio trabalho da Fase 1.

---

## 2. Gates — antes e depois

| Gate | Baseline (antes) | Agora |
|---|---|---|
| Suíte (um processo por arquivo, como o CI) | **51/51 PASS** (51 arquivos) | **64/64 PASS** (64 arquivos) |
| Arquivos de teste | 51 | 64 (**+13**) |
| Lint | não existe | não existe |
| Typecheck | não existe | não existe |
| E2E / navegador | não existe | não existe |
| Security scan | não existe | não existe |
| Mutation | não existe | não existe |
| Build Docker | não executado (sem daemon nesta máquina) | não executado (mesma limitação) |
| Portão de sintaxe (py + Jinja2 + JSON + NUL) | não existia | limpo — 389 arquivos |

O baseline de 51/51 foi **corrigido durante a auditoria**: `tests/test_hub.py`
aparecia vermelho por um timeout de 300 s meu contra um `import
google.generativeai` de **36,2 s medidos**, não por defeito do produto. Com
orçamento de 900 s ele passa. Registrar um vermelho falso teria contaminado toda
a comparação seguinte.

---

## 3. Findings — estado depois da reauditoria

588 findings brutos. Estado atual:

| Estado | Qtd | O que significa |
|---|---:|---|
| **RESOLVED** | 64 | corrigido **e** com teste de regressão nomeado |
| **BLOCKED_OPERATOR** | 62 | correção fora do repositório (produção, n8n, Traefik, banco, histórico git) |
| **ADDRESSED_UNVERIFIED** | 151 | a região de código mudou, mas **não afirmo** que aquele defeito específico acabou |
| **OPEN** | 311 | não tocado |
| **FALSE_POSITIVE** | 0 | (o único derrubado foi absorvido pela regra de região) |

Por severidade, o que **não** está resolvido nem bloqueado:

| Severidade | Não resolvidos | Total |
|---|---:|---:|
| **CRITICAL** | **0** | 29 |
| HIGH | 117 | 159 |
| MEDIUM | 247 | 286 |
| LOW | 98 | 114 |

**Como esses estados foram atribuídos — leia antes de citar o número.**
Não houve revisão manual dos 588, e dizer que houve seria mentira. A regra é
mecânica, reproduzível e está escrita no cabeçalho de `adjudicar.py`:

- a "região que mudou" é calculada nas linhas do **lado antigo** do diff contra
  o merge-base — a mesma numeração usada quando os findings foram escritos;
- `RESOLVED` exige região alterada **e** arquivo coberto por teste desta
  auditoria, **ou** um override explícito meu, com a razão e o teste gravados na
  coluna `adjudicacao` do `FINDINGS.csv`;
- `ADDRESSED_UNVERIFIED` é deliberadamente conservador: quer dizer "o código
  daquele ponto não é mais o mesmo", não "está consertado".

Os 29 CRITICAL foram tratados **um a um**, com override nomeado. Os 22 que
constam como `BLOCKED_OPERATOR` estão na seção 4.

---

## 4. O que só o operador pode fazer

Em ordem de urgência. Nenhum item abaixo foi executado por esta missão.

### 4.1 — URGENTE: uma API key do CRM está viva e exposta

`docs/n8n-toolHttpRequest-guia.md:180` continha uma chave de API do CRM em texto
claro, no formato exato de `generate_api_key()`, commitada desde `7fd122b`. O
arquivo foi corrigido nesta branch.

**Isso não contém o vazamento.** A chave continua válida e continua no histórico
do git. Chaves **não expiram**: `API_KEY_EXPIRY_DAYS` é lido em `config.py` e
não é usado em lugar nenhum. Qualquer `bna_...` válido autentica em **todas** as
rotas `/api/*` do CRM **e** do Conversas, porque os dois compartilham a tabela
`users`.

Ações, nesta ordem:
1. **Rotacionar** a chave (revogar a atual, emitir outra, atualizar as
   credenciais do n8n que a usam).
2. **Purgar o histórico** (`git filter-repo` ou equivalente) e forçar a
   reescrita nos clones.
3. Implementar de fato o `API_KEY_EXPIRY_DAYS`, ou remover a variável — hoje ela
   promete uma proteção que não existe.

### 4.2 — n8n é um control plane público sem autenticação

Três webhooks `POST` **abertos à internet, sem autenticação nenhuma**, acionam
agentes que carregam a API key do CRM e o token da Meta. Oito findings CRITICAL
vivem aqui (F-019, F-021 a F-026).

- `Agente_Gerenciador_de_Leads_BnA.json` expõe a superfície de **escrita** de 14
  ferramentas do CRM a quem chamar o webhook.
- `Gerente_Autonomo_de_Tarefas_IA.json` entrega **o método HTTP e a URL inteira**
  à decisão do LLM (`$fromAI`), com a credencial anexada pelo nó — e o prompt do
  agente é montado a partir de `titulo`/`descricao` de tarefa, que o outro agente
  escreve. É uma cadeia de injeção de prompt terminando em requisição
  autenticada arbitrária.
- `WF-01_Agente_Bia.json` não tem **nenhuma** defesa de prompt injection, e o
  parâmetro `whatsapp` da ferramenta é string livre escolhida pelo LLM, sem
  allowlist.

Ações:
1. Autenticar os três webhooks (header secreto no mínimo; idealmente HMAC).
2. **Arquivar `Gerente_Autonomo_de_Tarefas_IA`** — ele existe **somente** no
   export de produção `live_exports/20260708_1443/`, não está versionado como
   workflow mantido, e é o de maior poder. Se ainda estiver ativo, desligar.
3. Substituir método+URL vindos do LLM por ferramentas de escopo fixo.

> Os arquivos em `n8n/workflows/live_exports/` **não foram editados de
> propósito**: são um snapshot do que rodava em 2026-07-08. Alterá-los não muda
> nada na instância viva e adulteraria um registro histórico.

### 4.3 — Banco: privilégio e allowlist

- `docker-compose.yml:53` — a aplicação conecta como `POSTGRES_USER`, que a
  imagem do postgres cria como **SUPERUSER**. Trocar por um papel restrito exige
  **criar o papel e migrar privilégios no banco**: operação de produção. Mudar só
  o compose derrubaria a aplicação no próximo restart, com um usuário inexistente.
- `docker/postgres/init.sql:21` concede `SELECT ON ALL TABLES` a `crm_readonly`,
  incluindo `users.hashed_password` e as API keys. A denylist que esta auditoria
  colocou em `run_select_query` é camada de **aplicação**; o que fecha de fato é
  revogar o `SELECT` no banco.
- `docker/postgres/init-hardening.sh` só roda em volume novo — em produção ele
  **nunca rodou**. Precisa ser aplicado à mão.

> **A `m011` foi EXECUTADA, não só lida.** `tests/test_migration_m011.py` monta o
> schema real dos dois serviços num SQLite descartável, **remove os índices
> únicos** para reproduzir o estado de produção (onde o schema nasceu de um
> `create_all()` anterior a esta auditoria) e roda o script nos dois cenários:
> banco limpo → cria os quatro índices, sai 0, e a segunda execução não muda
> nada; banco com duas conversas do mesmo número → **exit 2, índice não criado,
> nenhuma linha apagada nem deduplicada**, e a saída diz o que reconciliar.
> Isso continua **não** sendo permissão para rodá-la em produção: rode primeiro
> contra uma cópia do dado real, porque só lá aparecem as duplicatas que
> existirem.
- `DATABASE_READONLY_URL` precisa apontar para um DSN `crm_readonly` real. Sem
  isso, a ferramenta SQL da IA fica **desligada** em produção — fail-closed
  deliberado desta auditoria (`app/services/ai_tools.py`, F5).

### 4.4 — Backup

`scripts/backup_postgres.sh` estava corrompendo **todo** dump: `docker exec -t`
aloca um pseudo-TTY, cujo ONLCR traduz LF→CRLF **antes** do gzip, e o formato
plain emite os dados como blocos `COPY ... FROM stdin`, onde a linha termina o
registro — cada valor da última coluna ganhava um `\r`. O restore funciona e
suja os dados em silêncio.

O script foi corrigido e ganhou guarda de regressão. O que falta ao operador:
1. **Considerar todo backup existente suspeito** e conferir um restore num
   ambiente descartável.
2. Não há **upload offsite** — o backup mora no mesmo host do volume do banco.
3. Não há **agendamento versionado** (o cron do cabeçalho é comentário).
4. Um backup nunca restaurado não é um backup: estabelecer teste periódico.

### 4.5 — Consequência operacional das correções de sessão

Tokens emitidos antes desta branch **não têm o claim `typ`** e passam a ser
recusados. **Todo usuário logado fará um re-login.** É deliberado: aceitar `typ`
ausente manteria aberto o bypass em que o token de verificação de e-mail valia
como sessão.

### 4.6 — Decisões de negócio, não de engenharia

Preço, prazo de reembolso e regra de altitude para menores de 7 anos aparecem
inconsistentes entre a base de conhecimento da Bia e a documentação. A auditoria
verificou **consistência entre arquivos**, não correção factual. Se 68.000 CLP é
o preço certo do Valle de la Luna é pergunta para a operação.

---

## 5. O que mudou de fato no código

Agrupado pelo que o usuário final sente.

**Perda de dados parou.**
Um `except Exception` envolvia os três laços do webhook e devolvia **200
incondicional** à Meta: uma falha numa mensagem descartava as irmãs do lote *e*
dizia à Meta que deu certo — e a Meta nunca reenvia. Mensagem de cliente perdida,
sem rastro.

**A Bia voltou a responder o primeiro contato.**
A saudação automática gravava um outbound **antes** de o debounce ser agendado; o
lote pendente é "inbound mais novo que o último outbound", então voltava vazio.
Como o auto-reply é deduplicado por hora, a falha parecia intermitente.

**Envio sem credencial deixou de parecer entrega.**
`{"simulated": True}` virava `status="sent"`. Agora existe `status='simulated'`
e, fora de development, a ausência de credencial **falha alto**.

**Cliente errado deixou de receber dados de outro cliente.**
O lookup de lead casava pelos 10 últimos dígitos do telefone e o primeiro
resultado vencia. Em número brasileiro isso não identifica ninguém:
`5511987654321` e `5521987654321` terminam iguais. Como o `lead_id` fica gravado
**permanentemente** na conversa e as variáveis de template saem dele, um
casamento errado manda nome e e-mail do cliente B para o cliente A dentro de um
template aprovado.

**XSS armazenado fechado, e virou regra.**
`esc()` produz `&#39;`, o que é correto em conteúdo e em atributo — e **inútil
dentro de `on*`**, porque o parser decodifica a entidade antes de o JavaScript
compilar. Nomes de lead vêm do webhook do WhatsApp e do n8n, sem passar por
operador. Oito lugares tinham o padrão. Além deles, a verificação descobriu que
`stage.id` **não é uma chave inteira**: é `StageSchema.id: str` sem validação,
escolhido pelo cliente, interpolado **cru em seis atributos**; e `allDestinos`
agrega o campo `destinos` de todo lead. `tests/test_frontend_injection_contract.py`
transforma isso em regra que vale para arquivos que ainda não existem.

**O link de verificação de e-mail deixou de ser uma sessão do Conversas.**
Achado na **reauditoria**, não na leitura: os dois serviços assinam com a mesma
`SECRET_KEY`, e `app/routers/users.py` emite um token de verificação de e-mail
que viaja na **query string** de um link — logo, em log de acesso, histórico e
`Referer`. O CRM passou a exigir `typ: "access"` (W1-A); o `decode_token` do
Conversas não olhava propósito nenhum. Enquanto isso valeu, **aquele link era
uma sessão válida do Conversas** — o inbox de WhatsApp inteiro, todas as
conversas de todos os clientes. Cada wave olhou só o seu serviço; o buraco vivia
exatamente no meio.

**Sessão parou de ser forjável.**
O Conversas tinha `SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-me")`
**versionado**. Como os dois serviços validam com a mesma chave e compartilham
`users`, qualquer pessoa com acesso ao repo assinava `{"sub": "<email de admin>"}`
e era admin **nos dois**.

**A linha que envenenava a ORM do CRM.**
`conversas/app/seed.py` gravava `role="admin"` minúsculo; o CRM persiste o **nome
do membro** do enum ("ADMIN"). Reproduzido nos dois sentidos: com `'admin'`,
`query(User)` levanta `LookupError` em **toda** consulta que retorne aquela linha.

**Filtro que nunca encontrava.**
`_ESPACOS` afirmava, em comentário, ser "o mesmo conjunto que `str.strip()`
remove". Não era: faltavam NBSP, `\x1c-\x1f`, `\x85` e o bloco U+2000. Uma chave
colada do Excel/Word/WhatsApp com NBSP na borda ficava **permanentemente**
impossível de filtrar, sem erro nenhum.

**Um NUL derrubava a listagem inteira.**
`campos_personalizados` é `Column(JSON)` (o tipo `json` do Postgres aceita NUL),
mas `query_filters` faz `cast(coluna, JSONB)` linha a linha — e `jsonb` **rejeita**
NUL. Uma única linha envenenada dava 500 na listagem de leads e em todo segmento
com campo personalizado, para todos os usuários.

---

## 6. Qualidade da suíte — o que foi corrigido nela

Cinco testes **afirmavam o defeito** ou não afirmavam nada. Nenhum foi removido;
todos foram reancorados no comportamento, e cada mudança está justificada no
commit.

| Teste | O que havia | O que passou a afirmar |
|---|---|---|
| `test_conversas_outbound_integrity.py` | exigia `status == "sent"` para envio simulado | exige `'simulated'` e que nunca seja lido como entregue |
| `test_conversas_service_window.py` | fixture com `timestamp: "1"` (epoch 1970) | epoch real **+ 3 checks novos** para a âncora da Meta |
| `test_pipeline_inline_lead_edit.py` | asserção terminada em `or True` (sempre verdadeira) | escrita de HTML no card só via `renderLeadCard` — verificada quebrando o template |
| `test_pipeline_inline_lead_edit.py` | exigia diff **zero** contra `origin/main` em 4 arquivos | o **contrato** que o editor consome (rotas + campos do schema) |
| `test_conversas_mobile_pwa.py` / `test_conversas_notifications.py` | fatiavam `loadChat` em `[:1200]` / `[:600]` | leem o corpo da função contando chaves |
| `test_conversas_media_storage.py` | criava N conversas com o mesmo número | reutiliza a conversa, como a aplicação faz |

Mais três defeitos estruturais da suíte:

1. **`test_conversas_security.py` rodava no job errado do CI.** Era o único teste
   do Conversas sem o literal `CONVERSAS_DIR`, que é o discriminador de job.
2. **O guard de "seleção vazia" do CI era código morto**: `run:` roda com
   `bash -e` e `grep -L` sai 1 sem casamento, então o step morria na atribuição,
   antes do guard.
3. **16 chamadas `subprocess.run(text=True)` sem `encoding`.** Sem ele a
   decodificação usa o codec da plataforma — cp1252 no Windows. Verde no CI
   (Linux), vermelho na máquina de quem escreve o código.

E a lacuna mais grave: **a única autenticação do webhook Meta nunca era
exercitada.** 29 arquivos de teste mencionam `META_APP_SECRET`; todos o definem
vazio, justamente para desligar a verificação. `tests/test_conversas_webhook_signature.py`
cobre isso com o segredo ligado, incluindo o check que prova que a assinatura
cobre os **bytes** e não o JSON.

---

## 6a. Cobertura da REAUDITORIA

A auditoria perguntava "todo arquivo do escopo foi lido?" — 345/345, registrado
em `AUDIT_COVERAGE.csv`. A reauditoria pergunta outra coisa: **todo arquivo que
MUDOU foi conferido, e por quem?** A resposta está em `REAUDIT_COVERAGE.csv`,
linha a linha.

Dos 124 arquivos alterados, 33 são artefatos desta própria auditoria
(`docs/audit/*`, escritos por mim). Sobram **91 arquivos de código**:

| Evidência | Arquivos | % |
|---|---:|---:|
| Diff lido por mim, linha a linha | 72 | 79% |
| Coberto por teste desta auditoria | 61 | 67% |
| **Ambos** | 42 | 46% |
| **Ao menos uma das duas** | **91** | **100%** |

As duas colunas são deliberadamente separadas porque valem coisas diferentes: um
teste prova comportamento e não vê intenção; uma leitura vê intenção e não prova
nada em runtime. A coluna de leitura é uma **lista curada, escrita à mão** —
inferi-la de heurística seria exatamente a cobertura fictícia que esta missão
proíbe.

A primeira geração dessa planilha acusou 17 arquivos de código sem evidência
nenhuma. Eles foram lidos na segunda passada, e é por isso que a linha final é
100% — não porque a régua foi afrouxada.

**Achados dessa segunda passada** (a leitura pagou o custo dela):

- O buraco do token de verificação de e-mail (§5) foi encontrado lendo o diff de
  `app/auth.py` e perguntando o que acontece do outro lado da chave compartilhada.
- O guard anti-SSRF foi **sondado**, não aceito: 13 tentativas de bypass —
  `@`-prefixo (a original), `//`, `%2e%2e`, `\`, fragmento com `#@host`, esquema
  absoluto — todas recusadas; caminhos legítimos passam.
- A afirmação de W1-E de que `--forwarded-allow-ips=*` é seguro foi **conferida**
  contra o `docker-compose.yml`: nenhum serviço publica `ports:`, todos usam
  `expose:`, então de fato não há caminho até eles que não passe pelo Traefik.
- Os `async def` trocados por `def` em analytics/tags foram verificados quanto a
  chamadas internas com `await` — não há nenhuma; são apenas route handlers.

---

## 6b. Impacto estrutural

O baseline do Graphify (3.182 nós / 7.148 arestas) **não pôde ser refeito**: o
ambiente Python do graphify não está disponível nesta sessão, e reinstalá-lo
trocaria uma medida por outra sem ganho. No lugar, o risco que aquele grafo
existia para vigiar foi medido diretamente por AST, comparando o merge-base com
`HEAD` (`docs/audit/impacto_estrutural.txt`, reproduzível):

| | Antes | Depois |
|---|---:|---:|
| Módulos internos | 129 | 130 (`+1` — a migration `m011`) |
| Arestas de import interno | 343 | 349 (**+6**) |
| Arestas removidas | — | **0** |
| Violações de fronteira entre os dois serviços | 0 | **0** |

As seis arestas novas são todas explicáveis e desejáveis:

- `app.routers.ai → app.limiter` — havia **dois** limitadores e o do router não
  compartilhava contagem com o resto do app.
- `app.routers.auth → app.config` — parou de ler env cru (era o que fazia
  `ENVIRONMENT="Production"` emitir cookie sem `Secure`).
- `app.routers.tasks → app.models.lead` — `lead_id` inexistente batia na FK e
  virava 500 com transação abortada.
- `app.schemas.user → app.models.user` — o enum passou a vir do dono.
- `conversas.app.routers.pages → conversas.app.{auth,database}` — o portão de
  página passou a **validar** o token, não só constatar que o cookie existe.

Nenhum módulo de alto blast radius cresceu mais que `+1` de in-degree
(`app.database` 41, `+0`; `app.auth` 22, `+0`). Não houve deriva arquitetural, e
**os dois serviços continuam sem se importar** — a fronteira que a auditoria
mapeou segue de pé.

---

## 6c. Uma correção que eu decidi NÃO fazer

`app/routers/segments.py` (F-054, HIGH, CONFIRMED): `get_segment_leads` e
`preview_segment` fazem `.all()` no conjunto filtrado INTEIRO e só então fatiam
em Python (`unique_leads[skip:skip+limit]`). Com 19 mil leads, devolver 100
carrega 19 mil objetos ORM. E não há critério de desempate, então a paginação é
instável quando `created_at` empata. É o mesmo anti-padrão que `leads.py:398`
documenta como já corrigido em outro lugar.

**Não corrigi, de propósito.** O `.all()` existe porque o `joinedload(Lead.tags)`
duplica linhas de `Lead`, e é por isso que a deduplicação acontece em Python. A
correção certa é paginar por ID numa query leve e só então carregar os objetos —
e é aí que mora a armadilha:

```sql
SELECT DISTINCT leads.id ... ORDER BY leads.created_at DESC
```

O SQLite aceita. O **PostgreSQL recusa**: *"for SELECT DISTINCT, ORDER BY
expressions must appear in select list"*. Toda a suíte deste repositório roda em
SQLite (§7). Eu escreveria a correção, ela passaria verde aqui, e quebraria a
listagem de segmentos em produção — na rota que o n8n usa antes de disparar
mensagem.

Trocar um problema de desempenho por uma falha de dialeto **não verificável neste
ambiente** não é estabilizar. Fica registrado com a armadilha nomeada, para quem
fizer a correção com um PostgreSQL na frente: selecione `Lead.id` **e**
`Lead.created_at` no `with_entities`, para que as duas colunas do `ORDER BY`
estejam na lista do `SELECT DISTINCT`; e confira antes se o caminho de
`tag_mode="all"` já traz `GROUP BY`/`HAVING`, porque isso muda a forma da query.

---

## 7. Limitações — o que esta entrega NÃO prova

- **Nenhum acesso ao banco de produção.** Todo enunciado sobre o schema real vem
  de model e migration. Não sei qual serviço criou `users` em produção, se
  `crm_readonly` existe, nem se a chave vazada ainda está ativa.
- **Nenhum teste em PostgreSQL.** A suíte inteira roda em SQLite. Divergências de
  dialeto — `lower()` ASCII-only, `FOR UPDATE` no-op, `TIMESTAMPTZ` aware vs
  naive, violação de UNIQUE — são **estruturalmente invisíveis** para ela.
- **Cross-browser não foi executado.** Não existe Playwright nem qualquer runner
  de navegador no repositório, e instalá-lo seria funcionalidade nova fora do
  escopo. As correções de front foram verificadas por leitura e, onde possível,
  **executando o JavaScript sob `node` com stub de DOM** — o que cobre a lógica
  de escape, não o comportamento de renderização de cada engine.
- **Nenhuma corrida executada.** Os findings de concorrência continuam sendo
  raciocínio estático sobre sequências check-then-act sem lock.
- **Build Docker não executado** (sem daemon nesta máquina). As mudanças de
  `Dockerfile` foram revisadas, não construídas.
- **`ADDRESSED_UNVERIFIED` não é "corrigido".** São 151 findings cujo código
  mudou sem teste desta auditoria apontando para o arquivo.
- **A instância n8n viva não foi vista.** Os workflows foram auditados pelos JSON
  versionados e pelo export de 2026-07-08.
- **O grafo do Graphify não foi refeito** (§6b). A comparação estrutural existe,
  mas é uma medida minha por AST, não a mesma métrica do baseline.

---

## 8. Observação sobre o método (para a próxima vez)

A fase de implementação rodou com múltiplos agentes editando **a mesma árvore de
trabalho** com propriedade exclusiva de arquivo. A propriedade de *arquivo*
funcionou; o **índice do git**, não. Durante a execução um agente rodou
`git stash` sobre a árvore inteira, revertendo 49 arquivos de outros agentes, e
um `git reset` concorrente esvaziou o índice entre um `git add` e o `git commit`
seguinte — dois arquivos meus ficaram de fora do commit em que deveriam estar.

Nada foi perdido (o stash é durável, e a reconciliação foi feita comparando cada
arquivo contra o stash e contra a base), mas a lição é concreta: **propriedade
exclusiva de arquivo não basta quando o índice do git é global.** Da próxima vez,
uma worktree por agente, ou nenhum comando de git nas mãos deles.

---

## 9. Veredito

**NOT READY FOR RELEASE VALIDATION**

Justificativa técnica, sem rodeios:

1. **Uma credencial de produção continua viva e exposta** (§4.1). A chave
   autentica em todas as rotas `/api/*` dos dois serviços, não expira, e está no
   histórico do git. Remover a linha do arquivo — que foi o que esta missão pôde
   fazer — **não revoga nada**. Enquanto não houver rotação, qualquer validação
   de release estaria validando um sistema com acesso administrativo distribuído
   publicamente.
2. **Três webhooks n8n públicos e sem autenticação continuam operando** (§4.2),
   um deles entregando método e URL à escolha de um LLM alimentado por texto de
   cliente. Isso não é alcançável pelo repositório.
3. **Todo backup existente deve ser considerado corrompido** (§4.4) e nenhum
   restore foi verificado. Liberar sem um backup confiável é apostar que nada
   dará errado.
4. **A suíte inteira roda em SQLite** (§7) e o alvo é PostgreSQL. Os UNIQUE
   novos, as diferenças de `lower()` e o comportamento de `TIMESTAMPTZ` não têm
   verificação empírica no dialeto real.

O que **não** motiva o "NOT READY": o código. Os 63 arquivos de teste passam, os
29 findings CRITICAL estão resolvidos com teste ou bloqueados por serem externos
ao repositório, e cada correção carrega a explicação do defeito que a motivou.

**O caminho para READY** é curto e não depende de mais programação:
rotacionar a chave e purgar o histórico (§4.1) · autenticar ou desligar os
webhooks n8n (§4.2) · executar `migrations/m011` num ambiente de teste com cópia
do dado real e conferir se há duplicatas a reconciliar · rodar a suíte contra
PostgreSQL · fazer um backup com o script corrigido e **restaurá-lo** num
ambiente descartável.

Feitos esses cinco, o veredito muda — e a mudança deve ser registrada aqui, com
a evidência de cada um.

---

## 10. Confirmações finais

- **NÃO houve merge automático.** A branch `audit/full-system-stabilization-2026-08-24`
  está local, sem PR aberto e sem merge em branch protegida.
- **NÃO houve deploy.** Nenhum `deploy.yml` foi disparado, nenhum acesso à VPS,
  nenhum container reiniciado, nenhum `git pull` em produção.
- **NÃO houve alteração de dados de produção**, nem execução de migration em
  produção, nem exclusão de dado real.
- **NÃO houve alteração de infraestrutura externa** (Traefik, n8n, Meta, VPS).
- **NENHUM segredo foi exposto ou documentado.** A chave encontrada foi
  substituída por placeholder; seu valor não aparece em nenhum artefato desta
  auditoria.
- **NENHUM teste foi removido** para ficar verde, **nenhum lint foi silenciado**,
  **nenhuma segurança foi reduzida** para fazer teste passar.

---

# RODADA 2026-08-26 — estabilização funcional

19 commits locais. **Nenhum push, merge, PR ou deploy. Nenhum dado de produção
tocado. Nenhuma migration executada fora do PostgreSQL descartável de auditoria.**

`96 files changed, 10267 insertions(+), 502 deletions(-)` sobre `3206eeb`.

## Inventário

`docs/audit/MASTER_FUNCTIONAL_BUG_MATRIX.md` — **110 sintomas** catalogados, um
por relato, com causa raiz, teste e commit por linha:

| Status | Nº |
|---|--:|
| `RESOLVED` | 55 |
| `FIXED_PENDING_MANUAL_N8N` | 18 |
| `DUPLICATE_ROOT_CAUSE` | 16 |
| `NOT_REPRODUCED_WITH_EVIDENCE` | 15 |
| `BLOCKED_OPERATOR` | 5 |
| `OPEN` | 1 |

Os 16 `DUPLICATE_ROOT_CAUSE` medem o quanto os relatos colapsaram: sete
sintomas da Wave 1 — fila vazia, Bia misturando cliente pronto, "meus
atendimentos" vazio, lead que continua aguardando, conversa atribuída que some —
eram **um** defeito visto de ângulos diferentes. Não implementei sete correções.

Os 55 `RESOLVED` foram fechados em **19 commits**; vários commits fecham mais de
um sintoma pela mesma causa.

## As três causas que explicavam mais

**1. O handoff não tinha chamador.**
`POST /api/conversations/{id}/handoff` existe, está correto e nenhum dos 18 nós
do workflow do Gerenciador alcança a porta 8001 — todos apontam para
`http://crm:8000/...`. Verificado por dump de todos os `url`/`method` dos exports
de 26/08 e por grep de `8001` em `n8n/`. `is_bot_active` nunca virava `False`,
`queued_at` nunca era preenchido, e a Bia dizia ao cliente que ele estava numa
fila que nunca o recebeu.

**2. "Atribuído" era tratado como "atendido".**
O inbox classificava por `atendente_id`, e `_apply_human_state` apagava
`queued_at` no instante em que um atendente era definido. Dar dono a uma conversa
a removia da FILA DE ESPERA antes de qualquer humano falar com o cliente.

**3. `POST /api/leads` criava só a linha `leads`.**
Sem `FunnelEntry`, sem `LeadHistory`, sem tag. Todo lead que a Bia cria passa por
esse endpoint, e o pipeline só renderiza quem tem entry — metade do funil de
entrada sumia em silêncio (F-341).

## O que só apareceu porque foi executado, não lido

- **F-043** — `json` aceita a sequência de escape de NUL, `jsonb` não. Uma linha
  legada derrubava o filtro de campo personalizado para **todos** os leads, com
  `UntranslatableCharacter`. Reproduzido no PostgreSQL 16 e corrigido movendo o
  cast para dentro do guard.
- **O botão de reenviar estava morto.** `retrySending` nunca foi declarada, e o
  arquivo é `'use strict'`: todo clique levantava `ReferenceError` antes do
  fetch.
- **M6** — a aplicação da D3 introduziu `==` no `jsonBody` do formulário. Mesmo
  mecanismo do M1. O `PUT` falha, `neverError: true` esconde, e o formulário do
  site **não atualiza nenhum lead que já existe**. Em produção agora.
- **A base de conhecimento da Bia não é o repositório.** O subworkflow lê a Data
  Table n8n `bia_knowledge_base`. Os 73 markdown não são lidos em runtime.

## Validações contra PostgreSQL 16 real

Container `bna-postgres-audit`, descartável. Nenhum outro container tocado.

| O quê | Resultado |
|---|---|
| `m012` (coluna + índice + backfill) | 6/6 combinações de estado, idempotente na 2ª execução |
| `FunnelEntry` sob concorrência (2 threads, 2 conexões) | 1 linha, nenhuma exceção, ambas convergem |
| Lock de anotação (com hold forçado de 0,5 s) | 5/5 rodadas, espera medida, as duas notas sobrevivem |
| F-043 (linha envenenada + 4 sadias) | consulta responde, devolve só as boas |
| `/api/conversations/inativas` | 13/13, mesma pertinência e ordenação do SQLite |

## Suíte

70 → **80 arquivos** de teste. `python tests/test_<nome>.py`, um processo por
arquivo, como o CI faz. Resultado da execução completa desta rodada registrado
abaixo.

**Dois testes tiveram asserções invertidas, de propósito, porque a REGRA mudou**
(`test_conversas_agent_timeout`: falha da Bia agora move para a fila;
`test_conversas_operational_state`: atribuir preserva a fila). Nenhum teste foi
enfraquecido para fazer uma correção passar. Onde um teste bloqueou uma mudança
sem que a regra tivesse mudado — o filtro de viajantes — a mudança **não** foi
forçada: virou parâmetro novo convivendo com o antigo.

## O que continua dependendo de você

| Item | O quê |
|---|---|
| **M6** | apagar um `=` no `jsonBody` do formulário — o formulário está quebrado agora |
| **M7** | definir `CONVERSAS_API_KEY` no ambiente do CRM para a ponte de handoff funcionar (sem ela é no-op, nada regride) |
| **M8** | criar o workflow de follow-up por inatividade (o endpoint já existe) |
| **M9** | apontar o formulário do rodapé para o mesmo webhook |
| **M10** | consolidar leads duplicados por sufixo de WhatsApp (consequência conhecida do 409) |
| **Data Table** | inserir as linhas de `N8N_KB_DATATABLE_ROWS.md` — sem isso a Bia não muda |
| **D2** | autenticar `/webhook/agent-bia` |
| **Decisões de negócio** | preços 2026 `[PENDENTE_VALIDACAO]`, altitude para menores de 7, resposta sobre visto, sazonalidade do roteiro combinado |

## Veredito

**Não é "release ready".** Nada foi validado em produção, e o item mais urgente
desta rodada (M6) é uma mudança que só você pode fazer. O que existe é: as causas
raiz nomeadas com prova, corrigidas com teste, e um inventário onde nenhum dos
110 sintomas ficou sem classificação.
