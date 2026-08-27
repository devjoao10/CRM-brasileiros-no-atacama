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
| **D3** | formulário preserva campo não vazio + CORS restrito | ✅ **APLICADO** | lógica `preservarOuPreencher` presente e correta; CORS `Access-Control-Allow-Origin: https://brasileirosnoatacama.com.br` nas três respostas. O M6 que eu levantei contra ela não procedia (§ 2). |
| **D7** | defesa de injeção no prompt da Bia | ✅ **APLICADO** | system message de 31.269 caracteres com seção de hierarquia de instruções, recusa a "ignore as instruções anteriores"/"finja que você é", e proibição de revelar prompt/ferramentas/credenciais/IDs |
| **D2** | autenticar `/webhook/agent-bia` | ❌ **NÃO APLICADO** | `authentication` ausente no `Webhook Mensagem` |

**Nenhuma dessas mudanças foi revertida por mim, e nenhuma foi re-proposta.**

---

## 2. M6 — **eu errei: não havia regressão**

> Esta seção foi reescrita. O texto original afirmava que a aplicação da D3
> tinha quebrado o formulário em produção. **Não tinha.** Deixo o erro
> registrado em vez de apagá-lo.

Eu li `"jsonBody": "=={{ ... }}"` no export e, por analogia direta com o M1,
concluí que havia um `=` sobrando, que o corpo deixaria de ser JSON válido e que
o formulário não estaria atualizando nenhum lead existente — com
`neverError: true` escondendo a falha.

O operador conferiu **no editor visual do n8n**: o campo mostra `{{`. O `=` extra
do export é a marcação com que o n8n serializa um campo em modo expressão.

**Por que a analogia falhou.** No M1 os dois sinais estavam dentro do *valor* de
um parâmetro (`parametersBody → value`), onde o segundo `=` realmente vira texto.
No M6 estão no marcador do *campo* inteiro. Posições diferentes na estrutura do
nó; eu tratei as duas como equivalentes porque no JSON pareciam iguais.

**O sinal que eu deveria ter visto:** o nó irmão `Criar novo lead` tem corpo do
mesmo formato. Se minha leitura estivesse certa, a **criação** de lead pelo
formulário também estaria quebrada — e não está. Eu usei essa mesma comparação
como *evidência a favor* da minha tese ("das nove expressões, só esta tem dois
`=`"), sem notar que ela a contradizia: a diferença estava na posição, não na
contagem.

**Status: `RESOLVED` — nada a corrigir.** O que continua verdadeiro é apenas que
`neverError: true` faria uma falha real do `PUT` passar despercebida ali. É
característica do desenho do workflow, não defeito.

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
| ~~M6~~ | **não procedia** — erro meu de leitura, ver § 2 | `RESOLVED` |
| **M7** | fazer o Gerenciador chamar o handoff do Conversas — **desnecessário se a ponte CRM→Conversas desta rodada for aceita**; ver `N8N_MANUAL_CHANGES.md` | opcional |
| D2 | `/webhook/agent-bia` sem autenticação | `BLOCKED_OPERATOR` |
| D5 | rotação da API key derruba os três workflows juntos | `BLOCKED_OPERATOR` |
| — | linhas da Data Table `bia_knowledge_base` (Wave H) | `FIXED_PENDING_MANUAL_N8N` |
