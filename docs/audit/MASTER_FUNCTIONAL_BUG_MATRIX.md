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
| W1-01 | Operador | Bia diz ao cliente que entrou na fila, mas responsável continua "Agente de IA" | RC-A1 handoff sem chamador (nenhum no do n8n alcanca a porta 8001) + RC-A2 handoff nao atribuia ninguem | Conversas + CRM |  | test_leads_handoff_bridge; test_conversas_operational_state 18 | RESOLVED | 092a781 + ponte |
| W1-02 | Operador | Atendente da conversa continua "Agente de IA" após triagem | DUPLICATE de W1-01; a linha tambem mostrava so o responsavel COMERCIAL | Conversas |  | test_conversas_operational_state | DUPLICATE_ROOT_CAUSE | 092a781 |
| W1-03 | Operador | FILA DE ESPERA não funciona / vazia ou errada | RC-A3 `queued_at` era apagado no instante em que um atendente era definido | Conversas |  | test_conversas_inbox_filters 1 | RESOLVED | 092a781 |
| W1-04 | Operador | ATENDIMENTOS BIA mistura clientes já prontos para humano | DUPLICATE de W1-01 (o bot nunca era desligado) | Conversas |  | test_conversas_inbox_filters | DUPLICATE_ROOT_CAUSE | 092a781 |
| W1-05 | Operador | MEUS ATENDIMENTOS não mostra clientes atribuídos | RC-A3 — `meus` exigia atendente e a conversa ja saira da fila ao ser atribuida | Conversas |  | test_conversas_inbox_filters 1 | RESOLVED | 092a781 |
| W1-06 | Operador | Leads já atendidos continuam aparecendo como aguardando | DUPLICATE de W1-03 | Conversas |  | test_conversas_inbox_filters | DUPLICATE_ROOT_CAUSE | 092a781 |
| W1-07 | Operador | Abrir/visualizar conversa tira ela da fila (não deveria) | Invariante novo: abrir nao toca estado operacional | Conversas |  | test_conversas_operational_state 18 | RESOLVED | 092a781 |
| W1-08 | Operador | Outro usuário abrir a conversa altera estado indevidamente | DUPLICATE de W1-07 | Conversas |  | test_conversas_operational_state 18 | DUPLICATE_ROOT_CAUSE | 092a781 |
| W1-09 | Operador | Contador da Julia inclui conversas do Beto | A linha mostrava `responsavel_nome` (comercial), nunca o atendente | Conversas |  | test_conversas_inbox_filters | RESOLVED | 092a781 |
| W1-10 | Operador | Conversas não lidas não são nítidas visualmente | Nao havia marcador visual proprio para a espera | Conversas UI |  | test_conversas_formatacao_mensagem; CSS | RESOLVED | 092a781 |
| W1-11 | Operador | Não existe badge "pós-Bia aguardando humano" | `/counts` nao expunha `aguardando_humano` | Conversas UI |  | test_conversas_inbox_filters 4 | RESOLVED | 092a781 |
| W1-12 | Operador | Clientes qualificados ficam presos na Bia | DUPLICATE de W1-01 | Conversas |  | test_conversas_operational_state | DUPLICATE_ROOT_CAUSE | 092a781 |
| W1-13 | Operador | Conversas que a Bia não respondeu ficam invisíveis | `_forward_to_agent` nao movia a conversa em falha degradada | Conversas |  | test_conversas_agent_timeout E | RESOLVED | 092a781 |
| W1-14 | Operador | Ordenação não reflete tempo de espera | Fila passa a ordenar por `queued_at ASC NULLS LAST, id` | Conversas |  | test_conversas_inbox_filters 2 | RESOLVED | 092a781 |
| W1-15 | Operador | Filtros perdem conversas | DUPLICATE de W1-03 + ausencia de desempate (F-523) | Conversas |  | test_conversas_inbox_filters 10 | RESOLVED | 092a781 |
| W1-16 | Operador | Testes antigos contaminam a contagem | Nao reproduzido: `/counts` conta por predicado, nao pela pagina carregada | Conversas |  | test_conversas_inbox_filters 4 | NOT_REPRODUCED_WITH_EVIDENCE | — |
| W1-17 | Requisito | Atribuição a atendente elegível deve ser configurável (hoje só Julia), sem hardcode | Nao existia pool; o unico seletor era `?responsavel_id=5` dentro do n8n | Conversas + CRM |  | test_conversas_operational_state 17 | RESOLVED | 092a781 |
| W1-18 | F-337 | `unread_count` significa duas coisas (bot zera após responder) | F-337 — `unread_count` significava duas coisas | Conversas |  | test_conversas_inbox_filters 4 | RESOLVED | 092a781 |
| W1-19 | F-085 | `PUT /conversations/{id}` grava `atendente_id`/`is_bot_active` fora de `_apply_human_state` | F-085 — o PUT escrevia estado operacional fora do ponto unico | Conversas |  | test_conversas_operational_state | RESOLVED | 092a781 |
| W1-20 | F-086 | `responsavel_id` commitado local e empurrado ao CRM em 2ª transação com resultado descartado | F-086 — `_apply_responsavel` era codigo morto; sync depois do commit | Conversas |  | test_conversas_assignment_notes | RESOLVED | 092a781 |
| W1-21 | F-087 / F-318 | claim/handoff são check-then-act sem lock | F-087/F-318 — check-then-act sem lock de linha | Conversas |  | test_conversas_operational_state (claim concorrente) | RESOLVED | 092a781 |
| W1-22 | F-115 | Conversa aberta não recebe mudanças de atendente/responsável/status feitas por outro | F-115 — o poll comparava so a contagem de mensagens | Conversas UI |  | test_conversas_service_window (polling) | RESOLVED | 092a781 |
| W1-23 | F-304 / F-316 | `responsavel_id` sem FK nem validação de existência | F-304/F-316 — `responsavel_id` nao validado | Conversas |  | test_conversas_assignment_notes | RESOLVED | 092a781 |
| W1-24 | F-523 | Ordenação default sem desempate → paginação duplica/pula linhas | F-523 — ordenacao sem desempate deterministico | Conversas |  | test_conversas_inbox_filters 10 | RESOLVED | 092a781 |

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
| W2-10 | Operador | Lead aparece em "Vendas WhatsApp" quando deveria estar no Principal | O Conversas PREFERIA qualquer funil ativo com whatsapp no nome | CRM |  | test_lead_funnel_entry 5 | RESOLVED | d211d61 |
| W2-11 | Operador | "Ver no Funil" não abre funil/etapa persistidos | | CRM UI | | | OPEN | |
| W2-12 | Operador | Localizar lead é intermitente | | CRM UI | | | OPEN | |
| W2-13 | F-341 | Lead criado sem `FunnelEntry` (reproduzido em PostgreSQL real) | F-341 — `POST /api/leads` criava so a linha `leads` | CRM + Conversas |  | test_lead_funnel_entry 1-2 + concorrencia no PostgreSQL | RESOLVED | d211d61 |
| W2-14 | Operador | Mover card no pipeline deixa cópia fantasma até o refresh | | CRM UI | | | OPEN | |
| W2-15 | Operador | Responsável do lead e da conversa divergem | O handoff so mudava o lead; a conversa nunca era avisada | CRM + Conversas |  | test_leads_handoff_bridge | RESOLVED | 092a781 + ponte |
| W2-16 | Operador | Conversa atribuída some da listagem | DUPLICATE de W1-03 | Conversas |  | test_conversas_inbox_filters | DUPLICATE_ROOT_CAUSE | 092a781 |
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
| W3-01 | Operador | Bia chama cliente pelo nome completo | Nenhuma regra separava o nome do CRM do nome falado ao cliente | KB/n8n |  | test_bna_agent_context_consistency H1 | FIXED_PENDING_MANUAL_N8N | 22a4e7f |
| W3-02 | Operador | Bia repete pergunta já respondida | A regra existente so cobria conversas DIFERENTES (`consultar_lead`) | KB/n8n |  | consistency H2 | FIXED_PENDING_MANUAL_N8N | 22a4e7f |
| W3-03 | Operador | Triagem não determinística: handoff com campo obrigatório faltando | Duas regras incompativeis sobre a MESMA alavanca `pronto_para_humano` | KB + repo |  | consistency H3 + validador | FIXED_PENDING_MANUAL_N8N | 22a4e7f |
| W3-04 | Operador | Bia promete ação interna inexistente ("já encaminhei", "prioridade máxima") | A categoria acao interna concluida nao existia no guardrail | KB |  | consistency H4 | FIXED_PENDING_MANUAL_N8N | 22a4e7f |
| W3-05 | Operador | Cotação antiga tratada como viagem confirmada | Nenhuma regra sobre contato recorrente / cotacao anterior | KB |  | consistency H5 | FIXED_PENDING_MANUAL_N8N | 22a4e7f |
| W3-06 | Operador | Bia afirma envio por e-mail (cotação vai por WhatsApp) | Nenhum arquivo dizia o canal de entrega da cotacao | KB |  | consistency H6 + validador | FIXED_PENDING_MANUAL_N8N | 22a4e7f |
| W3-07 | Operador | Uyuni: oferta de regular 1 dia / produto de 7 dias inexistente | O catalogo nao negava explicitamente produto de 1 e de 7 dias | KB |  | consistency H7 + validador | FIXED_PENDING_MANUAL_N8N | 22a4e7f |
| W3-08 | Operador | Uyuni 3 dias privativo oferecido a viajante solo (mín. 2 pax) | Minimo de 2 pax so existia no upgrade de quarto; a tabela contradiz a regra | KB |  | consistency H8 (conflito marcado, nao resolvido) | FIXED_PENDING_MANUAL_N8N | 22a4e7f |
| W3-09 | Operador | Redundância Rota dos Salares + Lagunas Altiplânicas | Sobreposicao de roteiro nao era um conceito modelado na KB | KB |  | consistency H9 | FIXED_PENDING_MANUAL_N8N | 22a4e7f |
| W3-10 | Operador | Alucinações (parceiros, pessoas, ações da equipe) | O guardrail so citava preco, disponibilidade e politica | KB + n8n guard |  | consistency H10 | FIXED_PENDING_MANUAL_N8N | 22a4e7f |
| W3-11 | Histórico | Vazamento de contexto interno / chain of thought | Cobertura por principio, sem few-shot de recusa a sondagem | n8n guard |  | consistency H11; guard de saida ja vivo no n8n (D7) | FIXED_PENDING_MANUAL_N8N | 22a4e7f |
| W3-12 | Operador | Emoji isolado gera resposta absurda | As regras de emoji tratavam so a QUANTIDADE | n8n |  | consistency H12; o n8n ja suprime (M3, 204) | RESOLVED | 22a4e7f |
| W3-13 | Operador | Links duplicados / não clicáveis / catálogo ausente | Nenhuma regra sobre envio de link | KB + Conversas UI |  | consistency H13 | FIXED_PENDING_MANUAL_N8N | 22a4e7f |
| W3-14 | Operador | B2B (agências) sem contexto | Contexto B2B inexistente | KB |  | consistency H14 | FIXED_PENDING_MANUAL_N8N | 22a4e7f |
| W3-15 | Operador | Erro Gemini vira loop "repita sua mensagem" | Falha da Bia deixava a conversa invisivel e o fallback pedia reenvio | Conversas + n8n |  | test_conversas_agent_timeout E | RESOLVED | 092a781 |
| W3-16 | F-073/F-074 | KB manda cotar preço `[PENDENTE_VALIDACAO]` e simultaneamente recusar | Decisao de negocio: qual preco 2026 vale | KB |  | mitigado em producao pelo bloco fixo de precos do subworkflow da KB | BLOCKED_OPERATOR | — |
| W3-17 | F-290/F-512 | Regra de altitude para <7 anos em três formas incompatíveis | Decisao medica/negocio: altitude para menores de 7 anos | KB |  | guard no teste impede resolver inventando valor | BLOCKED_OPERATOR | — |
| W3-18 | F-288 | Direito de arrependimento contradiz a escada de reembolso | Decisao juridica: direito de arrependimento vs escada de reembolso | KB |  | — | BLOCKED_OPERATOR | — |
| W3-19 | F-291/F-292 | FAQ responde imigração e promete reserva garantida contra o guardrail | Decisao de negocio: o que a FAQ pode responder sobre imigracao | KB |  | — | OPEN | — |
| W3-20 | F-138 | Prompt versionado instrui o modelo a duplicar handoff | ROOT-015 — o prompt versionado nao e o prompt vivo | KB/n8n |  | reconciliacao 26/08 | NOT_REPRODUCED_WITH_EVIDENCE | 1047aec |
| W3-21 | F-510/F-511 | Nomes de destino proibidos usados pela própria KB; sazonalidade conflitante | Decisao de negocio: nomes de destino e sazonalidade combinada | KB |  | — | OPEN | — |
| W3-22 | F-295 | Índice de pendências contradiz as próprias tabelas | A contagem do indice divergia das proprias tabelas dele | KB |  | consistency H15 | RESOLVED | 22a4e7f |

