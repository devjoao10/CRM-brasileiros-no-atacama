# MASTER_FUNCTIONAL_BUG_MATRIX

Inventário mestre da missão **Estabilização Funcional Completa** (CRM BnA +
Papos/Conversas + Bia), branch `audit/full-system-stabilization-2026-08-24`.

Cada sintoma relatado tem uma linha. Sintomas que compartilham causa são
marcados `DUPLICATE_ROOT_CAUSE` e apontam para o ID que carrega a correção.

**Status permitidos:** `OPEN` · `RESOLVED` · `FIXED_PENDING_MANUAL_N8N` ·
`FIXED_PENDING_PRODUCTION_VALIDATION` · `BLOCKED_OPERATOR` ·
`NOT_REPRODUCED_WITH_EVIDENCE` · `DUPLICATE_ROOT_CAUSE`

> Estado inicial deste documento: todos `OPEN`. As colunas Root cause / Teste /
> Commit são preenchidas conforme cada wave fecha.

---

## Procedência dos exports do n8n — RESOLVIDA

Quatro exports frescos chegaram durante a execução e estão versionados em
`n8n/workflows/live_exports/20260826_wa/`. A verificação campo a campo está em
**`docs/audit/N8N_RECONCILIACAO_20260826.md`**. Resumo:

- **M1, M2, M3, M4, M5, D1, D3, D7 — todos aplicados e verificados no export.**
  Nenhum foi revertido nem re-proposto.
- **D2** (`/webhook/agent-bia` sem autenticação) continua pendente.
- **M6 — regressão nova**, introduzida pela D3: o `jsonBody` do nó
  `Atualizar lead existente` do formulário começa com **dois** `=`. Mesmo
  mecanismo do M1. O corpo enviado deixa de ser JSON válido e o `PUT` falha em
  silêncio (`neverError: true`). Instrução completa na reconciliação.
- **A base de conhecimento da Bia não é `bna_agent_context/`.** O subworkflow
  `BIA — Consultar Knowledge Base` lê a Data Table n8n `bia_knowledge_base`.
  Editar o markdown não muda o comportamento em produção — por isso a Wave H
  entrega também as linhas da Data Table para o operador inserir.
- `Tool Alterar Responsavel` continua com `responsavel_id=5` fixo, e **nenhum
  nó do Gerenciador alcança a porta 8001** — a causa raiz da Wave 1 sobrevive
  intacta às mudanças manuais.

---

## WAVE 1 — Fila / handoff / pós-Bia / filtros

| ID | Origem/relato | Sintoma | Root cause | Componente | Repo/N8N | Teste | Status | Commit |
|---|---|---|---|---|---|---|---|---|
| W1-01 | Operador | Bia diz ao cliente que entrou na fila, mas responsável continua "Agente de IA" | | Conversas + CRM | | | OPEN | |
| W1-02 | Operador | Atendente da conversa continua "Agente de IA" após triagem | | Conversas | | | OPEN | |
| W1-03 | Operador | FILA DE ESPERA não funciona / vazia ou errada | | Conversas | | | OPEN | |
| W1-04 | Operador | ATENDIMENTOS BIA mistura clientes já prontos para humano | | Conversas | | | OPEN | |
| W1-05 | Operador | MEUS ATENDIMENTOS não mostra clientes atribuídos | | Conversas | | | OPEN | |
| W1-06 | Operador | Leads já atendidos continuam aparecendo como aguardando | | Conversas | | | OPEN | |
| W1-07 | Operador | Abrir/visualizar conversa tira ela da fila (não deveria) | | Conversas | | | OPEN | |
| W1-08 | Operador | Outro usuário abrir a conversa altera estado indevidamente | | Conversas | | | OPEN | |
| W1-09 | Operador | Contador da Julia inclui conversas do Beto | | Conversas | | | OPEN | |
| W1-10 | Operador | Conversas não lidas não são nítidas visualmente | | Conversas UI | | | OPEN | |
| W1-11 | Operador | Não existe badge "pós-Bia aguardando humano" | | Conversas UI | | | OPEN | |
| W1-12 | Operador | Clientes qualificados ficam presos na Bia | | Conversas | | | OPEN | |
| W1-13 | Operador | Conversas que a Bia não respondeu ficam invisíveis | | Conversas | | | OPEN | |
| W1-14 | Operador | Ordenação não reflete tempo de espera | | Conversas | | | OPEN | |
| W1-15 | Operador | Filtros perdem conversas | | Conversas | | | OPEN | |
| W1-16 | Operador | Testes antigos contaminam a contagem | | Conversas | | | OPEN | |
| W1-17 | Requisito | Atribuição a atendente elegível deve ser configurável (hoje só Julia), sem hardcode | | Conversas + CRM | | | OPEN | |
| W1-18 | F-337 | `unread_count` significa duas coisas (bot zera após responder) | | Conversas | | | OPEN | |
| W1-19 | F-085 | `PUT /conversations/{id}` grava `atendente_id`/`is_bot_active` fora de `_apply_human_state` | | Conversas | | | OPEN | |
| W1-20 | F-086 | `responsavel_id` commitado local e empurrado ao CRM em 2ª transação com resultado descartado | | Conversas | | | OPEN | |
| W1-21 | F-087 / F-318 | claim/handoff são check-then-act sem lock | | Conversas | | | OPEN | |
| W1-22 | F-115 | Conversa aberta não recebe mudanças de atendente/responsável/status feitas por outro | | Conversas UI | | | OPEN | |
| W1-23 | F-304 / F-316 | `responsavel_id` sem FK nem validação de existência | | Conversas | | | OPEN | |
| W1-24 | F-523 | Ordenação default sem desempate → paginação duplica/pula linhas | | Conversas | | | OPEN | |

