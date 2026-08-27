# N8N_CURRENT_STATE_RECONCILIATION.md

Reconciliação entre os workflows n8n **realmente em produção** (fornecidos como
evidência externa em 2026-08-25) e o estado do repositório na branch
`audit/full-system-stabilization-2026-08-24`.

> Este documento tem precedência sobre qualquer afirmação anterior sobre n8n em
> `FULL_SYSTEM_AUDIT.md`, `FINDINGS.csv` ou `RELEASE_READINESS.md`. Onde
> divergirem, **a evidência nova ganha**.

Evidência bruta e reproduzível em `docs/audit/evidencia_fase2/`
(scripts + saídas) e `n8n/workflows/live_exports/20260825_fase2/` (os três
exports, sem segredo — só referência de credencial por ID).

---

## CURRENT PRODUCTION WORKFLOWS PROVIDED

| Workflow | id | ativo | webhook | nós |
|---|---|:--:|---|--:|
| **WF-01 Agente Bia** | `sd9gjIKZpGi75qmq` | sim | `POST /webhook/agent-bia` | 14 |
| **Agente Gerenciador de Leads — BnA** | `6o8aUBnewvDU7eTT` | sim | `POST /webhook/gerenciador-leads` | 18 |
| **Formulário do Site → CRM BnA** | `b8R0yXpUcIlNhXIH` | sim | `POST /webhook/formulario-site` | 16 |

## NOT IN PRODUCTION

- **Notificador** (`/webhook/notificacao`) — removido. Não deve ser recriado,
  substituído nem tratado como ausência defeituosa.
- **Gerente Autônomo de Tarefas IA** — não está entre os três. Era a origem do
  finding "o LLM escolhe método e URL"; ver §5.
- **Analista de Métricas**, **Envio de Tarefas por Responsável**, **Notificação
  WhatsApp** — presentes em snapshots antigos, **ausentes** do conjunto atual.

**Regra de arquitetura desta fase:** *colocar na fila humana* ≠ *acionar
Notificador*. Os dois conceitos são separados e permanecem separados. O próprio
system message da Bia já codifica isso: ele proíbe, em quinze formulações
diferentes, dizer que "um atendente foi notificado", e manda explicar que o
atendimento entrou numa **fila por ordem de chegada**.

---

## 1. Fluxos ponta a ponta, como são hoje

### A. WhatsApp → Conversas → Bia → Gerenciador → CRM

```
cliente (WhatsApp)
  → Meta Cloud API  ──HMAC X-Hub-Signature-256──→  conversas/app/routers/webhook.py
  → _forward_to_agent  ──POST {conversation_id, lead_id, whatsapp, nome,
                               mensagem, historico}──→  /webhook/agent-bia
      → Code "Code in JavaScript"   (portão: mensagem só de emoji?)
        ├─ só emoji → "Ignorar mensagem"  →  HTTP 404 sem corpo
        └─ tem conteúdo → Agente Bia (Gemini)
             tools: Consultar Lead (GET /api/leads/by-whatsapp/{w})
                    consultar_contexto_bna (subworkflow ZaCLNwNbQ84y4eAW)
                    Enviar ao Gerenciador (POST /webhook/gerenciador-leads)
             → "Validar saída da Bia" (bloqueia vazamento de termo interno)
               ├─ segura  → "Responder ao Conversas"  { resposta }
               └─ insegura/vazia → "Fallback — erro Bia"
  → _split_agent_reply divide por "|||" e envia cada parte ao cliente
```

O **contrato de payload confere exatamente**: `conversas/app/routers/webhook.py:849-856`
monta `{conversation_id, lead_id, whatsapp, nome, mensagem, historico}` e o
system message da Bia lê precisamente essas chaves, incluindo
`historico.map(m => m.direction === 'inbound' ? ...)`.

### B. Bia → Gerenciador → CRM

