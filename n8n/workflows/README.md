# 🔧 Setup dos Workflows N8N — CRM Brasileiros no Atacama

## Pré-requisitos

1. N8N rodando na VPS (container `n8n_server` do `docker-compose.yml`)
2. CRM rodando e acessível como `http://crm:8000` dentro da rede Docker
3. Uma **API Key** gerada no CRM (via admin)

---

## Passo 1 — Gerar a API Key no CRM

1. Acesse o CRM e faça login como admin
2. Vá em **Configurações** → **API Keys** (ou use o endpoint):
   ```bash
   curl -X POST http://crm:8000/api/auth/api-key \
     -H "Authorization: Bearer SEU_TOKEN_JWT" \
     -H "Content-Type: application/json"
   ```
3. Copie a API Key gerada

---

## Passo 2 — Configurar variável de ambiente no N8N

No `docker-compose.yml`, adicione a variável `CRM_API_KEY` ao serviço N8N:

```yaml
n8n:
  environment:
    - CRM_API_KEY=SUA_API_KEY_AQUI  # ← adicione esta linha
```

Ou configure via UI do N8N: **Settings → Variables → Add Variable**:
- Nome: `CRM_API_KEY`
- Valor: sua API Key

---

## Passo 3 — Importar os Workflows

No N8N (UI web):

1. Clique em **"+"** → **"Import from File"**
2. Importe os 3 arquivos na ordem:

| Arquivo | Workflow | Quando roda |
|---|---|---|
| `wf-08-notificacoes-tarefas.json` | Notificações de Tarefas | Todo dia 08:00 |
| `wf-09-relatorio-diario.json` | Relatório Diário | Todo dia 19:00 |
| `wf-06-funil-automatico.json` | Funil Automático | A cada 2 horas |

---

## Passo 4 — Configurar envio de notificações

Os workflows WF-08 e WF-09 terminam com um nó placeholder **"📬 Conecte aqui o envio"**.

Conecte a um destes nós:

### Opção A: Email (SMTP)
1. Adicione o nó **"Send Email"**
2. Configure as credenciais SMTP
3. Use `{{ $json.mensagem }}` no corpo do email

### Opção B: WhatsApp (quando tiver)
1. Adicione um nó **"HTTP Request"**
2. Configure para a API do WhatsApp Cloud
3. Use `{{ $json.mensagem }}` no corpo

### Opção C: Telegram
1. Adicione o nó **"Telegram"**
2. Configure o Bot Token e Chat ID
3. Use `{{ $json.mensagem }}` na mensagem

---

## Passo 5 — Ativar os Workflows

1. Abra cada workflow
2. Teste manualmente clicando **"Execute Workflow"**
3. Se tudo funcionar, ative o toggle **"Active"** no canto superior direito

---

## Workflows — Resumo

### WF-08: Notificações de Tarefas
```
⏰ 08:00 → Buscar atrasadas → Buscar do dia → Formatar mensagem → Enviar
```

### WF-09: Relatório Diário
```
⏰ 19:00 → KPIs do dia → Relatório mensal → Board do funil → Montar relatório → Enviar
```

### WF-06: Funil Automático
```
⏰ 2h → Carregar Kanban → Analisar leads → Executar ações → Resumo

Regras automáticas:
├─ nova_oportunidade > 48h → mover para follow_up + criar tarefa
├─ nova_oportunidade > 7 dias → mover para perda + atualizar status
└─ proposta_enviada > 5 dias → criar tarefa de cobrança + nota
```

---

## Troubleshooting

| Problema | Solução |
|---|---|
| `401 Unauthorized` | API Key inválida ou expirada. Gere uma nova no CRM. |
| `Connection refused` | CRM não está acessível. Verifique se o container `crm_app` está rodando. |
| `404 Not Found` | Funil ID 1 não existe. Crie um funil no CRM antes. |
| Workflow não dispara | Verifique se o toggle "Active" está ligado. |
| Variável `CRM_API_KEY` não reconhecida | Configure via N8N UI: Settings → Variables. |
