# Validação cruzada — AIA Harness, revisores especialistas e Context7

Etapa formal de validação executada em 2026-08-28, **depois** do mapeamento AS-IS
(`AS-IS.md`) e **antes** de qualquer proposta TO-BE. Estritamente read-only:
nenhum arquivo do repositório foi alterado, nenhum workflow do n8n foi modificado,
nenhuma migration executada, nenhuma mensagem enviada. O `=={{` do WF-01 permanece
intocado por decisão explícita do dono do sistema.

Rótulos de evidência: **CONFIRMADO** (lido/executado, com citação) ·
**INFERIDO** (dedução, com base declarada) · **NÃO CONFIRMADO** (não verificável
com os meios disponíveis; o que resolveria está registrado).

---

## 1. AIA Harness — o que ele é, e o que ele não é

**CONFIRMADO** — `~/.claude/plugins/cache/leandro-plugins-registry/aia-harness/0.17.1/.claude-plugin/plugin.json`:

> `"description": "Scan a project and scaffold a complete Claude Code harness: hooks, skills, agents, rules, settings, strategic MCPs, worktree config and per-domain CLAUDE.md. Diagnose, approve, apply."`

O AIA Harness é um **scaffolder de harness**, não uma suíte de análise de código.
Ele não possui checks de concorrência, idempotência, integridade de contratos,
side effects, risco de LLM ou qualidade de backend. Afirmar que ele auditou essas
dimensões seria falso.

Superfície real instalada: 13 comandos, 3 agentes, 7 skills, 1 CLI (`bin/harness.mjs`).

Para as dimensões de análise pedidas, a ferramenta real deste repositório são os
quatro revisores especialistas que o próprio `CLAUDE.md` torna obrigatórios
(`code-reviewer`, `security-reviewer`, `python-reviewer`, `fastapi-reviewer`, com
os critérios do skill `uncle-bob-craft`). Foram executados nesta etapa.

### 1.1 Execuções realizadas

| Ferramenta/check | Comando | Escopo | Resultado |
|---|---|---|---|
| Diagnóstico de stack | `harness.mjs scan` | repositório | executado; saída completa |
| Dependências de sistema | `harness.mjs check` | repositório | **BLOCKED** |
| Pilar GitHub PM | `harness.mjs pm-check` | repositório | **blocked** |
| Mapeamento arquitetural | agente `aia-harness:architecture-mapper` | ambos os apps | executado |
| Auditoria adversarial do harness | agente `aia-harness:harness-reviewer` | `.claude/**` | executado |
| Adjudicação da stack | agente `aia-harness:stack-analyst` | repositório | executado |

### 1.2 Achados do `scan`

| # | Achado | Sev. | Já descoberto na auditoria? |
|---|---|---|---|
| H-1 | Camadas detectadas sob `app/`; `conversas/` **sem** `repositories/` | ALTO | **SIM** (parcial — ver correção C-3) |
| H-2 | 50 arquivos > 350 linhas; maiores: `conversations.py` 1782, `webhook.py` 1236, `leads.py` 1218, `ai_tools.py` 1070 | ALTO | **SIM** |
| H-3 | "Unit tests: none" — nenhum teste detectado | MÉDIO | **NÃO** |
| H-4 | Comandos canônicos sugeridos citam `ruff`/`mypy`/`pytest`/`build` — nenhum instalado | MÉDIO | **NÃO** |
| H-5 | Linguagem primária "SQL 59%" | BAIXO | **NÃO** |
| H-6 | "não é monorepo" | BAIXO | **NÃO** |
| H-7 | `.mcp.json` não existe, mas o `CLAUDE.md` o referencia | BAIXO | **NÃO** |
| H-8 | `rtk` NOT FOUND, marcado `[required]`; existe `.claude/hooks/rtk-hook.mjs` | ALTO | **NÃO** |
| H-9 | `gh` sem escopos `admin:public_key`, `project` | BAIXO | **NÃO** |
| H-10 | Pilar GitHub PM não configurado | BAIXO | **NÃO** |

### 1.3 Adjudicação do `stack-analyst` — o scanner erra em 5 de 6

