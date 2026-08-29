# TO-BE — Seções A, B e C

Entrega parcial, para aprovação. Cobre apenas:
**A.** Architecture Principles · **B.** Responsibility Matrix · **C.** State Machine.

Não contém arquitetura detalhada de workflows, contratos JSON, roadmap, migrations nem código.
A V1 permanece em produção, intacta. Nada aqui foi implementado.

Cada afirmação sobre o sistema atual referencia um achado **CONFIRMADO** do `AS-IS.md` ou da
`VALIDACAO-CRUZADA.md`.

---

## A. ARCHITECTURE PRINCIPLES

Nove princípios. Cada um existe porque um problema concreto foi confirmado — nenhum é preferência.

### P1 — Nenhuma saída de LLM atravessa uma fronteira sem passar por um schema que possa recusá-la

**Problema confirmado:** `pronto_para_humano` não aparece em nenhum arquivo `.py` do repositório
(grep). É produzido por um LLM e consumido por outro lendo prosa. Quando o valor virou `"=true"`,
atravessou as duas camadas **sem gerar um único erro**. O Context7 confirmou que o argumento
`description` do `$fromAI` é "hints", não validação.

**Decisão:** toda saída de IA é convertida em estrutura tipada na fronteira. Valor fora do schema
é evento de rejeição registrado, nunca degradação silenciosa.

**Trade-off:** perde-se a tolerância que hoje mascara erros. É o objetivo — um erro visível é
melhor que um handoff que às vezes não acontece.

### P2 — Estado é decidido por código determinístico; a IA fornece fatos, não decisões

**Problema confirmado:** o texto do `$fromAI` instrui o modelo a marcar `TRUE` *"se a resposta que
você pretende enviar ao cliente afirmar que ele foi colocado na fila"*. O estado é ajustado para
combinar com o que o modelo já decidiu dizer — a inversão exata do princípio desejado. E a decisão
de executar o handoff é tomada por um segundo LLM lendo uma descrição de tool em português.

**Decisão:** a IA emite fatos (`destinos`, `adultos`, `periodo`, `email`, `explicit_human_request`).
O Motor de Regras decide o que esses fatos significam.

**Trade-off:** regras de negócio passam a exigir alteração de código versionado em vez de edição de
prompt. Dado que o prompt é editado em produção sem versionamento, isso é ganho, não custo.

### P3 — Uma transição de estado é uma operação de domínio única com invariantes verificadas, ou não aconteceu

**Problema confirmado (Q-1):** o handoff são quatro chamadas HTTP em três processos, com
`onError: continueRegularOutput` no nó do CRM e `{"sucesso": true}` literal no retorno. Três
estados parciais são alcançáveis; um deles reporta sucesso enquanto produz `atendente ≠ responsável`.

**Decisão:** `handoff_to_human()` executa os efeitos e **verifica as invariantes depois**. Se
qualquer uma falhar, não há `HANDOFF_COMPLETED` e o estado não avança.

**Trade-off:** a operação fica mais lenta e mais falível de forma visível. Preferível a rápida e
silenciosamente inconsistente.

### P4 — A IA só comunica o que o sistema já confirmou

**Problema confirmado:** a Bia anuncia a fila **no mesmo turno**, antes de a cadeia completar. O
prompt lista 13 frases proibidas sobre exatamente isso e **nenhuma é verificada por código**. O
filtro de saída existente tem 14 regras contra vazamento de vocabulário interno e **nenhuma
verifica dinheiro**. `price_disclosure_blocked: true` é literal fixo, não verificação.

**Decisão:** a IA Comunicadora recebe uma ação autorizada e fatos confirmados. Um filtro
determinístico na entrega recusa o que ela não pode afirmar — incluindo valores monetários.

**Trade-off:** respostas ocasionalmente mais secas. É o preço de nunca mentir ao cliente.

### P5 — Idempotência por chave, não por disciplina

**Problema confirmado:** `retryOnFail: true, maxTries: 5` está ativo nos nós de agente e relança o
LLM do zero sobre tools de escrita não-idempotentes. Não há idempotency key em nenhuma fronteira.
`leads.whatsapp` não tem `UNIQUE`. O Context7 confirmou nas docs do PostgreSQL que **mesmo em
Serializable** a violação ocorre sob concorrência — constraint é a única proteção real.

**Decisão:** toda operação crítica carrega `event_id`; unicidade é imposta por constraint de banco.

