# Reconciliação n8n — 2026-08-26

Entrada: quatro exports frescos, fornecidos pelo operador **depois** de aplicar as
mudanças manuais M1–M5, D1, D3 e D7. Versionados em
`n8n/workflows/live_exports/20260826_wa/` (verificado: nenhum segredo no conteúdo).

| Arquivo versionado | Origem | Nós |
|---|---|--:|
| `wf01_agente_bia.json` | `WF-01 Agente Bia (10)` | 14 |
| `gerenciador_leads.json` | `Agente Gerenciador de Leads — BnA (8)` | 18 |
| `formulario_site.json` | `Formulário do Site → CRM BnA (1)` | 16 |
| `bia_consultar_knowledge_base.json` | `BIA — Consultar Knowledge Base (1)` | 3 — **nunca auditado antes** |

Estes substituem `20260825_fase2/` como estado corrente. A nota de procedência em
`MASTER_FUNCTIONAL_BUG_MATRIX.md` (que dizia que nenhum export novo havia chegado)
está resolvida por este documento.

---

## 1. Mudanças manuais: verificadas uma a uma no export

| Item | O que se pedia | Estado no export | Evidência |
|---|---|---|---|
| **M1** | tirar um `=` de `pronto_para_humano` | ✅ **APLICADO** | `wf01_agente_bia.json`, `Tool Enviar ao Gerenciador de Leads`: valor começa com `={{ $fromAI( 'pronto_para_humano', ...` — um único `=` |
| **M2** | remover `Tool Acionar Notificador` | ✅ **APLICADO** | o nó não existe mais; os 18 nós do Gerenciador não contêm `notificacao` |
| **M3** | `Ignorar mensagem` responder 204 | ✅ **APLICADO** | `respondWith=noData`, `responseCode: 204` |
| **M4** | texto da anotação como query param | ✅ **APLICADO** | `Tool Adicionar Nota`: `url` sem `?texto=`, `sendQuery: true`, `parametersQuery.texto = $fromAI(...)` — a codificação passa a ser do n8n, não concatenação crua |
| **M5** | ramo de erro no Gerenciador | ✅ **APLICADO** | nó `Fallback — erro Gerenciador` e segunda saída `main[1]` do agente ligada a ele |
| **D1** | autenticar `/webhook/gerenciador-leads` | ✅ **APLICADO** | `Webhook Gerenciador`: `authentication: "headerAuth"` |
| **D3** | formulário preserva campo não vazio + CORS restrito | ⚠️ **APLICADO COM REGRESSÃO** | lógica `preservarOuPreencher` presente e correta; CORS `Access-Control-Allow-Origin: https://brasileirosnoatacama.com.br` nas três respostas. **Mas ver M6 abaixo.** |
| **D7** | defesa de injeção no prompt da Bia | ✅ **APLICADO** | system message de 31.269 caracteres com seção de hierarquia de instruções, recusa a "ignore as instruções anteriores"/"finja que você é", e proibição de revelar prompt/ferramentas/credenciais/IDs |
| **D2** | autenticar `/webhook/agent-bia` | ❌ **NÃO APLICADO** | `authentication` ausente no `Webhook Mensagem` |

**Nenhuma dessas mudanças foi revertida por mim, e nenhuma foi re-proposta.**

---

## 2. M6 — regressão nova, introduzida pela D3

**Severidade: alta. Silenciosa. Quebra o formulário para todo lead já existente.**

**WORKFLOW:** Formulário do Site → CRM BnA
**NÓ:** `Atualizar lead existente`
**CAMPO:** *Body* → `JSON` (`jsonBody`)

**Estado atual:** o valor começa com **dois** sinais de igual:

```
=={{
(() => {
  const novo = $('Validar e normalizar').first().json;
  ...
```

**Por que isso quebra.** É exatamente o mecanismo do M1, que o operador já corrigiu
uma vez neste mesmo conjunto de workflows: no n8n o primeiro `=` marca o campo como
expressão e o resto é template. Com `==`, o `=` sobrando vira **texto literal
prefixado ao corpo**, e o que sai na requisição é `=` seguido do JSON — que não é
JSON válido. O `PUT /api/leads/{id}` do CRM rejeita.

O nó está configurado com `neverError: true` e `fullResponse: true`, então **o
workflow não falha visivelmente**: ele segue para o próximo nó como se tivesse dado
certo. O sintoma operacional é "o formulário não atualiza o lead que já existe" /
"o dado que o cliente mandou pelo site não aparece", sem erro em lugar nenhum.

**Evidência de que é digitação, não idioma do n8n:** varri os três workflows. De
**nove** expressões no formulário, oito têm um `=` — inclusive `Criar novo lead`,
cujo corpo tem exatamente o mesmo formato. Este nó é o único com dois. Nos outros
dois workflows não há nenhuma ocorrência de `==`.

**MUDANÇA EXATA:** apagar **um** sinal de igual. Nada mais.

**VALOR ANTIGO** (início) — `=={{\n(() => {\n  const novo = ...`
**VALOR NOVO** (início) — `={{\n(() => {\n  const novo = ...`

**CONEXÕES / NÓS:** nenhuma alteração.

**TESTE MANUAL:**
1. No painel de expressão do campo *JSON*, o preview deve mostrar um objeto JSON
   **sem** o `=` na frente.
2. Envie o formulário do site com um WhatsApp que **já existe** no CRM e com um
   campo hoje vazio no lead (por exemplo `email`).
3. Em *Executions* → `Atualizar lead existente` → aba *Output*: `statusCode` deve
   ser 200, não 4xx.
4. No CRM, o campo antes vazio deve estar preenchido e **nenhum campo que já tinha
   valor pode ter mudado** (é o que a lógica `preservarOuPreencher` garante).