| Item | Veredito | Correto |
|---|---|---|
| Linguagem primária | **INCORRETO** | Python (236 arquivos versionados, 92,3% dos bytes) |
| Monorepo | **INCORRETO** | Dois serviços, duas árvores de dependência independentes |
| Testes | **INCORRETO** | **87** arquivos executáveis standalone |
| Comandos canônicos | **INCORRETO** | 5 dos 6 errados |
| Camadas | **PARCIAL** | Falta de `repositories/` é real; "nada sob `conversas/`" é falso |
| CI (`CONVERSAS_DIR`) | não opinou | Regra confirmada: 39 + 48 = 87, split exaustivo e disjunto |

Causa do "SQL 59%": **CONFIRMADO** — o scanner percorre diretórios *gitignored*.
Os três "arquivos SQL" grandes estão em `scratch/backup_e2e/` e são fixtures que o
próprio `tests/test_backup_restore_e2e.py` gera. Existe **um** `.sql` versionado:
`docker/postgres/init.sql`, 66 linhas.

Consequência prática se o plano do scanner fosse aplicado: hooks de
lint/typecheck/build quebrariam de imediato; o gate de teste **pularia 87 testes de
regressão**, incluindo os de segurança; e um install único reproduziria o conflito
de pins que o `CLAUDE.md` existe para prevenir.

**Descoberta relevante para o TO-BE:** rodar a suíte sob `pytest` seria **inseguro**,
não apenas desnecessário — 59 dos 87 arquivos executam asserções no import, e os dois
apps compartilham o nome de pacote `app`, colidindo no mesmo processo. A suíte precisa
continuar rodando um processo por arquivo.

### 1.4 NÃO EXECUTADO — com motivo técnico

| Capacidade | Motivo |
|---|---|
| `/aia-harness:doctor` | `allowed-tools` inclui `Edit`; invoca `harness.mjs apply --only=...`. **Muta o harness.** Substituído pelas partes read-only do CLI |
| `harness.mjs apply` | Escreve por natureza |
| `/aia-harness:init` | Faz scaffold novo sobre um harness em uso |
| `patch`, `add-mcp`, `add-tools`, `add-plugins`, `add-github-pm`, `add-obsidian` | Instalam/modificam artefatos |
| `revise-agent-routing`, `condense-harness-prompts` | Reescrevem `CLAUDE.md` e frontmatter |
| skills `revise-claude-md`, `revise-agent-frontmatter`, `revise-agent-routing-workflow`, `safe-hooks`, `harness-engineering`, `mcp-catalog`, `condense-harness-prompts` | São skills de **autoria** de harness; nenhuma produz achado sobre o sistema auditado |
| `harness.mjs plan` | Omissão deliberada: valor diagnóstico coberto por `scan` + `harness-reviewer` |

---

## 2. Correções a afirmações anteriores da auditoria

Registradas separadamente porque alteram conclusões já comunicadas.

### C-1 — `N8N_RESTRICT_ENVIRONMENT_VARIABLES_ACCESS` não existe

**Status anterior:** a auditoria afirmou que `docker-compose.yml:118` define
`N8N_RESTRICT_ENVIRONMENT_VARIABLES_ACCESS=false`, permitindo que expressões e Code
nodes leiam `$env` (incluindo `META_ACCESS_TOKEN` e a API key do CRM).

**Correção (Context7, docs oficiais do n8n):** essa variável **não existe** na
documentação do n8n. A variável real é **`N8N_BLOCK_ENV_ACCESS_IN_NODE`**:

> "Whether to allow users to access environment variables in expressions and the Code node (false) or not (true)."

**Consequência — pior do que o diagnóstico original.** Uma variável inexistente no
compose **não tem efeito nenhum**: o n8n aplica o default da variável real. O
controle de segurança que aparenta estar configurado é **inerte**.

**Agravante:** a documentação oficial se contradiz sobre o default — a página de
referência atual diz `false` (acesso permitido); o changelog de breaking changes da
v20 diz que o default passou a `true` (bloqueado).

**NÃO CONFIRMADO:** o estado efetivo na instância de produção.
**O que resolveria:** verificar na instância real qual variável está em uso e qual o
valor efetivo, ou testar uma expressão `{{ $env.META_ACCESS_TOKEN }}` num workflow de
rascunho não publicado.