**Trade-off:** exige migrations e tratamento de `IntegrityError`. Sem isso, "não chama duas vezes"
é suposição, não garantia.

### P6 — Uma fonte de verdade por estado, e um único caminho de escrita

**Problema confirmado:** duas definições de fila coexistem e discordam; dois caminhos de reabertura
com efeito oposto; duas implementações de criação de lead; o Conversas escreve tabelas do CRM por
SQL cru sem autenticação; `conversations.lead_id/atendente_id/responsavel_id` não têm FK.

**Decisão:** cada estado tem uma coluna autoritativa e um único caminho de escrita.

**Trade-off:** o SQL cru é mais rápido que HTTP. O padrão correto já existe no repositório
(`conversas_bridge`) e sua própria docstring cita esta causa-raiz.

### P7 — O que não pode ser reconstruído a partir de eventos não aconteceu de forma auditável

**Problema confirmado:** nada abaixo de `WARNING` sai do processo — os `.info` que já registram
"Handoff BIA→humano" nunca são emitidos. Não há correlation id entre Conversas → n8n → CRM.
`triage_started_at`, `triage_completed_at`, `encerrada_at` e reabertura não existem. E
`queued_at` é **apagado** na primeira resposta humana, destruindo o tempo de fila no instante em
que ele se tornaria calculável.

**Decisão:** cada transição emite um evento persistido. "O que aconteceu com o lead X" é uma query.

**Trade-off:** uma tabela e escrita por transição. No volume atual (≤50 conversas/dia) o custo é
irrelevante; sem isso, nada é mensurável — nem se a V2 melhorou algo.

### P8 — Comportamento crítico vive em artefato versionado e testável

**Problema confirmado:** o WF-01 foi editado duas vezes em ~6h durante esta auditoria, com
`versionId` mudando e sem changelog. Dois workflows ativos não existiam no repositório. O
repositório não é fonte de verdade do n8n. Existem 87 testes cobrindo Python e **zero** cobrindo n8n.

**Decisão:** decisões de negócio vivem em Python, sob a suíte existente. O n8n não guarda regra
de negócio.

**Trade-off:** perde-se a edição visual das regras. A auditoria mostrou que essa edição visual é a
origem de uma classe real de defeito.

### P9 — Preservar o que já está correto; a mudança mínima que torna o sistema determinístico

**Confirmado como correto e reutilizável:** `resolver_atendente_elegivel()` (menor carga,
determinístico), `service_window_open()` (função pura, testada), `record_outbound_message` (caminho
único de saída), `_apply_human_state`/`marcar_atendimento_humano` (choke-points **sem import de
FastAPI**), dedup por `UNIQUE(whatsapp_msg_id)`, `criar_lead()` com SAVEPOINT, o padrão
`conversas_bridge`, e `lead_history`.

**Confirmado também:** a Dependency Rule **não está violada** — nenhum model importa service,
nenhum service importa router. O dano é duplicação e fronteira, não estrutura de camadas.

**Decisão:** a V2 acrescenta uma camada de decisão **acima** dessas peças. Não as reescreve. Não
há refactor de camadas.

**Restrição estrutural confirmada:** CRM e Conversas não podem se importar — ambos os pacotes se
chamam `app`. Qualquer regra compartilhada reproduzirá a duplicação atual, a menos que a fronteira
mude antes. Isso é decisão explícita, não detalhe.

---

## B. RESPONSIBILITY MATRIX — TO-BE

### B.1 Onde vive o Motor de Regras

| Opção | Testabilidade | Consistência transacional | Dependência do n8n | Veredito |
|---|---|---|---|---|
| **A. n8n** | ~zero (0 testes hoje) | impossível — só saltos HTTP | total | **Rejeitado.** É onde o problema está: não versionado (2 edições em 6h), bug de expressão é classe real de falha, `retryOnFail` reexecuta side effects |
| **B. Conversas backend** | alta — suíte de 87 testes | **possui a linha e o lock** (`_lock_conversation`, `FOR UPDATE`) | reduzida a borda de IA | **RECOMENDADO** |
| **C. CRM backend** | alta | não possui o estado da conversa | reduzida | **Rejeitado.** Inverteria a propriedade: o CRM teria de alcançar o Conversas, direção que já é corretamente HTTP |
| **D. Módulo de domínio compartilhado** | alta | não remove a fronteira de processo | reduzida | **Bloqueado hoje** — os dois pacotes se chamam `app`. Viável só depois de mudar a fronteira |
| **E. Híbrido (B + endpoint de domínio no CRM)** | alta | melhor possível entre dois processos | reduzida | **Refinamento adotado** |

