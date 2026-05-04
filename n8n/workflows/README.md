# 🔧 Setup dos Workflows N8N — CRM Brasileiros no Atacama

## Arquitetura

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│  WhatsApp   │◄───►│   Conversas  │◄───►│      CRM        │
│  (Meta API) │     │  (port 8001) │     │   (port 8000)   │
└─────────────┘     └──────┬───────┘     └────────┬────────┘
                           │                      │
                    ┌──────▼──────────────────────▼──────┐
                    │            N8N (port 5678)          │
                    │                                     │
                    │  WF-06: Funil Automático            │
                    │  WF-08: Notificações de Tarefas     │
                    │  WF-09: Relatório Diário            │
                    │  WF-10: Agente IA (Bia)       ★NEW │
                    │  WF-11: Envio WhatsApp        ★NEW │
                    └─────────────────────────────────────┘
```

## Pré-requisitos

1. N8N rodando na VPS (container `n8n_server` do `docker-compose.yml`)
2. CRM rodando e acessível como `http://crm:8000` dentro da rede Docker
3. Conversas rodando e acessível como `http://conversas:8001` dentro da rede Docker
4. Uma **API Key** gerada no CRM (via admin)
5. **META_ACCESS_TOKEN** e **META_PHONE_NUMBER_ID** configurados (para WhatsApp)

---

## Passo 1 — Gerar a API Key no CRM

1. Acesse o CRM e faça login como admin
2. Use o endpoint:
   ```bash
   curl -X POST http://crm:8000/api/auth/api-key \
     -H "Authorization: Bearer SEU_TOKEN_JWT" \
     -H "Content-Type: application/json"
   ```
3. Copie a API Key gerada

---

## Passo 2 — Configurar Variáveis no N8N

Configure via UI do N8N: **Settings → Variables → Add Variable**:

| Variável | Descrição | Exemplo |
|---|---|---|
| `CRM_API_KEY` | API Key do CRM | `crm_ak_xxxxx...` |
| `ADMIN_WHATSAPP` | WhatsApp do admin (para receber alertas) | `5511999999999` |
| `META_ACCESS_TOKEN` | Token do Meta Cloud API | `EAAxxxx...` |
| `META_PHONE_NUMBER_ID` | ID do número WhatsApp Business | `1234567890` |

---

## Passo 3 — Importar os Workflows

No N8N (UI web), clique em **"+"** → **"Import from File"**:

| Arquivo | Workflow | Quando roda | Status |
|---|---|---|---|
| `wf-06-funil-automatico.json` | Funil Automático + Conversas | A cada 2 horas | ✏️ Atualizado |
| `wf-08-notificacoes-tarefas.json` | Notificações de Tarefas + WhatsApp | Todo dia 08:00 | ✏️ Atualizado |
| `wf-09-relatorio-diario.json` | Relatório Diário + Conversas | Todo dia 19:00 | ✏️ Atualizado |
| `wf-10-agente-ia.json` | Agente IA (Bia) | Webhook (sob demanda) | ✨ Novo |
| `wf-11-notificacoes-whatsapp.json` | Envio WhatsApp (utilitário) | Webhook (sob demanda) | ✨ Novo |

> **⚠️ Importante**: Configure a credencial **Google Gemini** no N8N antes de importar o WF-10.

---

## Passo 4 — Configurar Credenciais

### Google Gemini (para WF-10)
1. No N8N, vá em **Credentials → Add Credential**
2. Busque **"Google Gemini"**
3. Cole sua `GEMINI_API_KEY`

### WhatsApp (para WF-11)
As variáveis `META_ACCESS_TOKEN` e `META_PHONE_NUMBER_ID` são usadas diretamente via `$env`.

---

## Passo 5 — Ativar os Workflows

1. Abra cada workflow
2. Teste manualmente clicando **"Execute Workflow"**
3. Se tudo funcionar, ative o toggle **"Active"** no canto superior direito

---

## Workflows — Documentação Detalhada

### WF-06: Funil Automático + Conversas

```
⏰ 2h → Carregar Kanban → Analisar leads → Rotear →┬→ Executar no CRM → Resumo
                                                      └→ Formatar Alertas → Enviar WhatsApp ↗
```

**Regras automáticas:**
- `nova_oportunidade` > 48h → mover para `follow_up` + criar tarefa
- `nova_oportunidade` > 7 dias → mover para `perda` + atualizar status
- `proposta_enviada` > 5 dias → criar tarefa de cobrança
- ★ **NOVO**: Lead sem responsável > 24h → alerta via WhatsApp

### WF-08: Notificações de Tarefas + WhatsApp

```
⏰ 08:00 → Buscar atrasadas → Buscar do dia → Buscar usuários → Formatar → Enviar WhatsApp
```

**Melhorias:**
- ★ Agora inclui nome do responsável em cada tarefa
- ★ Envio real via WhatsApp (não mais placeholder)

### WF-09: Relatório Diário + Conversas

```
⏰ 19:00 → KPIs → Relatório mensal → Board → Conversas → Montar relatório → Enviar WhatsApp
```

**Melhorias:**
- ★ Inclui métricas do Conversas (conversas abertas/encerradas)
- ★ Distribuição por responsável
- ★ Envio real via WhatsApp

### WF-10: Agente IA (Bia) ★ NOVO

```
Webhook ← Conversas → AI Agent (Gemini) → Resposta → Conversas → WhatsApp
                           │
                     13 Tools CRM:
                     ├─ Buscar/Criar/Detalhar/Atualizar Lead
                     ├─ Listar Funis / Adicionar / Mover
                     ├─ Adicionar Nota no Histórico
                     ├─ Listar/Atribuir Tags
                     ├─ Criar Tarefa
                     └─ ★ Alterar Responsável (Conversas)
```

### WF-11: Envio WhatsApp (Utilitário) ★ NOVO

```
Webhook ← Qualquer WF → Tem template? →┬→ Enviar Template HSM → Resultado → Resposta
                                         └→ Enviar Texto Livre  ↗
```

---

## Arquivos Obsoletos (remover)

| Arquivo | Motivo |
|---|---|
| `automacao_whatsapp_n8n.json` (raiz) | Substituído pelo WF-10 + Conversas |
| `n8n_agent_completo.json` (raiz) | Substituído pelo WF-10 |

---

## Troubleshooting

| Problema | Solução |
|---|---|
| `401 Unauthorized` | API Key inválida ou expirada. Gere uma nova no CRM. |
| `Connection refused crm:8000` | CRM não está acessível. Verifique se `crm_app` está rodando. |
| `Connection refused conversas:8001` | Conversas não está acessível. Verifique se `conversas_app` está rodando. |
| `404 Not Found` | Funil ID 1 não existe. Crie um funil no CRM antes. |
| Workflow não dispara | Verifique se o toggle "Active" está ligado. |
| `$env.CRM_API_KEY` vazio | Configure via N8N UI: Settings → Variables. |
| Gemini não responde | Verifique credencial Google Gemini no N8N. |
| WhatsApp não envia | Verifique META_ACCESS_TOKEN e META_PHONE_NUMBER_ID. |
| Mensagem fora da janela 24h | Use template HSM (WF-11 com template_name). |