### C-2 — o comportamento de `==` não é documentado

**Status anterior:** a auditoria classificou como CONFIRMADO que
`"=={{ ... }}"` avalia para a string literal `"=true"`/`"=false"`.

**Correção (Context7):** apenas o prefixo `=` como marcador de expressão está
confirmado por documentação oficial (changelog do n8n e exemplo de JSON exportado).
O comportamento específico de `==` **não aparece em nenhuma página oficial acessível**.

**Separação correta das duas afirmações:**

| Afirmação | Status |
|---|---|
| O valor gravado ao vivo difere de todos os irmãos e do export versionado; o nó foi editado à mão | **CONFIRMADO** — calibração: 16 expressões nos dois workflows ativos, 15 com `=` simples, só `pronto_para_humano` com `==`; o campo irmão `whatsapp`, no mesmo `parametersBody.values[]` do mesmo nó, usa `=` simples |
| Esse valor avalia para `"=true"` em runtime | **INFERIDO** — plausível e de conhecimento comum entre usuários de n8n, mas não citável em documentação |

**O que resolveria:** o payload real de uma execução (`get_execution` — as ferramentas
liberadas não listam execuções) ou o preview de expressão no editor visual.

### C-3 — "nada sob `conversas/`" era moldura do scanner, e é falsa

`conversas/app/` **tem** `models/`, `routers/`, `schemas/` e `services/`, cada um com
seu próprio `CLAUDE.md`. O que falta é **apenas** `repositories/`. O achado substantivo
(services falam SQLAlchemy direto, sem camada de acesso a dados) permanece.

### C-4 — o guard de segredos protege a ferramenta `Read`, não o `Bash`

**Status anterior:** a auditoria registrou que o guard "funciona", porque um agente foi
bloqueado ao ler `.env.example`.

**Correção:** verdadeiro apenas para a ferramenta `Read`. Ver achado HR-1 na §3.

### C-5 — a espera de 240s não bloqueia o event loop

`_fetch_agent_parts` (`conversas/app/routers/webhook.py:1018`) usa `httpx.AsyncClient`
real e fecha a sessão do banco (`db.close()`, linha 1131) **antes** do `await`,
com comentário `AUDIT-2026-08-WF2 D1` explicando o motivo. A preocupação com
esgotamento de pool durante a espera **não procede**. O bloqueio real está em outro
lugar (ver FA-1).

### C-6 — a Dependency Rule não está violada

Verificado por grep nos dois apps: nenhum model importa service, nenhum service
importa router. **A regra de dependência se sustenta estruturalmente.** O dano real é
duplicação de regra de negócio e alcance cross-*service* por SQL cru — não violação de
camada. Um refactor de camadas seria trabalho desperdiçado.

### C-7 — são 87 arquivos de teste, não 86

---

## 3. Achados novos da etapa de validação

Numerados por origem: **HR** harness-reviewer · **SEC** security-reviewer ·
**PY** python-reviewer · **FA** fastapi-reviewer · **CR** code-reviewer ·
**AM** architecture-mapper.

### CRÍTICO

| # | Achado | Evidência | Dano |
|---|---|---|---|
| CR-1 | **Nada abaixo de WARNING sai do processo.** Nenhum `main.py` chama `basicConfig`/`dictConfig`; Dockerfiles sobem uvicorn sem `--log-config`; o `LOGGING_CONFIG` do uvicorn não define a chave `"root"` | verificado empiricamente | Toda a trilha `.info` que o código já escreve — "Handoff BIA→humano", "Debounce: enviando N msg(s)", "Resposta da Bia" — **nunca sai do processo**. Não há como saber se o handoff dispara ou não |
| HR-1 | **Regras de `deny` são escopadas por ferramenta, não por caminho.** `Read(.env)` negado e `Bash: wc -l .env` negado, mas `Bash: python -c "open('.env').read()"` **permitido, exit 0** — `Bash(python:*)` está no allow | reproduzido diretamente; `.claude/settings.json:32-33` vs `:39-48` | Com `defaultMode:"bypassPermissions"` e sem humano no loop, qualquer one-liner induzido exfiltra segredos |
| HR-2 | **`validate-settings-schema.mjs` derruba o processo Node** no caminho padrão (cold-cache, com rede): `Assertion failed: !(handle->flags & UV_HANDLE_CLOSING) ... exit 127`. Forçando o ramo de arquivo local, sai limpo com 0 | reproduzido duas vezes; `.claude/hooks/validate-settings-schema.mjs:125` | A validação de schema do `settings.json` **nunca completa nesta máquina**. Fail-open por crash, não pelo contrato documentado |