**Decisão: B com o refinamento de E.** O Motor de Regras vive no Conversas, porque **quem decide
precisa possuir a linha que trava**. O estado da conversa é do Conversas, e `_lock_conversation`
com `SELECT … FOR UPDATE` já está lá. O CRM expõe **uma** operação idempotente para o lado
comercial do handoff, substituindo a chamada de query string sem `require_admin`.

**Trade-off aceito:** o handoff continua atravessando dois processos. Isso é irredutível sem fundir
os serviços. O que muda é que passa a haver **verificação de invariante após os efeitos**, e um
estado explícito para o caso em que ela falha (`HANDOFF_PENDING`).

### B.2 Componentes

| Componente | Responsabilidade TO-BE |
|---|---|
| **Meta/WhatsApp** | Transporte. Nada mais |
| **Conversas** | Fonte de verdade da conversa · pré-filtro · debounce · Motor de Regras · executor · verificação de invariantes · observabilidade |
| **CRM** | Fonte de verdade comercial (lead, funil, responsável) · expõe operação de domínio idempotente |
| **n8n** | Borda de IA (chamar modelo, devolver JSON) · automações periféricas (métricas, inatividade, formulário) · **nenhuma regra de negócio** |
| **IA** | Interpreta linguagem · comunica resultado autorizado · **nunca decide estado** |
| **Banco** | Impõe as invariantes que cabem em uma linha (CHECK, UNIQUE, FK) |
| **Observabilidade** | Tabela de eventos + logging configurado, no Conversas |

### B.3 Função por função

| Função | Responsável ATUAL | Responsável TO-BE | Justificativa |
|---|---|---|---|
| Recebe mensagem | Conversas (webhook HMAC) | **Conversas** — inalterado | Verificação de assinatura correta, falha fechada |
| Persiste mensagem | Conversas | **Conversas** — inalterado | Correto |
| Deduplica | `UNIQUE(whatsapp_msg_id)` + pré-check | **inalterado** | Única proteção com garantia de banco; Context7 confirma ser o mecanismo certo |
| Debounce | dicts em memória do processo | **Conversas, estado em Postgres** | O próprio código documenta que quebra com >1 worker uvicorn |
| Pré-filtro | dentro do n8n ("Precisa responder?") | **Conversas, código determinístico** | Regra absoluta não pode ser julgamento de LLM |
| Interpreta linguagem | Bia (LLM **com tools de escrita**) | **IA Interpretadora — sem tools, saída com schema imposto** | P1, P2. Context7 confirmou que o `response_schema` do Gemini restringe de verdade |
| Valida fatos | **ninguém** | **Conversas (Pydantic estrito, `extra="forbid"`)** | Não existe hoje; `{"whatsap": …}` com typo retorna 201 e perde o dado |
| Decide estado | Bia + Gerenciador (2 LLMs, prosa) | **Motor de Regras (Conversas)** | P2; decisão no processo que trava a linha |
| Escolhe o humano | `resolver_atendente_elegivel` → **sobrescrito** por `user_id: 5` | **`resolver_atendente_elegivel`, sem hardcode** | Já existe, determinístico, testado. Ver B.4 |
| Executa handoff | 4 HTTP, 3 processos, sucesso literal | **1 operação de domínio (Conversas) + 1 endpoint idempotente (CRM)** | Q-1, P3 |
| Atualiza `responsavel_id` | n8n → `PUT` CRM com `onError: continue` | **CRM, chamado pela operação, com verificação do retorno** | Fonte de verdade comercial é do CRM |
| Atualiza `atendente_id` | `/assign` com id hardcoded | **Conversas, com o mesmo valor de `responsavel_id`** | Invariante atendente = responsável |
| Desliga o bot | `_apply_human_state` | **inalterado** | Já é choke-point puro de HTTP |
| Registra fila | `queued_at` via `_apply_human_state` | **inalterado, mas não apagado** | Ver B.5 |
| Gera resposta | Bia — a mesma LLM que decide | **IA Comunicadora — recebe ação autorizada + fatos** | P4 |
| Filtra saída | 14 regex de vocabulário interno | **+ filtro determinístico de moeda** | Confirmado ausente; regra de negócio "Bia não fala preço" |
| Envia ao WhatsApp | `record_outbound_message` | **inalterado** | Caminho único, correto |
| Registra eventos/métricas | quase nada (`.info` não sai do processo) | **Tabela de eventos + logging configurado (Conversas)** | P7 |
| Trata fallback | fallback fixo + entra na fila | **manter, separando falha de geração de falha de ação** | Funciona; falta a separação |
| Conhecimento (FAQ, tours, políticas) | Data Table n8n, keyword scoring | **manter no n8n** | Não é transacional, não é a causa do problema. YAGNI |
| **Preço** | markdown/KB + prompt | **fora do alcance da IA** | Regra de negócio; hoje a última barreira é obediência do modelo |