## WAVE 2 — Integridade CRM ↔ Papos/Conversas

| ID | Origem/relato | Sintoma | Root cause | Componente | Repo/N8N | Teste | Status | Commit |
|---|---|---|---|---|---|---|---|---|
| W2-01 | Operador | Adicionar uma tag apaga as outras | | CRM + Conversas | | | OPEN | |
| W2-02 | Operador | Tags somem após reload | | CRM + Conversas | | | OPEN | |
| W2-03 | Operador | Precisa tentar várias vezes para a tag colar | | CRM UI | | | OPEN | |
| W2-04 | F-529 | Apagar tag no Conversas é desfeito ao reabrir a conversa | | Conversas | | | OPEN | |
| W2-05 | Operador | Data salva desaparece | | CRM | | | OPEN | |
| W2-06 | Operador | Dado fornecido pelo cliente desaparece | | CRM + n8n | | | OPEN | |
| W2-07 | Operador | Edição humana sobrescrita por update automático vazio | | CRM | | | OPEN | |
| W2-08 | F-239 | Anotações: read-modify-write em JSON sem lock (IA + humano) | | CRM | | | OPEN | |
| W2-09 | F-056 | `LeadUpdate` copia NULL explícito sobre coluna NOT NULL | | CRM | | | OPEN | |
| W2-10 | Operador | Lead aparece em "Vendas WhatsApp" quando deveria estar no Principal | | CRM | | | OPEN | |
| W2-11 | Operador | "Ver no Funil" não abre funil/etapa persistidos | | CRM UI | | | OPEN | |
| W2-12 | Operador | Localizar lead é intermitente | | CRM UI | | | OPEN | |
| W2-13 | F-341 | Lead criado sem `FunnelEntry` (reproduzido em PostgreSQL real) | | CRM + Conversas | | | OPEN | |
| W2-14 | Operador | Mover card no pipeline deixa cópia fantasma até o refresh | | CRM UI | | | OPEN | |
| W2-15 | Operador | Responsável do lead e da conversa divergem | | CRM + Conversas | | | OPEN | |
| W2-16 | Operador | Conversa atribuída some da listagem | | Conversas | | | OPEN | |
| W2-17 | Operador | Botão "Editar Lead" / rota direta abre lead errado ou falha | | CRM UI | | | OPEN | |
| W2-18 | Operador | Filtro de viajantes usa mínimo quando a regra pede quantidade exata | | CRM | | | OPEN | |
| W2-19 | F-084 | Quatro regras incompatíveis de normalização de telefone | | Conversas | | | OPEN | |
| W2-20 | F-312 / F-302 | Find-or-create de conversa por `whatsapp` sem UNIQUE nem lock | | Conversas | | | OPEN | |
| W2-21 | F-236 | `responsavel_id` mutável por 5 caminhos com 3 conjuntos de regra | | CRM | | | OPEN | |
| W2-22 | F-419 | `loadAllTags()` sem await + deep link `?open=` → checkboxes vazios | | CRM UI | | | OPEN | |
| W2-23 | F-427 | `formatWhatsappInput` indefinido no partial usado pelo pipeline | | CRM UI | | | OPEN | |
| W2-24 | F-430 | Filtro "Chegada em X dias" lido mas nunca enviado na request | | CRM UI | | | OPEN | |
| W2-25 | F-165 | `loadStage` descarta chamadas concorrentes; busca sem debounce | | CRM UI | | | OPEN | |
| W2-26 | F-246 | Substituir etapas órfã `FunnelEntry` das etapas removidas | | CRM | | | OPEN | |