### ALTO

| # | Achado | Evidência | Dano |
|---|---|---|---|
| SEC-1 | **Cadeia de ataque completa:** mensagem de cliente → Gerenciador (LLM, defesas só em prosa) → `PUT /api/leads/{id}/responsavel` (só `get_current_user`) → **reatribui qualquer lead a qualquer usuário** | `app/routers/leads.py:1092-1098` | Integridade comercial: roubo/sabotagem de carteira |
| PY-1 | **Falta `db.rollback()` no `except`** de `lookup_lead_by_whatsapp` e `get_lead_pipeline_info`; cinco funções irmãs no mesmo arquivo têm, com comentários `AUDIT-2026-08-W2F-orq` | `conversas/app/services/crm.py:129-135`, `:177-179` | No Postgres a transação fica abortada: **toda operação seguinte da mesma request falha, inclusive persistir a mensagem inbound**. SQLite não reproduz — nenhum dos 87 testes pega |
| FA-1 | **Bloqueio sistêmico do event loop.** Engines são `create_engine` síncrono; toda rota de `webhook.py`/`conversations.py` é `async def` chamando `db.query()` sem `run_in_threadpool`. `list_conversations`/`get_conversation` são **pollados a cada 5s por aba aberta** | `conversas/app/routers/*.py`; contraste: `app/routers/leads.py` usa `def` síncrono corretamente | Bloqueio recorrente multiplicado por atendente conectado |
| FA-2 | **Perda silenciosa sem recuperação:** o `except` amplo de `_debounce_then_forward` cobre o commit final de `_forward_to_agent`, que roda **depois** de as mensagens já terem sido enviadas ao cliente (`commit=False` no loop). O log diz só "Erro no debounce da conversa X", **sem os wamids** | `webhook.py:924-925`, `:1168`, `:1218`; contraste `outbound.py:280-286` | Cliente recebe; banco não registra; sem como reconciliar |
| FA-3 | `PUT /api/conversations/{id}` comita `status='encerrada'` e **depois** envia a mensagem de encerramento sem try/except; `record_outbound_message` levanta se seu commit falhar | `conversations.py:1145`, `:1182-1186` | Retorna 500 para uma operação que teve sucesso |
| FA-4 | Mais **dois GETs que escrevem**: `GET /api/conversations` (read-repair comita) e `GET /{id}/crm-link` | `conversations.py:740`, `:1240` | Quebra a semântica "GET é seguro"; sensível a prefetch/cache |
| PY-2 | `store_bytes` grava em disco **depois** de `record_outbound_message` comitar `status='sent'` | `media_storage.py:108,117`; `outbound.py:351-368` | Falha de disco vira 500 para um envio que já saiu |
| PY-3 | A garantia read-only no Postgres depende **só do GRANT externo**; sem `SET TRANSACTION READ ONLY`. O ramo SQLite impõe pelo driver (`?mode=ro`) | `app/services/ai_tools.py:43-66` | GRANT mal configurado em produção não é pego por teste nenhum |
| PY-4 | Ramo SQLite monta `ilike` sem `autoescape`; a função irmã usa `autoescape=True` com comentário explicando | `app/query_filters.py:227` vs `:184` | Dev/CI diverge de produção |
| HR-3 | Regex do `guard-main-branch.mjs` exige **espaço** antes de `main`. `git push origin feature-x:main` **não casa** | `.claude/hooks/guard-main-branch.mjs:63` | Push direto para `main` remoto sem confirmação, com sintaxe git corriqueira |
| HR-4 | `currentBranch()` engole falha e retorna `""`, lido como "não estou no main" | `guard-main-branch.mjs:43-53` | Guard **silenciosamente desligado**, não apenas pulado |

### MÉDIO