### B.4 Seleção do humano — menor carga, não rodízio

Comparação pedida:

| Mecanismo | Com 1 atendente | Com N atendentes | Estado necessário |
|---|---|---|---|
| **Menor carga** (existe hoje) | idêntico ao rodízio | distribui por carga real | nenhum — recalculado a cada chamada |
| Rodízio | idêntico | entrega a quem já está afogado | ponteiro persistente |
| Regras de disponibilidade/prioridade | idêntico | mais expressivo | agenda/turno — não existe |

**Decisão: reutilizar `resolver_atendente_elegivel()` como está.** É determinístico, sem estado,
já testado, e superior a rodízio quando houver expansão. O que muda não é o algoritmo — é que hoje
**seu resultado é descartado** pelo `user_id: 5` hardcoded do nó seguinte (CONFIRMADO em Q-1).

Expansão futura: acrescentar disponibilidade/turno **dentro** dessa função, sem hardcode de usuário
em lugar nenhum.

### B.5 A invariante `atendente_id = responsavel_id` — onde impor

Combinação de três camadas, cada uma cobrindo o que a outra não alcança:

1. **Banco (CHECK):** `conversations` já tem as duas colunas. Com uma coluna `state` explícita, a
   invariante vira expressável em uma linha e o estado proibido torna-se **impossível de gravar**.
2. **Operação de domínio:** o que atravessa processos (`leads.responsavel_id` no CRM) não cabe em
   CHECK. A operação verifica o retorno do CRM antes de declarar `HANDOFF_COMPLETED`.
3. **Contrato:** a operação recebe **um** `target_user_id` e o aplica aos dois lados. Não existe
   caminho que aceite dois valores diferentes.

**Ressalva registrada:** `conversations.responsavel_id` é *cache* de `leads.responsavel_id`
(há read-repair a cada listagem). O CHECK garante coerência **dentro da linha**; a coerência entre
serviços é responsabilidade da camada 2.

---

## C. STATE MACHINE — TO-BE

### C.1 Estados

Seis estados, em uma coluna `state` explícita. Hoje o estado é inferido de seis campos e
**nove combinações são alcançáveis**, duas delas contraditórias.

| Estado | Significado |
|---|---|
| `NEW` | Conversa criada; nenhuma interpretação de IA ainda. Transitório |
| `AI_TRIAGE` | Bia atendendo e coletando dados |
| `HANDOFF_PENDING` | Triagem completa; handoff **em execução ou com falha parcial**. Não confirmado |
| `WAITING_HUMAN` | Handoff confirmado. Pertence a um humano; aguarda a primeira resposta dele |
| `HUMAN_ACTIVE` | Humano respondeu ao menos uma vez |
| `CLOSED` | Encerrada |

**`WAITING_INFORMATION` não foi criado.** A distinção "perguntei e estou esperando" não é observável
hoje nem altera comportamento algum — seria estado sem consequência. Passará a se justificar quando
houver follow-up de inatividade (o workflow existe, inativo, e o endpoint `/inativas` é somente
leitura). Registrado como extensão prevista, não omissão.

**`HANDOFF_PENDING` é a resposta direta ao Q-1.** Hoje não existe lugar onde estar quando o CRM
falha e o Conversas não — e é por isso que o sistema reporta sucesso e produz
`atendente ≠ responsável`. Com o estado explícito, a falha parcial tem endereço e é retentável.

### C.2 Invariantes por estado