O Gerenciador é um agente com **13 tools HTTP** sobre o CRM (buscar/criar/
atualizar lead, listar/buscar/definir tags, listar funis, adicionar ao funil,
mover etapa, transferir funil, adicionar nota, criar tarefa, alterar
responsável) **mais** `Tool Acionar Notificador`, que aponta para um workflow
que não existe mais.

### C. Site → Formulário → CRM

```
formulário do site  ──POST FormData──→  /webhook/formulario-site   (CORS: *)
  → "Validar e normalizar"  (honeypot, e-mail, telefone, datas, destinos)
    ├─ inválido → 400 { sucesso:false, erros[] }
    └─ válido → GET /api/leads/by-whatsapp/{w}
         ├─ existe → PUT  /api/leads/{id}
         └─ não    → POST /api/leads
       → POST /api/pipeline/funnels/3/leads  { etapa_id: "nova_oportunidade" }
         (409 = já estava no funil → tratado como sucesso, etapa preservada)
       → PUT  /api/leads/{id}/anotacoes?texto=...
       → 200 { sucesso:true, lead_id, acao, funil, etapa_inicial }
```

---

## 2. Delta: snapshot antigo × export atual

Medido em `docs/audit/evidencia_fase2/delta_workflows.txt`.

| Workflow | nós antes | nós agora | removidos | adicionados |
|---|--:|--:|--:|--:|
| WF-01 Agente Bia | 7 | 14 | **0** | **7** |
| Agente Gerenciador | 18 | 18 | **0** | **0** |
| Formulário do Site | — | 16 | — | novo, **nunca auditado** |

**A Bia melhorou muito e a auditoria anterior não sabia.** Os 7 nós novos são
todos defesa: portão de emoji, validação da saída contra vazamento de termo
interno (`CRM`, `workflow`, `prompt`, `tool`, `system message`…), fallback de
erro, e a base de conhecimento como subworkflow. O nó do agente ganhou
`onError: continueErrorOutput`. O system message cresceu de 20.470 para 28.697
caracteres, com uma seção inteira nova — *"FILA DE ATENDIMENTO HUMANO — REGRA
ABSOLUTA"* — que proíbe explicitamente afirmar que alguém foi notificado.

**O Gerenciador não mudou em nada estrutural.** Mesmos 18 nós, mesmas conexões.
Só o system message cresceu (1.524 → 2.231), acrescentando as regras de tags e a
obrigação de usar o funil "Vendas: Principal" / etapa "Sem Contato". **`Tool
Acionar Notificador` continua lá, intacto.**

---

## 3. Contratos CRM ↔ n8n

Todas as 15 chamadas HTTP dos três workflows foram casadas contra as **208 rotas
reais** extraídas por AST do código (`evidencia_fase2/contratos_n8n.txt`).

**Rotas: todas existem.** Nenhuma chamada aponta para endpoint inexistente.
Autenticação: header `X-API-Key`, credencial `CRM Brasileiros API` —
bate com `app/auth.py:156` (`Header(None, alias="X-API-Key")`). Todos os
endpoints consumidos são guardados por `get_current_user`, que aceita a API key.

**Payloads: rodados de verdade contra os schemas** (`evidencia_fase2/payloads_reais.txt`).

| Chamada | Resultado |
|---|---|
| `POST /api/leads` — lead completo | ACEITO |
| `POST /api/leads` — campos vazios (como o toolDescription manda) | ACEITO (`""` → `None`) |
| `POST /api/leads` — `destinos: "Atacama, Uyuni"` (string) | ACEITO → `["Atacama","Uyuni"]` |
| `POST /api/leads` — formulário do site (números como número) | ACEITO |
| `POST /api/tasks` — `lead_id` como **string** | ACEITO (coerção) |
| `POST /api/pipeline/funnels/3/leads` — `etapa_id: "nova_oportunidade"` | ACEITO |
| **`PUT /api/leads/{id}` — `nome: ""`** | **RECUSADO — 422** |

A última linha é o achado **N8N-F04**, detalhado abaixo.