## WAVE 4 — Meta / templates / janela 24h / resiliência

| ID | Origem/relato | Sintoma | Root cause | Componente | Repo/N8N | Teste | Status | Commit |
|---|---|---|---|---|---|---|---|---|
| W4-01 | Operador | Janela de 24h detectada errado | Janela ancorada no relogio da Meta e monotonica desde a Fase 2 | Conversas |  | test_conversas_service_window (256-275) | NOT_REPRODUCED_WITH_EVIDENCE | — |
| W4-02 | Operador | Atendente não sabe quando template é obrigatório | | Conversas UI | | | OPEN | |
| W4-03 | F-349 | Envio sem credencial parece sucesso (`simulated` → `sent`) | F-349 ja corrigido na Fase 2: `simulated` virou status proprio | Conversas |  | test_conversas_webhook_hardening (265-291) | NOT_REPRODUCED_WITH_EVIDENCE | — |
| W4-04 | Operador | Status de envio ambíguo (sent/delivered/failed/pending) | | Conversas | | | OPEN | |
| W4-05 | Operador | Data+hora não permitem entender a janela | | Conversas UI | | | OPEN | |
| W4-06 | Operador | Template correto não é selecionável / sem preview | Picker e preview existem (`openTemplateForm`/`updatePreview`) | Conversas UI |  | test_conversas_template_param_map | NOT_REPRODUCED_WITH_EVIDENCE | — |
| W4-07 | F-347 | Lookup de template por nome só, ignorando o idioma | F-347 ja corrigido: o lookup e `(name, language)` | Conversas |  | test_conversas_service_window (399-412) | NOT_REPRODUCED_WITH_EVIDENCE | — |
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
| W5-02 | Operador | Leads de formulário chegam sem tags | `POST /api/leads` nao aplicava tag nenhuma (o caminho do WhatsApp aplicava) | n8n + CRM |  | test_lead_funnel_entry | RESOLVED | d211d61 |
| W5-03 | Operador | Segundo formulário (rodapé do site) não integrado | O segundo formulario nunca foi integrado a nenhum workflow | n8n |  | — | BLOCKED_OPERATOR | — |
| W5-04 | Operador | Campos do formulário inconsistentes com o CRM | | CRM | | | OPEN | |
| W5-05 | Operador | Formulário sobrescreve dado existente | D3 aplicada pelo operador — e introduziu a regressao M6 (`==` no jsonBody) | CRM + n8n (D3) |  | reconciliacao 26/08 secao 2 | FIXED_PENDING_MANUAL_N8N | 1047aec |
| W5-06 | Operador | Lead de formulário sem `FunnelEntry` adequado | DUPLICATE de W2-13 | CRM |  | test_lead_funnel_entry | DUPLICATE_ROOT_CAUSE | d211d61 |
| W5-07 | Operador | Contato automático da Bia não inicia após formulário | O disparo depende de um no do n8n que nao existe | n8n |  | — | BLOCKED_OPERATOR | — |
| W5-08 | Derivado | Formulário precisa respeitar janela/template Meta | A janela ja e imposta pelo backend em todo envio free-form | Conversas |  | test_conversas_service_window | NOT_REPRODUCED_WITH_EVIDENCE | — |