| # | Achado | Evidência |
|---|---|---|
| SEC-2 | **HMAC não cobre a query string.** `_canonical_path()` descarta a query antes de assinar. `PUT /api/leads/{id}/responsavel?responsavel_id=X` **não tem body** — o dado que decide quem recebe o lead trafega sem cobertura. Replay dentro de 300s trocando só o parâmetro reatribui qualquer lead sem forjar HMAC | `app/services/internal_ai_auth.py:36-46`; `app/routers/leads.py:1097` |
| SEC-3 | Senha fallback hardcoded `crm_readonly_secret`; `pg_hba.conf` libera essa conta de todas as faixas Docker, inclusive do container `n8n` | `docker/postgres/init-hardening.sh:14` |
| AM-1 | `ai_tools.py::add_tag_to_lead` faz get-or-create sem proteção de corrida, e `create_task` **nunca seta `user_id`**; ambos chamáveis pelo LLM a cada turno | `app/services/ai_tools.py:555-586`, `:520-553` |
| PY-5 | `add_tag_to_lead` do `crm.py`: check-then-act sem SAVEPOINT; docstring diz "idempotente" e não é | `conversas/app/services/crm.py:694-716` |
| PY-6 | `auto_create_lead_in_crm` comita sozinho e `auto_link_conversation` comita de novo: duas transações para uma operação atômica | `crm.py:531`, `:591` |
| CR-2 | O espelho de funil em `crm.py` insere `funnel_entries` **sem** o SAVEPOINT do lado autoritativo; o `except` faz rollback de tudo, **inclusive do Lead recém-criado** | `crm.py:410-428` vs `lead_creation.py:294-343` |
| FA-5 | `LeadBase`/`LeadUpdate` sem `extra="forbid"`: `{"nome":"Teste","whatsap":"5511..."}` → **201 Created, lead sem WhatsApp, sem 422** | `app/schemas/lead.py` |
| FA-6 | `MessageCreate.msg_type: str` livre: `"banana"` cria um `Message` fantasma com `status='failed'` para um envio nunca tentado | `conversas/app/schemas/conversation.py:9` |
| HR-5 | `secret-scan.mjs` cobre 8 formatos de token de fornecedor; **sem** checagem de entropia nem padrão para `SECRET_KEY=`, `INTERNAL_AI_AUTH_SECRET=` ou connection string | `.claude/hooks/secret-scan.mjs:32-41` |
| HR-6 | `rtk-hook.mjs` faz `process.exit(0)` quando não acha o binário — **zero stderr, zero log**. O `aia-harness check` classifica `rtk` como `[required]` | `.claude/hooks/rtk-hook.mjs:19` |

### BAIXO

| # | Achado |
|---|---|
| CR-3 | `X-Conversa-Handoff` **tem** consumidor — `templates/pipeline.html:1071`, para troca manual pela UI. `grep` em `n8n/` não acha referência: **a via automatizada não tem quem leia o sinal** |
| CR-4 | Código morto: `conversations.py:1248` — `CRM_BASE_URL.replace(":8000", ":8000")`, no-op |
| CR-5 | Normalização de telefone **triplicada**: `crm.py:26-28`, `leads.py:85-94` (cópia deliberada, comentada), `conversations.py:527` (inline, **sem** comentário reconhecendo a duplicação) |
| FA-7 | `list_custom_field_keys` e `append_anotacao` são `async def` **sem nenhum `await`**; a segunda segura `with_for_update()` durante o bloqueio. Correção mínima: trocar por `def` |
| FA-8 | `response_model` ausente em 7 rotas; `/send-notification` muda a *shape* entre o ramo simulado e o real |
| SEC-4 | Webhook da Meta sem rate limit próprio (mitigado por HMAC) |
| SEC-5 | UI do n8n exposta publicamente via Traefik |

---

## 4. Cross-check — harness vs. sete investigadores

### 4.1 Achados confirmados pelo harness