Nenhuma das correções da Fase 1 quebrou um workflow atual. O único risco
introduzido pela Fase 1 é o **N8N-F12** (padrão de `etapa_id`), corrigido nesta
fase antes de causar dano.

---

## 4. Findings desta fase

| ID | Sev | Confiança | Estado |
|---|---|---|---|
| N8N-F01 | CRITICAL | CONFIRMED | PROPOSED_FIX / BLOCKED_OPERATOR |
| N8N-F02 | CRITICAL | CONFIRMED | PROPOSED_FIX / BLOCKED_OPERATOR |
| N8N-F03 | HIGH | CONFIRMED | corrigido no repositório + mudança manual |
| N8N-F04 | HIGH | CONFIRMED | corrigido no repositório |
| N8N-F05 | HIGH | CONFIRMED | BLOCKED_OPERATOR |
| N8N-F06 | HIGH | CONFIRMED | BLOCKED_OPERATOR |
| N8N-F08 | MEDIUM | CONFIRMED | PROPOSED_FIX / BLOCKED_OPERATOR |
| N8N-F09 | MEDIUM | EXTERNAL_STATE_UNVERIFIED | BLOCKED_OPERATOR |
| N8N-F10 | MEDIUM | CONFIRMED | PROPOSED_FIX / BLOCKED_OPERATOR |
| N8N-F11 | LOW | EXTERNAL_STATE_UNVERIFIED | BLOCKED_OPERATOR |
| N8N-F12 | MEDIUM | CONFIRMED | corrigido no repositório |

### N8N-F01 — o sinal de entrada na fila humana sai como `"=true"`

`WF-01 Agente Bia` → `Tool Enviar ao Gerenciador de Leads` → campo
`pronto_para_humano`:

```
"value": "=={{ $fromAI('pronto_para_humano', ..., 'boolean', false) ? 'true' : 'false' }}"
             ^^ dois sinais de igual
```

No n8n, um valor de parâmetro que começa com `=` é uma **expressão**: o primeiro
`=` é o marcador, e o resto é um template onde `{{ }}` é interpolado e o texto
fora dele é **literal**. A documentação oficial mostra os dois casos lado a lado
— `"value": "={{$json[\"orderID\"]}}"` (interpolado) e `"name": "=orderId"`
(marcador consumido, resto literal).

Logo, com `==`, o template é `={{ … }}` e o resultado é a string **`"=true"`**
ou **`"=false"`**.

Todos os outros campos do mesmo nó usam **um** `=`. A anomalia é local a este
campo.

**Consequência.** O consumidor é o system message do Gerenciador, que compara
literalmente:

```
Se pronto_para_humano = "true"  → tags "Atendimento Humano" e "Lead quente"
Se pronto_para_humano = "false" → tag "IA Atendimento"
```

e o `toolDescription` de `Tool Alterar Responsavel` diz *"Use APENAS quando o
payload contiver pronto_para_humano=true"*. Com `"=true"`, **nenhum dos dois
ramos casa**. O que acontece a seguir é decisão de um LLM sobre uma string que
não corresponde a nenhuma regra escrita.

Sejamos precisos: isto **não é uma falha determinística**. É uma **ambiguidade
determinística na transição de estado mais importante do sistema** — se o lead
entra ou não na fila humana. Um modelo pode ler `"=true"` como verdadeiro; pode
não ler. O sintoma esperado é exatamente "às vezes o lead qualificado não chega
na fila", que é o tipo de falha que ninguém consegue reproduzir.

**Correção:** apagar um `=`. Nada mais. Instruções em `N8N_MANUAL_CHANGES.md`.

### N8N-F02 — dependência morta viva: `Tool Acionar Notificador`

Nó `Tool Acionar Notificador`, tipo `@n8n/n8n-nodes-langchain.toolHttpRequest`,
`POST http://n8n:5678/webhook/notificacao`, **sem autenticação e sem
credencial**, ligado como `ai_tool` ao `Agente Gerenciador de Leads`.

Respondendo exatamente ao que foi perguntado:

- **Ainda é alcançável?** Sim. Está conectado como tool; o modelo pode escolhê-la
  a qualquer turno.
- **Em quais condições?** Quando o modelo julga que `pronto_para_humano` é
  verdadeiro — que, por N8N-F01, é justamente a decisão ambígua.
- **O que acontece quando é chamada?** O caminho `/webhook/notificacao` não está
  mais registrado no n8n → **404**. O `toolHttpRequest` devolve erro ao modelo.
- **A falha interrompe outras ações?** Pode. O nó `Agente Gerenciador de Leads`
  tem `retryOnFail: true` e **não tem** `onError` — ao contrário do agente da
  Bia, que tem `continueErrorOutput` e um ramo de fallback. O Gerenciador tem
  **uma única saída main**, para `Responder ao Webhook`. Se o agente falhar, esse
  nó nunca roda e a chamada da Bia fica sem resposta.
- **O agente espera o retorno?** Sim — `toolHttpRequest` é síncrono dentro do
  loop do agente.
- **Pode produzir comportamento parcial?** Sim, e este é o ponto. A ordem das
  tools é escolhida pelo modelo. Se ele chamar o notificador **antes** de
  `Definir Tags` / `Alterar Responsavel`, um erro que derrube ou trunque o loop
  deixa o lead **criado mas sem tag e sem responsável** — visível no CRM,
  invisível na fila.
- **Explica findings anteriores?** Sim: é candidato direto à classe "lead
  qualificado não aparece para a equipe", junto com N8N-F01.

**Correção:** remover o nó e sua conexão `ai_tool`. **Não** substituir por outra
notificação — a fila humana já é o mecanismo, e o próprio prompt da Bia diz ao
cliente que o atendimento é por ordem de chegada.

### N8N-F03 — reação de emoji recebe pedido de desculpas por instabilidade

O portão novo da Bia responde **404 sem corpo** para mensagem composta só de
emoji (nó `Ignorar mensagem`). Do outro lado,
`conversas/app/routers/webhook.py::_fetch_agent_parts`:

```python
if resp.status_code != 200:
    logger.error(f"Agente IA retornou status {resp.status_code} ...")
    return []
```

e lista vazia significa `degraded = True`, que envia ao cliente:

> "Tive uma instabilidade para processar sua mensagem agora. Pode me enviar
> novamente em alguns instantes? 🙂"

**Quem manda um 👍 sozinho recebe um pedido de desculpas por instabilidade** —
exatamente o oposto do que o portão foi feito para produzir — e cada reação
grava uma linha de **ERRO** no log de um evento perfeitamente normal.

A causa raiz está escrita na própria docstring da função: *"quem chama nao
precisa distinguir os modos de falha"*. Silêncio e falha estão conflados num
único `[]`. Corrigido nesta fase, **nos dois lados**: o Conversas passa a
distinguir os dois casos, e o n8n passa a responder `204` em vez de `404`.

### N8N-F04 — toda atualização de lead sem nome novo devolve 422

`Tool Atualizar Lead` tem `jsonBody` **fixo**: manda as doze chaves em toda
chamada. O `toolDescription` instrui *"Campos sem informacao devem ficar
vazios"*. E:

```
LeadUpdate(nome="", ...)  →  422  "String should have at least 1 character"
```

`LeadCreate` já tratava `""` como ausente; `LeadUpdate` não. **Defeito
pré-existente** — `min_length=1` é idêntico em `origin/main`, não veio da Fase 1.

Efeito: a Bia coleta e-mail, datas, número de viajantes; o Gerenciador tenta
gravar; o CRM recusa; ninguém percebe, porque o `toolHttpRequest` devolve o erro
ao modelo, que segue conversando. Corrigido no repositório — a correção pertence
ao CRM, porque a ferramenta n8n **não consegue** omitir chaves condicionalmente.

### N8N-F05 — `/webhook/gerenciador-leads` é um agente com escrita no CRM, aberto na internet