## WAVE 6 — Mensagens rápidas / editor

| ID | Origem/relato | Sintoma | Root cause | Componente | Repo/N8N | Teste | Status | Commit |
|---|---|---|---|---|---|---|---|---|
| W6-01 | Operador | Mensagens rápidas saem desformatadas | `.message-content` sem `white-space`: o CSS colapsava o `\n` | Conversas UI |  | test_conversas_formatacao_mensagem 1 | RESOLVED | 092a781 |
| W6-02 | Operador | Atalhos esperados não funcionam | A paleta `/` funciona e insere sem enviar | Conversas UI |  | test_conversas_formatacao_mensagem 4 | NOT_REPRODUCED_WITH_EVIDENCE | — |
| W6-03 | Operador | Copiar mensagem enviada destrói a formatação | Nao existia botao de copiar/reaproveitar em lugar nenhum do codigo | Conversas UI |  | test_conversas_formatacao_mensagem 3 | RESOLVED | 092a781 |
| W6-04 | Operador | Quebras de linha / negrito / itálico / sintaxe WhatsApp perdidos | Nenhuma renderizacao da marcacao do WhatsApp | Conversas UI |  | test_conversas_formatacao_mensagem 2 (10 casos executados no Node) | RESOLVED | 092a781 |
| W6-05 | Operador | Editar/reutilizar orçamento exige reconstruir a mensagem | DUPLICATE de W6-03 | Conversas UI |  | test_conversas_formatacao_mensagem 3 | DUPLICATE_ROOT_CAUSE | 092a781 |