## WAVE 3 — Bia / triagem / regras de negócio

| ID | Origem/relato | Sintoma | Root cause | Componente | Repo/N8N | Teste | Status | Commit |
|---|---|---|---|---|---|---|---|---|
| W3-01 | Operador | Bia chama cliente pelo nome completo | | KB/n8n | | | OPEN | |
| W3-02 | Operador | Bia repete pergunta já respondida | | KB/n8n | | | OPEN | |
| W3-03 | Operador | Triagem não determinística: handoff com campo obrigatório faltando | | KB + repo | | | OPEN | |
| W3-04 | Operador | Bia promete ação interna inexistente ("já encaminhei", "prioridade máxima") | | KB | | | OPEN | |
| W3-05 | Operador | Cotação antiga tratada como viagem confirmada | | KB | | | OPEN | |
| W3-06 | Operador | Bia afirma envio por e-mail (cotação vai por WhatsApp) | | KB | | | OPEN | |
| W3-07 | Operador | Uyuni: oferta de regular 1 dia / produto de 7 dias inexistente | | KB | | | OPEN | |
| W3-08 | Operador | Uyuni 3 dias privativo oferecido a viajante solo (mín. 2 pax) | | KB | | | OPEN | |
| W3-09 | Operador | Redundância Rota dos Salares + Lagunas Altiplânicas | | KB | | | OPEN | |
| W3-10 | Operador | Alucinações (parceiros, pessoas, ações da equipe) | | KB + n8n guard | | | OPEN | |
| W3-11 | Histórico | Vazamento de contexto interno / chain of thought | | n8n guard | | | OPEN | |
| W3-12 | Operador | Emoji isolado gera resposta absurda | | n8n | | | OPEN | |
| W3-13 | Operador | Links duplicados / não clicáveis / catálogo ausente | | KB + Conversas UI | | | OPEN | |
| W3-14 | Operador | B2B (agências) sem contexto | | KB | | | OPEN | |
| W3-15 | Operador | Erro Gemini vira loop "repita sua mensagem" | | Conversas + n8n | | | OPEN | |
| W3-16 | F-073/F-074 | KB manda cotar preço `[PENDENTE_VALIDACAO]` e simultaneamente recusar | | KB | | | OPEN | |
| W3-17 | F-290/F-512 | Regra de altitude para <7 anos em três formas incompatíveis | | KB | | | OPEN | |
| W3-18 | F-288 | Direito de arrependimento contradiz a escada de reembolso | | KB | | | OPEN | |
| W3-19 | F-291/F-292 | FAQ responde imigração e promete reserva garantida contra o guardrail | | KB | | | OPEN | |
| W3-20 | F-138 | Prompt versionado instrui o modelo a duplicar handoff | | KB/n8n | | | OPEN | |
| W3-21 | F-510/F-511 | Nomes de destino proibidos usados pela própria KB; sazonalidade conflitante | | KB | | | OPEN | |
| W3-22 | F-295 | Índice de pendências contradiz as próprias tabelas | | KB | | | OPEN | |

## WAVE 4 — Meta / templates / janela 24h / resiliência