`docker-compose.yml:117-121` publica o n8n via Traefik em
`n8n.crmbrasileirosnoatacama.cloud`. O webhook não tem autenticação. O corpo
recebido é interpolado **verbatim** no prompt do agente:

```
"Processe o seguinte payload recebido da Bia:\n\n{{ JSON.stringify($json.body, null, 2) }}"
```

Qualquer pessoa na internet posta JSON arbitrário que vira instrução para um
agente que carrega a API key do CRM e 13 ferramentas, incluindo criação de lead,
tarefa, alteração de responsável e substituição total de tags.

Este é o **trust boundary errado**: `/webhook/gerenciador-leads` é
service-to-service (Bia → Gerenciador), não público. Ação de operador.

### N8N-F06 — o formulário público pode sobrescrever o cadastro de um cliente real

`/webhook/formulario-site` é **legitimamente público** — é um formulário de site.
Tem honeypot, validação estrita de e-mail/telefone/datas/destinos, e só escreve
num funil fixo. O trust boundary aqui é **diferente** dos outros dois e não deve
receber a mesma solução.

O problema específico dele: busca lead por WhatsApp e, se achar, faz
`PUT /api/leads/{id}` com nome, e-mail, destinos e datas vindos do formulário —
**sem nenhuma verificação de que quem preencheu é o dono do número**. Um
anônimo que saiba (ou acerte) o WhatsApp de um cliente sobrescreve o cadastro
dele. Some-se: sem rate limit e com `Access-Control-Allow-Origin: *` (a própria
sticky note do workflow reconhece que o `*` era para testes).

### N8N-F08 — `{texto}` da anotação vai cru para a query string

`Tool Adicionar Nota`: `PUT http://crm:8000/api/leads/{lead_id}/anotacoes?texto={texto}`.
O texto é escolhido pelo modelo e substituído por concatenação simples, sem
codificação de URL. Um `&` ou `#` no resumo trunca a anotação ou injeta
parâmetro. O workflow do Formulário faz o certo no mesmo endpoint: usa
`sendQuery` com parâmetro nomeado, que o n8n codifica.

### N8N-F09 — nome de modelo suspeito nos dois agentes

Os dois agentes usam `modelName: "models/gemini-3.5-flash-lite"`, num nó
rotulado "Gemini 2.5 Flash". Não consigo verificar daqui se esse identificador
existe na API do Google. Se não existir, os dois agentes falham em toda execução
— e o fallback da Bia mascara isso como "instabilidade". Verificação é de
operador; o comando exato está em `N8N_MANUAL_CHANGES.md`.

### N8N-F10 — o Gerenciador não tem ramo de erro

O agente da Bia tem `onError: continueErrorOutput` e um `Fallback — erro Bia`.
O `Agente Gerenciador de Leads` tem `retryOnFail: true` e **nenhum** `onError`,
com saída única para `Responder ao Webhook`. Falha do agente = webhook sem
resposta = a tool da Bia recebe erro. Assimetria de robustez entre dois nós que
fazem a mesma coisa.

### N8N-F11 — subworkflow da base de conhecimento não foi fornecido

`consultar_contexto_bna` chama o subworkflow `ZaCLNwNbQ84y4eAW`
("BIA — Consultar Knowledge Base"), que não está entre os três exports. O system
message manda tratar o retorno dele como **fonte de verdade**. É uma dependência
não auditada de uma decisão de atendimento.

### N8N-F12 — regressão introduzida pela Fase 1, corrigida aqui

