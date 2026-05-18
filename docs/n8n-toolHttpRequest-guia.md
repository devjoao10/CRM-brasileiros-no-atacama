# Guia N8N: Configuração de Agentes AI com toolHttpRequest

> **Contexto:** Documentação gerada após 2+ horas de troubleshooting para configurar corretamente o node `toolHttpRequest` (legado, v1.1) no N8N 2.16.1 self-hosted com a API FastAPI/Pydantic do CRM Brasileiros no Atacama.
>
> **Leia isso antes de criar qualquer workflow de agente AI com HTTP tools.**

---

## 1. Identificando a versão do node HTTP Tool

Existem **duas versões** do HTTP Request Tool no N8N:

| Versão | Tipo interno | Como identificar |
|---|---|---|
| **Legada (v1.1)** | `@n8n/n8n-nodes-langchain.toolHttpRequest` | **NÃO** tem botão "Add option" na canvas |
| **Nova** | `n8n-nodes-base.httpRequest` como tool | **TEM** botão "Add option" na canvas |

**Este sistema usa a versão legada (v1.1).** Toda a configuração neste guia é para essa versão.

---

## 2. Regra fundamental: usar `{placeholder}`, NÃO `$fromAI()`

### ❌ NÃO usar: `$fromAI()`

```
={{ $fromAI('nome', 'Nome do lead', 'string') }}
```

**Por quê não funciona na v1.1 legada:**
- `$fromAI()` é para a versão **nova** do node
- Na versão legada, `$fromAI()` com tipo `'number'` causa crash de inicialização: `Cannot read properties of undefined (reading 'includes')` no N8N 2.16.1
- Em campos "Using JSON Below" (modo Fixed), `{{ $fromAI() }}` NÃO é avaliado — é enviado como texto literal para a API
- Em campos "Using Fields Below", causa o erro de inicialização acima

### ✅ Usar: `{placeholder}` com Placeholder Definitions

O próprio N8N exibe essa dica na UI:
> *"Tip: You can use a {placeholder} for any part of the request to be filled by the model. Provide more context about them in the placeholders section"*

---

## 3. Como configurar cada tool

### 3.1 Configuração geral (toda tool)

- **Authentication:** Generic Credential Type
- **Generic Auth Type:** Header Auth
- **Header Auth:** `CRM Brasileiros API` (credential com `X-API-Key`)
- **Todos os campos URL/JSON:** modo **Fixed** (não Expression)

### 3.2 Para tools GET sem body

```
Method: GET
URL: http://crm:8000/api/endpoint/{placeholder}
Send Body: OFF
```

Placeholder Definitions:
- `placeholder` → descrição do que o AI deve fornecer

### 3.3 Para tools POST/PUT com body JSON

```
Method: POST ou PUT
URL: http://crm:8000/api/endpoint/{url_placeholder}
Send Body: ON
Specify Body: Using JSON Below
```

JSON body:
```json
{
  "campo_string": "{placeholder_string}",
  "campo_array": {placeholder_array}
}
```

> **Regra de aspas no JSON:**
> - Campos string → com aspas: `"{placeholder}"`
> - Campos array/objeto → sem aspas: `{placeholder}` (o AI fornece o JSON bruto)

Placeholder Definitions: um por campo que o AI deve preencher.

### 3.4 Para tools com Query Parameters (ex: Adicionar Nota)

```
Send Query Parameters: ON
Specify Query Parameters: Using Fields Below
```

Adicionar parâmetro:
- **Name:** `descricao`
- **Value Provided:** Using Field Below
- **Value:** `{descricao}`

---

## 4. Regra crítica: IDs devem ser strings

O LangChain valida os parâmetros das tools com schema estrito. Todos os placeholders são do tipo `string` por padrão. Se o AI tentar passar um número como `lead_id = 51` (inteiro), o LangChain rejeita:

```
Error: Received tool input did not match expected schema
✖ Expected string, received number → at lead_id
```

**Solução:** Adicionar no System Prompt do agente:

```
REGRA DE TIPOS: Ao chamar qualquer ferramenta, SEMPRE passe todos os 
parâmetros como texto (string). Nunca passe números sem aspas.
- CORRETO: lead_id = "51", funnel_id = "1"
- ERRADO: lead_id = 51, funnel_id = 1

Para campos de lista (destinos, tag_ids), passe como string simples:
- CORRETO: destinos = "Atacama"
- ERRADO: destinos = ["Atacama"]
```

> **Nota:** A API FastAPI/Pydantic do CRM aceita strings e converte automaticamente para int/list onde necessário (validadores `mode="before"` e coerção de tipo do Pydantic v2).

---

## 5. Bugs conhecidos no N8N 2.16.1