| ID | Origem/relato | Sintoma | Root cause | Componente | Repo/N8N | Teste | Status | Commit |
|---|---|---|---|---|---|---|---|---|
| W4-01 | Operador | Janela de 24h detectada errado | | Conversas | | | OPEN | |
| W4-02 | Operador | Atendente não sabe quando template é obrigatório | | Conversas UI | | | OPEN | |
| W4-03 | F-349 | Envio sem credencial parece sucesso (`simulated` → `sent`) | | Conversas | | | OPEN | |
| W4-04 | Operador | Status de envio ambíguo (sent/delivered/failed/pending) | | Conversas | | | OPEN | |
| W4-05 | Operador | Data+hora não permitem entender a janela | | Conversas UI | | | OPEN | |
| W4-06 | Operador | Template correto não é selecionável / sem preview | | Conversas UI | | | OPEN | |
| W4-07 | F-347 | Lookup de template por nome só, ignorando o idioma | | Conversas | | | OPEN | |
| W4-08 | F-348 | Parâmetro de template não sanitizado (Meta rejeita \n/\t/runs) | | Conversas | | | OPEN | |
| W4-09 | F-350 | Nenhum retry/backoff; `send_attempts` existe e nunca é usado | | Conversas | | | OPEN | |
| W4-10 | F-109/F-083/F-112 | Retry sem idempotência → cliente recebe a mesma mensagem duas vezes | | Conversas | | | OPEN | |
| W4-11 | F-335 | Status callback da Meta chega antes do commit da mensagem | | Conversas | | | OPEN | |
| W4-12 | F-454 | Janela de 24h: naive vs aware datetimes divergem SQLite/Postgres | | Conversas | | | OPEN | |
| W4-13 | F-541 | Cache de catálogo de template sobrevive à troca de credencial | | Conversas | | | OPEN | |
| W4-14 | F-321 | Aridade do param map lida da linha local editável por qualquer usuário | | Conversas | | | OPEN | |

## WAVE 5 — Follow-up / formulários

| ID | Origem/relato | Sintoma | Root cause | Componente | Repo/N8N | Teste | Status | Commit |
|---|---|---|---|---|---|---|---|---|
| W5-01 | Operador | Cliente para de responder e fica no limbo (sem follow-up ~8h) | | n8n + Conversas | | | OPEN | |
| W5-02 | Operador | Leads de formulário chegam sem tags | | n8n + CRM | | | OPEN | |
| W5-03 | Operador | Segundo formulário (rodapé do site) não integrado | | n8n | | | OPEN | |
| W5-04 | Operador | Campos do formulário inconsistentes com o CRM | | CRM | | | OPEN | |
| W5-05 | Operador | Formulário sobrescreve dado existente | | CRM + n8n (D3) | | | OPEN | |
| W5-06 | Operador | Lead de formulário sem `FunnelEntry` adequado | | CRM | | | OPEN | |
| W5-07 | Operador | Contato automático da Bia não inicia após formulário | | n8n | | | OPEN | |
| W5-08 | Derivado | Formulário precisa respeitar janela/template Meta | | Conversas | | | OPEN | |

## WAVE 6 — Mensagens rápidas / editor

| ID | Origem/relato | Sintoma | Root cause | Componente | Repo/N8N | Teste | Status | Commit |
|---|---|---|---|---|---|---|---|---|
| W6-01 | Operador | Mensagens rápidas saem desformatadas | | Conversas UI | | | OPEN | |
| W6-02 | Operador | Atalhos esperados não funcionam | | Conversas UI | | | OPEN | |
| W6-03 | Operador | Copiar mensagem enviada destrói a formatação | | Conversas UI | | | OPEN | |
| W6-04 | Operador | Quebras de linha / negrito / itálico / sintaxe WhatsApp perdidos | | Conversas UI | | | OPEN | |
| W6-05 | Operador | Editar/reutilizar orçamento exige reconstruir a mensagem | | Conversas UI | | | OPEN | |

## WAVE 7 — Segmentação / login / sessão / cache

| ID | Origem/relato | Sintoma | Root cause | Componente | Repo/N8N | Teste | Status | Commit |
|---|---|---|---|---|---|---|---|---|
| W7-01 | Operador | Lista de segmentação com comportamento quebrado | | CRM | | | OPEN | |
| W7-02 | Operador | Tela de segmentação "embaçada" | | CRM UI | | | OPEN | |
| W7-03 | Operador | Filtros de segmentação não aplicam | | CRM | | | OPEN | |
| W7-04 | Operador | Campos personalizados não filtrados corretamente | | CRM | | | OPEN | |
| W7-05 | Operador | Falha intermitente de login | | CRM | | | OPEN | |
| W7-06 | Operador | Usuários que acessavam deixam de acessar | | CRM | | | OPEN | |
| W7-07 | Operador | Safari em loop de login | | CRM | | | OPEN | |
| W7-08 | Operador | Frontend antigo após atualização (precisa hard refresh) | | Ambos | | | OPEN | |
| W7-09 | F-043 | Filtro de campo personalizado 500 permanente com ` ` no Postgres | | CRM | | | OPEN | |
| W7-10 | F-440 | Debounce só nos campos de texto; 14 `onchange` sem sequenciamento | | CRM UI | | | OPEN | |
| W7-11 | F-495 | Só uma página protegida preserva `?next=` no redirect de login | | CRM | | | OPEN | |