| Estado | `is_bot_active` | `atendente_id` | `responsavel_id` | `queued_at` | `primeira_resposta_humana_at` | `status` |
|---|---|---|---|---|---|---|
| `NEW` | true | NULL | qualquer | NULL | NULL | aberta |
| `AI_TRIAGE` | true | NULL | qualquer | NULL | NULL | aberta |
| `HANDOFF_PENDING` | **true** | qualquer | qualquer | qualquer | NULL | aberta |
| `WAITING_HUMAN` | **false** | **NOT NULL** | **NOT NULL** | **NOT NULL** | **NULL** | aberta |
| `HUMAN_ACTIVE` | **false** | **NOT NULL** | **NOT NULL** | **NOT NULL** | **NOT NULL** | aberta |
| `CLOSED` | false | preservado | preservado | preservado | preservado | encerrada |

Em `WAITING_HUMAN` e `HUMAN_ACTIVE`: **`atendente_id = responsavel_id`**, obrigatoriamente.

**Por que o bot continua ativo em `HANDOFF_PENDING`:** se o handoff não foi confirmado, o cliente
não pode ficar sem ninguém. O bot segue respondendo, **mas a IA Comunicadora não está autorizada a
dizer que houve encaminhamento** (P4). Isso corrige diretamente o caso em que a Bia anuncia a fila
e o CRM falhou em silêncio.

**Por que `queued_at` deixa de ser apagado:** hoje `marcar_atendimento_humano` faz
`queued_at = NULL` na primeira resposta humana, destruindo o tempo de fila no instante em que ele
se torna calculável. O predicado de fila passa a ser `primeira_resposta_humana_at IS NULL` — que
já é a definição nova adotada pelo `?inbox=fila`. Isso **unifica as duas definições concorrentes** e
preserva a métrica, de uma vez.

### C.3 Transições permitidas

| De → Para | Evento | Condição | Quem executa |
|---|---|---|---|
| — → `NEW` | `MESSAGE_RECEIVED` (primeira) | conversa inexistente | Conversas |
| `NEW` → `AI_TRIAGE` | `AI_INTERPRETATION_STARTED` | pré-filtro liberou | Motor de Regras |
| `AI_TRIAGE` → `AI_TRIAGE` | `TRIAGE_DATA_UPDATED` | fatos novos, triagem incompleta | Motor de Regras |
| `AI_TRIAGE` → `HANDOFF_PENDING` | `HANDOFF_REQUESTED` | **triagem completa** OU **`explicit_human_request`** OU regra de exceção | Motor de Regras |
| `HANDOFF_PENDING` → `WAITING_HUMAN` | `HANDOFF_COMPLETED` | **todas as invariantes verificadas** | Operação de domínio |
| `HANDOFF_PENDING` → `HANDOFF_PENDING` | `HANDOFF_FAILED` | falha parcial; incrementa tentativas | Operação de domínio |
| `WAITING_HUMAN` → `HUMAN_ACTIVE` | `HUMAN_FIRST_RESPONSE` | primeiro outbound humano com sucesso | Conversas |
| `WAITING_HUMAN` → `WAITING_HUMAN` | `HUMAN_REASSIGNED` | troca de dono; **`queued_at` preservado** | Conversas |
| `HUMAN_ACTIVE` → `HUMAN_ACTIVE` | `HUMAN_REASSIGNED` | troca de dono | Conversas |
| qualquer aberto → `CLOSED` | `CONVERSATION_CLOSED` | ação humana explícita | Conversas |
| `CLOSED` → `HUMAN_ACTIVE` | `CONVERSATION_REOPENED` | inbound **dentro da janela** e tinha dono humano | Motor de Regras |
| `CLOSED` → `AI_TRIAGE` | `CONVERSATION_REOPENED` | inbound **fora da janela** OU nunca teve dono humano | Motor de Regras |
| `HUMAN_ACTIVE`/`WAITING_HUMAN` → `AI_TRIAGE` | `AI_RESUMED` | **apenas por ação humana explícita** | Conversas |

### C.4 Transições proibidas

- `AI_TRIAGE` → `WAITING_HUMAN` **direto.** Todo handoff passa por `HANDOFF_PENDING`; não existe
  atalho que pule a verificação de invariante.
- `HANDOFF_PENDING` → `WAITING_HUMAN` **sem confirmação do lado do CRM.** É o defeito do Q-1.
- Qualquer transição para `WAITING_HUMAN`/`HUMAN_ACTIVE` com `atendente_id ≠ responsavel_id`,
  ou com algum dos dois nulo, ou com `is_bot_active = true`.
- Reabertura que **apague** `atendente_id` sem regra explícita. Hoje o inbound do cliente faz reset
  incondicional e a IA reassume; passa a ser decisão do Motor de Regras, com condição declarada.