| Bug | Causa | Solução |
|---|---|---|
| `Cannot read properties of undefined (reading 'includes')` | `$fromAI()` com tipo `'number'` em "Using Fields Below" | Usar `{placeholder}` em vez de `$fromAI()` |
| Tool falha em inicialização antes de executar | Qualquer `$fromAI('x', ..., 'number')` conectado ao agente | Trocar para `'string'` ou migrar para `{placeholder}` |
| `Could not replace placeholders in body` | AI passou array `["Atacama"]` em `{destinos}` | Instruir no prompt a passar strings simples |
| `[object Object]` no body | `$fromAI('body', ..., 'json')` como único valor do body | Não usar $fromAI para o body inteiro |
| `Unexpected end of JSON input` | Body em Expression mode com JSON gerado pelo AI truncado | Usar modo Fixed com placeholders individuais |

---

## 6. Mapeamento completo das tools deste CRM

### Base URL interna: `http://crm:8000`

| Tool | Method | URL | Body | Placeholders |
|---|---|---|---|---|
| Buscar Lead WhatsApp | GET | `/api/leads/by-whatsapp/{whatsapp}` | — | `whatsapp` |
| Criar Lead | POST | `/api/leads` | JSON | `nome`, `whatsapp`, `destinos` |
| Atualizar Lead | PUT | `/api/leads/{lead_id}` | JSON | `lead_id`, `nome`, `whatsapp`, `destinos`, `data_chegada`, `data_partida` |
| Listar Tags | GET | `/api/tags` | — | — |
| Buscar Tags Lead | GET | `/api/tags/lead/{lead_id}` | — | `lead_id` |
| Definir Tags Lead | PUT | `/api/tags/lead/{lead_id}` | JSON | `lead_id`, `tag_ids` |
| Listar Funis | GET | `/api/pipeline/funnels` | — | — |
| Adicionar ao Funil | POST | `/api/pipeline/funnels/{funnel_id}/leads` | JSON | `funnel_id`, `lead_id`, `etapa_id` |
| Mover Etapa | PUT | `/api/pipeline/entries/{entry_id}/move` | JSON | `entry_id`, `etapa_id` |
| Transferir Funil | **POST** | `/api/pipeline/entries/{entry_id}/transfer` | JSON | `entry_id`, `destino_funnel_id`, `destino_etapa_id` |
| Adicionar Nota | POST | `/api/pipeline/history/{lead_id}/note` | Query Param | `lead_id`, `descricao` |
| Criar Tarefa | POST | `/api/tasks` | JSON | `titulo`, `descricao`, `lead_id`, `data_vencimento` |

> ⚠️ **Transferir Funil é POST** (não PUT) — erro fácil de cometer.
> ⚠️ **Adicionar Nota usa Query Parameter** (não body) — `descricao` via "Send Query Parameters".

---

## 7. Endpoint by-whatsapp — comportamento e busca

O endpoint `GET /api/leads/by-whatsapp/{whatsapp}` normaliza o número (remove `+`, espaços, traços) e busca por:

1. **Match exato** do número normalizado
2. **Match exato** com prefixo `+`
3. **Sufixo** dos últimos 11 dígitos (fallback para variações de código do país)

Retorna `404` se não encontrar. O agente deve interpretar 404 como "lead não existe, criar novo".

---

## 8. Credencial de autenticação

```
Tipo: HTTP Header Auth
Nome: CRM Brasileiros API
Header Name: X-API-Key
Header Value: bna_pTe_Jz6MYulKkbftE8fuQX391_D8Oi75_4lj4EQLepu7HMwbq6fR1-Jvdrq4LHnY
```

---

## 9. Checklist para criar nova tool

- [ ] Verificar se o node é a versão legada (sem "Add option")
- [ ] Definir Method correto (GET/POST/PUT) — conferir no router da API
- [ ] URL com `{placeholder}` para parâmetros dinâmicos (modo Fixed)
- [ ] Send Body: ON se POST/PUT com body
- [ ] Specify Body: "Using JSON Below" com `{placeholder}` nos campos string (com aspas) e arrays (sem aspas)
- [ ] Placeholder Definitions: um por placeholder, com descrição clara
- [ ] Authentication: Generic Credential Type → Header Auth → CRM Brasileiros API
- [ ] System Prompt do agente: incluir regra de tipos (strings para IDs)

---

## 10. Como debugar erros

| Erro | O que verificar |
|---|---|
| Crash na inicialização antes de executar | Alguma tool conectada ao agente ainda tem `$fromAI()` com tipo `'number'` |
| `Expected string, received number` | AI passou ID como número. Reforçar instrução no System Prompt |
| `Could not replace placeholders in body` | AI passou array/objeto onde esperava string. Reforçar instrução no System Prompt |
| `422 Unprocessable Entity` | Body inválido chegou à API. Ver Output da tool para detalhes do erro Pydantic |
| `404` em busca por WhatsApp | Comportamento normal para lead novo — o agente deve criar o lead |
| `405 Method Not Allowed` | Method errado na tool (ex: PUT onde deveria ser POST) |
| Response HTML em vez de JSON | URL errada — está apontando para o frontend em vez da API (verificar prefixo `/api/`) |

---

*Documentação gerada em 18/05/2026 após configuração bem-sucedida do Agente Gerenciador de Leads.*