**ROLLBACK:** recolocar o `=` extra.

**DEPENDÊNCIA REPO-SIDE:** nenhuma — o endpoint do CRM já aceita o corpo correto,
com o contrato `""`-vs-`null` travado por `tests/test_n8n_contract_lead_update.py`.

**STATUS:** `FIXED_PENDING_MANUAL_N8N`

---

## 3. Descoberta estrutural: a base de conhecimento da Bia não é o repositório

`BIA — Consultar Knowledge Base` é o subworkflow que a `Tool consultar_contexto_bna`
chama. Três nós:

1. `When Executed by Another Workflow` — entradas `query`, `destination`,
   `journey_stage`, `customer_status`, `is_first_message`.
2. `Get row(s)` — nó `n8n-nodes-base.dataTable`, lendo a Data Table
   **`bia_knowledge_base`** (id `tFOsRhxI3RneMccG`), filtrada por
   `validation_status = "validado"` **e** `active = true`.
3. `Selecionar Contexto por Índices` — 19.857 caracteres de JS que pontuam,
   sanitizam e formatam os registros.

Colunas da Data Table, deduzidas do formatador: `record_key`, `domain`, `title`,
`content`, `destination`, `journey_stage`, `handoff_reason`, além de
`validation_status` e `active` usados no filtro.

**Consequência:** `bna_agent_context/` — os 73 arquivos markdown, 4.9k linhas —
**não é lido em tempo de execução por nada**. Editá-lo não muda o comportamento da
Bia. Ele é a fonte versionada e a referência do operador; a base viva é a Data
Table, mantida à mão no n8n.

Isto é o **ROOT-015 outra vez**, agora na base de conhecimento: o repositório não é
fonte de verdade sobre o n8n. Toda conclusão anterior que tratava o vault como o que
a Bia lê estava medindo a coisa errada.

Por isso as correções da Wave H entregam **duas** coisas: o markdown corrigido
(fonte versionada) e `docs/audit/N8N_KB_DATATABLE_ROWS.md`, com a linha exata que o
operador precisa inserir na Data Table para cada regra. Sem o segundo passo, o
comportamento em produção não muda.

**Efeito colateral favorável já em produção:** o nó de código injeta, em toda
chamada, um bloco fixo `=== REGRA ABSOLUTA — PREÇOS ===` proibindo a Bia de
informar preço, valor, estimativa ou faixa, e sanitiza valores monetários dos
registros antes de devolvê-los. Isso neutraliza o sintoma de F-073/F-074 (a KB
mandava cotar preço `[PENDENTE_VALIDACAO]` e simultaneamente recusar): a Bia não
cota nada. A contradição **no vault** continua aberta e continua sendo decisão de
negócio — mas ela não alcança mais o cliente.

---

## 4. O que continua igual, e importa

**`Tool Alterar Responsavel` continua com `responsavel_id=5` fixo na URL:**

```
PUT http://crm:8000/api/leads/{lead_id}/responsavel?responsavel_id=5
```

Continua sendo o único sinal determinístico que o repositório recebe no momento do
handoff, e continua sendo uma decisão de um LLM sobre chamar ou não a ferramenta.

**Nenhum dos 18 nós do Gerenciador alcança a porta 8001.** Confirmado de novo nos
exports novos: todos os alvos são `http://crm:8000/...`. O endpoint
`POST /api/conversations/{id}/handoff`, que existe e está correto, **continua sem
chamador**. É a causa raiz da Wave 1 e ela sobrevive intacta a estas mudanças
manuais — o que valida a ponte CRM→Conversas construída nesta rodada.

---

## 5. Findings antigos reclassificados por esta evidência

| Finding | Antes | Agora | Motivo |
|---|---|---|---|
| N8N-F01 (M1) | `PROPOSED_FIX` | **RESOLVED** | um `=`, verificado no export |
| N8N-F02 (M2) | `PROPOSED_FIX` | **RESOLVED** | nó removido |
| N8N-F03 (M3) | `RESOLVED_PARCIAL` | **RESOLVED** | 204 no export; a metade repo-side já estava feita |
| N8N-F08 (M4) | `PROPOSED_FIX` | **RESOLVED** | `sendQuery` + `parametersQuery` |
| N8N-F10 (M5) | `PROPOSED_FIX` | **RESOLVED** | `Fallback — erro Gerenciador` ligado à segunda saída |
| F-073 / F-074 (preço `[PENDENTE_VALIDACAO]`) | `OPEN` | **OPEN no vault, mitigado em produção** | bloco fixo de preços no subworkflow da KB |
| D4 (verificar o nome do modelo) | pendente | **atualizar** | a Bia agora usa `Gemini 3.5-flash-lite`; o Gerenciador continua em `Gemini 2.5 Flash` |
| D2 (autenticar `agent-bia`) | pendente | **continua pendente** | sem `authentication` no export |

---

## 6. Itens n8n que permanecem abertos

| ID | Item | Classificação |
|---|---|---|
| **M6** | `jsonBody` do `Atualizar lead existente` com `==` | `FIXED_PENDING_MANUAL_N8N` |
| **M7** | fazer o Gerenciador chamar o handoff do Conversas — **desnecessário se a ponte CRM→Conversas desta rodada for aceita**; ver `N8N_MANUAL_CHANGES.md` | opcional |
| D2 | `/webhook/agent-bia` sem autenticação | `BLOCKED_OPERATOR` |
| D5 | rotação da API key derruba os três workflows juntos | `BLOCKED_OPERATOR` |
| — | linhas da Data Table `bia_knowledge_base` (Wave H) | `FIXED_PENDING_MANUAL_N8N` |