## WAVE 7 — Segmentação / login / sessão / cache

| ID | Origem/relato | Sintoma | Root cause | Componente | Repo/N8N | Teste | Status | Commit |
|---|---|---|---|---|---|---|---|---|
| W7-01 | Operador | Lista de segmentação com comportamento quebrado | | CRM | | | OPEN | |
| W7-02 | Operador | Tela de segmentação "embaçada" | `erroNoDetalhe` nao removia `.show`/`.open` do overlay com blur | CRM UI |  | test_segmentacao_ui_fix | RESOLVED | b510ce7 |
| W7-03 | Operador | Filtros de segmentação não aplicam | 14 `onchange` fora do debounce e `previewCount` sem sequenciamento | CRM |  | test_segmentacao_ui_fix | RESOLVED | b510ce7 |
| W7-04 | Operador | Campos personalizados não filtrados corretamente | | CRM | | | OPEN | |
| W7-05 | Operador | Falha intermitente de login | | CRM | | | OPEN | |
| W7-06 | Operador | Usuários que acessavam deixam de acessar | | CRM | | | OPEN | |
| W7-07 | Operador | Safari em loop de login | O Conversas nao tinha o quebra-loop de um salto que o CRM ja tem | CRM |  | test_conversas_login_loop_guard (executa o script no Node) | RESOLVED | b510ce7 |
| W7-08 | Operador | Frontend antigo após atualização (precisa hard refresh) | JS compartilhado sem `?v=`; tokens manuais ja divergentes entre telas | Ambos |  | test_asset_cache_busting | RESOLVED | b510ce7 + 1047aec |
| W7-09 | F-043 | Filtro de campo personalizado 500 permanente com ` ` no Postgres | | CRM | | | OPEN | |
| W7-10 | F-440 | Debounce só nos campos de texto; 14 `onchange` sem sequenciamento | DUPLICATE de W7-03 | CRM UI |  | test_segmentacao_ui_fix | DUPLICATE_ROOT_CAUSE | b510ce7 |
| W7-11 | F-495 | Só uma página protegida preserva `?next=` no redirect de login | | CRM | | | OPEN | |