| Achado original | Como o harness corroborou |
|---|---|
| `conversas/` não tem camada de repositório; services falam SQLAlchemy direto | `scan` detectou camadas só sob `app/`; `architecture-mapper` confirmou a ausência de `repositories/` por listagem de diretório |
| A máquina de estados está fundida com roteamento HTTP em god-files | `scan` listou 50 arquivos > 350 linhas, com `conversations.py` (1782) e `webhook.py` (1236) no topo |
| Regra de negócio duplicada entre CRM e Conversas | `architecture-mapper` mapeou a duplicação e **explicou a causa**: os dois processos se chamam `app` e não podem se importar — parede de pacote, não descuido |

### 4.2 Achados novos do harness

Todos os itens da §3 marcados HR, mais H-3 a H-10 da §1.2. Os de maior peso:
HR-1 (bypass do guard de segredos via Bash), HR-2 (validação de settings nunca roda),
HR-3/HR-4 (guard de branch com buraco e fail-open silencioso), H-8 (`rtk` morto).

### 4.3 Achados dos agentes não detectados pelo harness

O harness **não detectou nenhum** dos achados centrais da auditoria — o que é
esperado, dado o que ele é (§1). Especificamente invisíveis para ele:
`pronto_para_humano` nunca tocar em Python; a regressão do `==`; o handoff em duas
transações; a ponte best-effort; as duas definições de fila; os dois caminhos de
reabertura; a Bia falar depois do handoff; a ausência de timestamps de métricas; e
os dois workflows opacos.

**Conclusão metodológica:** o AIA Harness é complementar, não substituto. Ele cobre a
saúde do *ambiente de desenvolvimento*; a auditoria cobre o *sistema de produção*.
Nenhum dos dois enxerga o que o outro enxerga.

### 4.4 Contradições — investigadas, não descartadas

| # | Contradição | Resolução |
|---|---|---|
| 1 | **Handoff: arquivo vs. produção.** Cinco agentes descreveram `PUT /api/leads/{id}/responsavel?responsavel_id=5` a partir do export versionado; o agente que inspecionou a instância live achou o nó reescrito em 2026-08-27 para `POST /webhook/handoff-julia-interno` | **Ambos corretos, evidências diferentes.** O repositório não é fonte de verdade do n8n. Prevalece o live; o arquivo está desatualizado |
| 2 | **`pronto_para_humano`: `=` vs `==`.** Export do repo tem `=` simples (correto); a instância live tem `==` | **Ambos corretos.** É drift real, confirmado por calibração. Ver C-2 para a separação entre o drift (confirmado) e sua consequência em runtime (inferida) |
| 3 | **`.env.example`: legível ou não?** O security-reviewer foi bloqueado; o harness-reviewer leu nomes de variáveis | **Ambos corretos.** O guard bloqueia a ferramenta `Read`; não bloqueia `Bash`+`python`. Ver HR-1/C-4 |
| 4 | **Guard de segredos: funciona?** A auditoria afirmou que sim | **Refutado.** Ver C-4 e HR-1 |
| 5 | **240s esgota o pool?** Levantado como risco no mapeamento inicial | **Refutado.** Ver C-5 |
| 6 | **Camadas violadas?** Sugerido pelo "layered" do `scan` e pela god-file analysis | **Refutado.** Ver C-6 |
| 7 | **"Gerente Autônomo" arquivado?** Um agente marcou NÃO CONFIRMADO (só evidência de arquivo) | **Resolvido por cross-reference:** a inspeção live confirmou arquivado |
| 8 | **Default de `N8N_BLOCK_ENV_ACCESS_IN_NODE`** — duas páginas oficiais do n8n se contradizem (`false` na referência, `true` no changelog da v20) | **NÃO RESOLVIDO por documentação.** Precisa de verificação na instância |

---

## 5. Context7 — validação técnica