A Fase 1 pôs `pattern=r"^[A-Za-z0-9_-]+$"` em `StageSchema.id` como defesa em
profundidade. `FunnelUpdate` revalida a lista `etapas` **inteira**, então
qualquer funil de produção cuja etapa tenha id com espaço ou acento passaria a
dar **422 em qualquer edição** — e o próprio system message do Gerenciador chama
a etapa de **"Sem Contato"**. O padrão foi trocado por um que rejeita o que é
perigoso (`" ' < > & \` e controle) em vez de permitir só slug. A defesa que
realmente vale — escape no template — continua travada por
`tests/test_frontend_injection_contract.py`.

---

## 5. Findings anteriores INVALIDADOS pela evidência nova

| Antes | Agora | Evidência |
|---|---|---|
| **"Um webhook entrega método E URL à escolha de um LLM"** (F-022, F-023, CRITICAL) | **OBSOLETE** | Isso existia em `Gerente_Autonomo_de_Tarefas_IA.json`, que **não está em produção**. Nos três workflows atuais, toda URL de tool é string fixa; o modelo só preenche `{placeholder}` de caminho e corpo. |
| F-021 — prompt do Gerente Autônomo montado a partir de tarefa do CRM | **OBSOLETE** | Mesmo workflow ausente. |
| F-019 / F-020 — `Agente_Gerenciador_de_Leads_BnA` expõe 14 tools de escrita | **UPDATE** | O workflow existe e continua expondo escrita, mas são **13** tools de CRM + 1 morta (o notificador). O número muda e a superfície precisa ser redescrita. |
| Findings sobre **Notificação WhatsApp**, **Analista de Métricas**, **Envio de Tarefas por Responsável** | **OBSOLETE** | Nenhum está no conjunto de produção atual. |
| "três webhooks públicos, mesma solução para os três" | **UPDATE** | São três, mas com **trust boundaries diferentes**: um é legitimamente público (formulário) e dois são service-to-service indevidamente expostos. Ver N8N-F05 e N8N-F06. |

**Resíduo real do finding invalidado.** O modelo ainda controla um segmento de
caminho em `PUT /api/leads/{lead_id}` (placeholder no fim da URL, sem sufixo),
o que permite redirecionar o PUT para outras rotas do CRM por normalização de
`..`. O alcance é limitado pelo método e pela tabela de rotas: as rotas PUT
existentes são de board, card, checklist, entry/move, funnel, tag, task, team,
user e segment. `PUT /api/users/{id}` exige `require_admin` — se a conta dona da
API key do n8n **não** for admin, está fechado; se for, está aberto. Qual conta
detém a chave não é verificável daqui. Severidade **MEDIUM**, não CRITICAL.

---

## 6. Findings anteriores CONFIRMADOS

- **Webhooks n8n públicos e sem autenticação** — confirmado, e agora com o
  caminho de exposição provado: `docker-compose.yml` publica o n8n no Traefik em
  `n8n.crmbrasileirosnoatacama.cloud`.
- **A API key do CRM vive nas credenciais do n8n** — confirmado: os três
  workflows referenciam `CRM Brasileiros API` (`QulESeRfj4JdhZUI`). A rotação da
  chave (blocker desde a Fase 1) **exige atualizar essa credencial no n8n**, ou
  os três workflows param juntos. Isso não estava dito antes e muda o
  procedimento de rotação.
- **`PUT /api/tags/lead/{id}` é substituição total** — confirmado, e o
  `toolDescription` do Gerenciador já alerta para isso e manda buscar as tags
  atuais antes. A guarda que a Fase 1 pôs no editor de lead do front protege o
  outro consumidor do mesmo endpoint.

---

## 7. Riscos que continuam NÃO verificáveis daqui

1. Se a credencial `CRM Brasileiros API` pertence a uma conta **admin** (define
   se N8N-F07 residual é explorável).
2. Se `models/gemini-3.5-flash-lite` existe (N8N-F09).
3. O conteúdo do subworkflow `ZaCLNwNbQ84y4eAW` (N8N-F11).
4. Se algum funil de produção tem `etapa_id` com espaço/acento (motiva N8N-F12,
   já corrigido de forma que o risco deixa de importar).
5. Se o Traefik expõe `/webhook/*` sem filtro adicional na borda.
6. Se o workflow Notificador está apenas desativado ou realmente apagado — muda
   se o `POST /webhook/notificacao` devolve 404 ou fica pendurado.