- Envio de resposta gerada pela IA quando o estado mudou durante a geração (ver C.6).

### C.5 Como estados contraditórios se tornam impossíveis

Três camadas, do mais forte ao mais fraco:

1. **CHECK constraint no banco** — o estado proibido não pode ser gravado por nenhum caminho,
   nem por `UPDATE` manual em psql. Cobre o que cabe em uma linha:
   `state ∈ (WAITING_HUMAN, HUMAN_ACTIVE)` ⟹ `NOT is_bot_active AND atendente_id IS NOT NULL AND
   responsavel_id IS NOT NULL AND atendente_id = responsavel_id AND queued_at IS NOT NULL`.
2. **Função única de transição** — nenhuma rota escreve os campos de estado diretamente. Hoje há
   três sítios que os escrevem à mão fora do choke-point, incluindo `initiate_conversation`, que
   monta os quatro campos no construtor.
3. **Verificação pós-efeito na operação de domínio** — para o que atravessa processos.

A camada 1 é a que falta hoje: **nenhuma coluna "status-like" do sistema tem CHECK constraint**
(CONFIRMADO), e a validade é 100% aplicação.

### C.6 Concorrência IA × humano

**Problema confirmado:** `is_bot_active` é verificado na chegada e no disparo do debounce, mas
**nunca mais antes do envio**, após até 240s de espera. O ramo `degraded` relê o estado
corretamente; só o caminho de sucesso ficou desguarnecido. Um humano que assume durante a geração é
atropelado pela Bia.

**Mecanismo escolhido: token de estado.** A resposta gerada carrega o `state` e a versão da conversa
observados quando a geração começou. Antes de enviar, o sistema relê sob `FOR UPDATE`; se o estado
mudou, a resposta é **descartada** e o evento registrado.

**Por que não as alternativas:** re-leitura simples sem versão não distingue "mudou e voltou";
optimistic locking com coluna `version` resolve, mas exige incrementar a versão em todo escritor —
mais superfície para esquecer. O token compara `state`, que a máquina já mantém, e cobre a
invariante com o mecanismo de lock que já existe.

**Não depende de prompt.**

### C.7 `CLOSED` e reabertura

**Problema confirmado:** hoje existem dois mecanismos com efeito **oposto** sobre o mesmo evento.
Se o cliente escreve primeiro, `atendente_id`, `is_bot_active` e `primeira_resposta_humana_at` são
resetados incondicionalmente — a IA reassume e o dono é apagado. Se o humano escreve primeiro, tudo
é preservado.

**Regra única no TO-BE**, decidida pelo Motor de Regras, independente de quem escreve primeiro:

- Encerrada **com** dono humano, inbound **dentro da janela de retomada** → `HUMAN_ACTIVE`,
  mesmo dono, bot permanece desligado. O "obrigado" volta para quem atendeu.
- Encerrada **sem** dono humano, ou inbound **fora da janela** → `AI_TRIAGE`, nova triagem.

**Decisão pendente sua:** o tamanho da janela de retomada. Proponho alinhar com a janela de
serviço de 24h da Meta — já existe no código como função pura testada (`service_window_open`), é
significativa para o cliente, e evita inventar um segundo conceito de tempo. **Não implemento sem
sua confirmação.**

---

## Questões que permanecem NÃO CONFIRMADAS

| # | Questão | Impacto em A/B/C |
|---|---|---|
| Q-3 | Valor efetivo do acesso a `$env` no n8n de produção — o compose define variável inexistente | Nenhum sobre A/B/C; afeta segurança da V1 |
| Q-6 | Quantos workers uvicorn rodam em produção | **Afeta B** — define se o debounce em Postgres é correção ou pré-requisito |
| — | Se o usuário 5 é a Julia | Nenhum — o TO-BE elimina o hardcode |
| — | Tamanho da janela de retomada | **Afeta C.7** — aguarda sua decisão |

---

## Fora do escopo desta entrega

Arquitetura detalhada de workflows, contratos JSON, motor de regras em detalhe, fronteira da IA
detalhada, estratégia de erro, métricas, migração V1→V2 e roadmap. Serão entregues após aprovação
de A, B e C.

A V1 permanece em produção sem alteração. O `=={{` — já corrigido por terceiros durante a auditoria —
e os demais achados da V1 serão tratados como **HOTFIX**, em trilha separada da construção da V2.