| Claim | Veredito | Nota |
|---|---|---|
| C1 — prefixo `=` / `==` em expressões n8n | **NÃO RESOLVE** | Só o `=` está documentado. Ver correção C-2 |
| C2 — `retryOnFail` no AI Agent reexecuta tools | **CONFIRMA** | Consequência de três mecanismos documentados: retry é por node inteiro, tools rodam dentro do `execute()` do Agent, memória é só de conversa |
| C3 — `$fromAI` description é dica, não validação | **CONFIRMA** | A doc usa literalmente "hints rather than existing value references" |
| C4 — variável de acesso a env | **CORRIGE** | Nome real: `N8N_BLOCK_ENV_ACCESS_IN_NODE`. Ver correção C-1 |
| C5 — webhook `responseNode` é síncrono | **CONFIRMA** | Conexão aberta até o node Respond to Webhook; um retry do POST inicia execução independente |
| C6 — `begin_nested()` = SAVEPOINT | **CONFIRMA** | Com ressalvas de autoflush forçado e expiração seletiva de estado |
| C7 — `FOR UPDATE` no Postgres; SQLite não suporta | **CONFIRMA** | SQLite usa locks de arquivo inteiro, sem lock por linha. O mecanismo exato (no-op vs. erro) não está documentado |
| C8 — UNIQUE é a única proteção real contra duplicata | **CONFIRMA** | As docs do Postgres afirmam que **até em Serializable** a violação pode ocorrer sob concorrência, exigindo tratar `IntegrityError` |
| C9 — sem garantia transacional entre commit e HTTP; BackgroundTasks pós-resposta | **CONFIRMA** | Nuance: o exception handler **é** acionado, mas não pode mais alterar a resposta já enviada |
| C10 — Gemini `response_schema` restringe de verdade | **CONFIRMA** | Decodagem restrita real. Modos de falha documentados: schema rejeitado por complexidade, bloqueio por safety/recitation, `Malformed_Function_Call` |

**Implicação de C10 para o TO-BE:** a saída estruturada do Gemini é **imposta pela API**,
não pedida por prompt — o que elimina a classe de erro do `$fromAI`. Mas tem modos de
falha próprios que precisam de tratamento determinístico, não de confiança cega.

**Implicação de C8:** a ausência de `UNIQUE` em `leads.whatsapp` não é mitigável por
disciplina de aplicação. As docs do Postgres são explícitas.

---

## 6. Audit Execution Log

| Ferramenta | Ação | Objetivo | Evidência | Resultado |
|---|---|---|---|---|
| **Superpowers** | Skill `superpowers:brainstorming`, caminho arquitetural | Estruturar a investigação e travar o gate de aprovação antes de qualquer implementação | Classificação anunciada; ordem audit → document → propose respeitada | Usado. Gate mantido: nada implementado |
| **Subagents** | 8 investigadores AS-IS + 8 de validação, todos read-only, despachados em paralelo | Cobrir escopos disjuntos com propriedade de arquivo declarada | 16 relatórios; 7 arquivos em `scratchpad/audit/` | Usado. Base de todo o AS-IS |
| **Graphify** | `graphify query` sobre o fluxo de mensagem | Orientação inicial antes de qualquer grep (exigido por hook do projeto) | 911 nós retornados, truncados a 58 pelo orçamento de tokens | Usado, **com baixo rendimento**. A consulta ampla devolveu ruído; a orientação real veio da leitura direta. Registrado como limitação |
| **Código** | Leitura integral dos arquivos do caminho crítico pelos agentes | Determinar comportamento real, não o nome | Cada afirmação com `file:line` | Usado. Fonte primária da auditoria |
| **n8n MCP** | `search_workflows`, `get_workflow_details`, `search_data_tables` | Comparar repositório vs. instância live | 17 workflows enumerados; drift de 27-28/08 detectado; calibração de 16 campos de expressão | Usado, **read-only**. Nenhuma ferramenta de escrita invocada. **Dois workflows ativos recusaram leitura** (`availableInMCP:false`) |
| **Banco/schema** | Leitura estática de models, migrations, `init.sql` | Mapear estado, constraints e fontes de verdade | Catálogo de colunas de estado; 12 migrations conferidas | Usado. **Nenhuma conexão ao banco foi aberta**; nenhuma query executada |
| **AIA Harness** | `scan`, `check`, `pm-check` + 3 agentes | Diagnóstico de stack, dependências e segurança do harness | §1 deste documento | Usado. Comandos mutantes **não executados**, com motivo registrado |
| **Context7** | 10 claims técnicos validados contra docs oficiais | Não assumir comportamento de framework quando é verificável | §5 deste documento | Usado. **2 correções produzidas** (C1, C4) |

**Não utilizados:** `get_execution` do n8n (as ferramentas liberadas não listam
execuções — impediu a prova mais direta do `==`); editor visual do n8n (sem acesso
autenticado pelo navegador); execução da suíte de testes (não requisitada nesta etapa
e sem valor diagnóstico adicional para o escopo auditado).

---

## 7. Questões ainda NÃO CONFIRMADAS

Ordenadas por impacto sobre o TO-BE.

| # | Questão | O que resolveria |
|---|---|---|
| Q-1 | **O que o workflow `Handoff Humano → Julia` realmente faz.** É o mecanismo real de handoff do sistema inteiro, criado em 27/08, ativo, e não existe no repositório | Ligar `availableInMCP` nele (solicitado ao dono; pendente) ou exportar o JSON |
| Q-2 | **O que o workflow `BIA — Buscar Contexto BNA` faz**, e quem o chama. Ativo, sem chamador identificado | Idem |
| Q-3 | **Valor efetivo do acesso a `$env` no n8n de produção** — o compose define uma variável inexistente; o default da variável real é contraditório entre duas páginas oficiais | Inspecionar a instância; ou testar `{{ $env.X }}` num rascunho não publicado |
| Q-4 | **O `==` avalia mesmo para `"=true"` em runtime** | Payload de uma execução real, ou o preview do editor visual |
| Q-5 | **Estado live do workflow `Formulário do Site`** — `availableInMCP:false`; toda a análise vem do export de 26/08 | Ligar o toggle ou exportar |
| Q-6 | **Quantos workers uvicorn rodam em produção.** Toda a coordenação de debounce/lock é por processo; com >1 worker, quebra | `docker compose ps` / inspeção do comando de start em produção |
| Q-7 | **Se `permissionDecision:"ask"` de um hook pausa sob `defaultMode:"bypassPermissions"`** — determina se o `worktree-write-guard` tem enforcement real | Teste interativo |
| Q-8 | **`jsonBody` do nó `Atualizar lead existente`** (Formulário) começa com `==` — mesma classe do M1, mas a reconciliação de 26/08 retratou como falso positivo por uma distinção estrutural que não se sustentou à releitura | Mesma verificação de Q-4, aplicada a esse nó |
| Q-9 | **Frequência real do cenário Meta-aceita-mas-commit-falha** | Telemetria — que hoje não existe (CR-1) |

Note-se que **Q-9 depende de CR-1**: sem logging funcional, várias destas perguntas
são estruturalmente irrespondíveis. Isso posiciona a observabilidade como
pré-requisito de qualquer medição, não como melhoria opcional.

---

## 8. O que esta etapa mudou na leitura do sistema

1. **A causa raiz ficou mais nítida.** Não é só "a IA decide demais". É que
   **não existe camada onde uma decisão errada seja recusada** — sem schema, sem
   constraint, sem validação de contrato — **e não existe observabilidade para
   perceber que foi recusada ou não**. O `==` atravessou duas camadas de LLM sem
   gerar um único erro, e nenhum log teria mostrado.

2. **O padrão correto já existe no repositório, aplicado pela metade — três vezes.**
   `conversas_bridge` (HTTP autenticado) vs. SQL cru na direção maior;
   `_obter_ou_criar_tag` (SAVEPOINT + retry) vs. duas cópias inseguras;
   `lead_history` (trilha de eventos) vs. nada equivalente para a conversa.
   A migração é **terminar de aplicar o que já foi provado**, não inventar arquitetura.

3. **Restrição estrutural nova:** CRM e Conversas não podem se importar — ambos os
   pacotes se chamam `app`. Qualquer regra compartilhada reproduzirá a duplicação
   atual a menos que a fronteira mude antes (pacote `shared`, ou HTTP em vez de import).
   Isso vira decisão explícita do TO-BE.

4. **As costuras da migração já existem e são puras.** `aplicar_estado_humano`,
   `marcar_atendimento_humano`, `record_outbound_message` e
   `resolver_atendente_elegivel` não importam FastAPI e são exatamente os pontos que
   um motor de regras chamaria. **Não precisam mudar.** O que falta é a decisão —
   que hoje não existe em Python nenhum.

5. **A camada não está quebrada.** A Dependency Rule se sustenta. Refactor de camadas
   seria trabalho desperdiçado; o alvo é duplicação e fronteira, não estrutura.
