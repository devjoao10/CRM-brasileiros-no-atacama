# FULL_SYSTEM_AUDIT.md — auditoria global do sistema BnA

Auditoria read-only integral do repositório, seguida de estabilização.
Commit base: `d4831486b767988ed2b91518167d8c50fbeb636e` (HEAD de `main`).
Branch de trabalho: `audit/full-system-stabilization-2026-08-24`.

---

## 1. O sistema, como ele realmente é

Não como a documentação descreve — como o código faz.

**Dois serviços FastAPI sobre UM banco PostgreSQL.**

| | CRM (`app/`) | Conversas (`conversas/`) |
|---|---|---|
| Python | 3.11 | 3.12 |
| Porta / host | 8000 / crm.crmbrasileirosnoatacama.cloud | 8001 / conversas.crmbrasileirosnoatacama.cloud |
| Função | leads, pipeline, segmentos, tarefas, kanban operacional, IA "Perpétua" | inbox WhatsApp (Meta Cloud API), agente "Bia" via n8n |
| Tabelas declaradas | 29 | 15 |

**Tabela compartilhada: `users`, e só ela.** Verificado enumerando
`__tablename__` nos dois serviços. `tags` e `leads` são exclusivas do CRM — o
Conversas usa `conversation_tags` próprias e alcança `leads` **por SQL cru**.

**Mais três componentes:** n8n (5 workflows versionados + 1 que existe só no
export de produção), Meta WhatsApp Cloud API, e um Traefik externo que termina
TLS para os três hosts. O Traefik **não está neste repositório** — só os labels.

**Entrypoints reais:**
- `app/main.py` → 20 routers, todos `/api/*` exceto as 13 páginas Jinja.
- `conversas/app/main.py` → 12 routers.
- `conversas/app/routers/webhook.py` → **o único ponto de entrada não autenticado**, alcançável pela internet, protegido por HMAC `X-Hub-Signature-256`.
- 3 webhooks n8n públicos **sem autenticação nenhuma**.

**Persistência:** `Base.metadata.create_all()` no startup dos **dois** serviços,
mais 10 scripts de migration manuais sem ledger. As 15 tabelas
`operational_*`, `message_templates`, `api_config`, `quick_replies`, e todas as
tabelas CRM exceto `leads`/`tasks`/`internal_tasks` **nunca passaram por
migration** — existem só por `create_all`, que nunca faz `ALTER`.

**Mapa estrutural (Graphify, baseline):** 3.182 nós, 7.148 arestas, 290 arquivos
com nós. Maior blast radius: `app/database.py` (in-degree 91), `app/main.py`,
`app/auth.py` (grau agregado 281; `get_current_user` sozinho tem in-degree 51),
`conversas/static/js/conversas.js`, `conversas/app/routers/conversations.py`.
Essa medição definiu quais 43 arquivos receberam segunda revisão independente.
Limitação registrada: os JSON de workflow n8n produziram zero nós e o `.sql` não
foi parseado (`tree_sitter_sql` ausente) — ambos foram lidos manualmente.

---

## 2. Método

22 agentes independentes, propriedade de arquivo declarada, **proibidos de
editar qualquer coisa** durante a fase read-only. Cada um declarou os ranges que
efetivamente leu; nenhum arquivo foi marcado como revisado por ter aparecido em
grep ou no grafo.

- 18 agentes de primeira passada, cobrindo os 345 arquivos no escopo.
- 3 agentes transversais: arquitetura entre arquivos, threat review de segurança, e uma **segunda passada independente** sobre os 10 arquivos de maior blast radius (esse agente não viu os relatórios dos outros).
- 1 baseline de qualidade executado antes de qualquer edição.

**O que eu verifiquei pessoalmente**, em vez de aceitar do relatório:
o SSRF do `call_internal_api` (reproduzido), o `return 200` do webhook que
descarta o lote, a ordem auto-reply→debounce que impede a Bia de responder, a
lista de tabelas compartilhadas, a chave de API commitada, o formato da chave, a
dupla execução do `init.sql`, e a causa real do único teste vermelho do baseline.

---

## 3. Baseline (antes de qualquer alteração)

| Gate | Existe? | Resultado |
|---|---|---|
| unit/integration | sim (51 arquivos, um processo por arquivo) | **51/51 PASS** |
| lint | **não existe** | — |
| typecheck | **não existe** | — |
| E2E | **não existe** | — |
| security scan | **não existe** | — |
| mutation | **não existe** | — |
| build | sim (Docker) | não executado (sem daemon nesta máquina) |

O único `rc != 0` local foi `tests/test_hub.py`, e **não era um defeito do
produto**: `faulthandler` mostrou o bloqueio em
`app/routers/ai.py:11 import google.generativeai` → `grpc/__init__.py:2325`,
**36,2 s medidos isoladamente**. Com orçamento de 900 s o teste passa. Isso é
um achado real, mas de custo: 43 dos 51 arquivos de teste importam `app.main` e
pagam esse custo, o que domina os ~61 minutos de wall clock da suíte — e o pacote
é EOL, fixado com `>=`.

---

## 4. O que a auditoria encontrou

588 findings brutos (sobreposição deliberada entre agentes independentes):
**29 CRITICAL, 159 HIGH, 286 MEDIUM, 114 LOW** — 471 marcados CONFIRMED.
184 arquivos distintos com pelo menos um finding.

Detalhe completo em `FINDINGS.csv`; agrupamento causal em `ROOT_CAUSES.md`.
Os seis que mudam a avaliação de risco do sistema:

**1. Uma API key do CRM em texto claro, commitada e no histórico.**
`docs/n8n-toolHttpRequest-guia.md:180`, formato exato de `generate_api_key()`,
presente desde `7fd122b`. Chaves não expiram (`API_KEY_EXPIRY_DAYS` é lido em
`config.py` e usado em lugar nenhum) e valem nos **dois** serviços. Encontrada
por dois agentes independentes e re-verificada por mim.

**2. Mensagens de clientes são perdidas em silêncio.**
`webhook.py:130-149`: um `except Exception` envolve os três loops aninhados e
devolve **200 incondicional** à Meta. Uma falha numa mensagem descarta todas as
restantes do lote **e** diz à Meta que deu certo — a Meta nunca reenvia.

**3. A Bia não responde o primeiro contato de nenhum lead novo.**
O auto-reply de saudação grava um outbound *antes* do debounce ser agendado; o
lote pendente é calculado como "inbound mais novo que o último outbound", logo
volta vazio e a função retorna. Como o auto-reply "waiting" é deduplicado por
hora, a falha parece intermitente.

**4. Um envio sem credencial é marcado como entregue.**
Sem `META_ACCESS_TOKEN`, o cliente devolve `{"simulated": True}`, que vira
`ok=True` e é persistido como `status="sent"`. Um token rotacionado silencia o
canal inteiro enquanto o histórico parece normal. **Um teste existente exige esse
comportamento.**

**5. A superfície de ferramentas do LLM é ilimitada.**
`call_internal_api` aceita qualquer método em qualquer caminho `/api/`, e o guard
anti-SSRF é contornado por um `@` inicial (verificado). `run_select_query` não
tem allowlist de tabela e `crm_readonly` tem `SELECT` em tudo, incluindo
`users.hashed_password`. E `POST /api/auth/token` — que devolve uma key
permanente de privilégio total — é alcançável por esse caminho.

**6. n8n é um control plane público sem autenticação.**
Três webhooks abertos acionam agentes que carregam a API key do CRM e o token da
Meta. Um workflow que existe **só no export de produção** entrega método e URL
ao próprio LLM com a credencial anexada.

---

## 5. O que estava certo (registrado para não regredir)

A auditoria não encontrou só defeito. Estes controles foram verificados e estão
corretos — quem mexer perto deles precisa saber:

- HMAC do webhook Meta: verificado sobre o corpo **cru**, com `hmac.compare_digest`, **antes** de qualquer parsing, e **fail-closed** fora de development.
- Idempotência inbound: `messages.whatsapp_msg_id` é UNIQUE **no banco** — o SELECT em Python é só otimização; sob corrida real o perdedor aborta antes de enviar auto-reply.
- `service_window_open` (`conversas/app/models/conversation.py:13-36`): função pura, uma definição, normaliza naive→UTC para o split SQLite/Postgres. É o padrão que falta ao resto.
- `media_storage.resolve_local_file`: confinamento correto por `is_relative_to`; nomes de arquivo local são gerados pelo servidor.
- `app/query_filters.py`: **não é injetável** — compilado nos dois dialetos com `literal_binds`; `contains(..., autoescape=True)` trata metacaracteres LIKE.
- Download da IA (`app/routers/ai.py:269-272`): path traversal tratado corretamente — o buraco é só na **escrita**.
- Roteamento: nenhuma colisão de rota entre os 20 routers.
- `is_admin_role()` do Conversas, incluindo o `isinstance(role, Enum)` deliberado.
- Nenhuma injeção de SQL em código de aplicação fora da ferramenta de IA; nenhum `eval`/`new Function`; nenhuma desserialização insegura; nenhum command injection nos scripts.

---

## 6. Cobertura

| | |
|---|---|
| Arquivos no repositório | 347 |
| Excluídos (com motivo) | 2 — dois PNG binários, sem lógica |
| **No escopo** | **345** |
| **Revisados** | **345 (100,0%)** |
| **Linhas no escopo** | **62.293** |
| **Linhas revisadas** | **62.293 (100,0%)** |
| Arquivos com segunda passada independente | 43 |

Cada percentual é derivável dos ranges em `AUDIT_COVERAGE.csv`, e cada range foi
declarado explicitamente pelo agente que leu o arquivo (`path :: 1-N reviewed`).
Conferi os totais de linha contra o arquivo real.

**Honestidade sobre o que esse 100% significa:** significa que todo arquivo e
toda linha no escopo foram lidos e analisados por pelo menos um revisor, e os 43
de maior risco por dois. **Não** significa que todo defeito foi encontrado —
nenhuma auditoria pode afirmar isso. As lacunas conhecidas estão na seção 7.

---

## 7. Limitações — o que esta auditoria NÃO pôde verificar

Registrado explicitamente porque um relatório de auditoria que esconde suas
lacunas é pior que nenhum:

- **Nenhum acesso ao banco de produção.** Todo enunciado sobre o schema real vem das declarações de model e das migrations. Em particular: não sei qual serviço criou `users` em produção, nem se `crm_readonly` existe de fato (o `init.sql` não pode ter rodado limpo como está escrito), nem se a chave vazada ainda está ativa.
- **Nenhum acesso à instância n8n viva.** Os workflows foram auditados pelos JSON versionados e pelo export de 2026-07-08.
- **Traefik não está no repositório.** Se a borda remove `X-Forwarded-For` ou as headers `X-Internal-AI-*` é desconhecido; os findings assumem que não remove, que é o que este repositório mostra.
- **Nenhum teste em PostgreSQL.** Toda a suíte roda em SQLite. Divergências de dialeto — `lower()` ASCII-only, `FOR UPDATE` no-op, `TIMESTAMPTZ` aware vs naive, violação de UNIQUE — são estruturalmente invisíveis para ela e permanecem não verificadas empiricamente.
- **Nenhuma execução em navegador.** Os findings de frontend vêm de leitura de código. Onde dependem de runtime (bfcache, medição do FullCalendar, fallback de custom property CSS) estão marcados HIGH, não CONFIRMED.
- **Nenhuma corrida executada.** Os findings de concorrência são raciocínio estático sobre sequências check-then-act sem lock e sem constraint.
- **Correção de preço e política não avaliada.** Só consistência entre arquivos. Se 68.000 CLP é o preço certo do Valle de la Luna é uma pergunta para a operação.

---

## 8. O que a fase de IMPLEMENTAÇÃO descobriu (e a leitura não)

Registrado separado porque muda a leitura da seção 6: 100% de cobertura de
leitura não é 100% de descoberta, e estes são a prova disso. Todos apareceram
**depois**, ao verificar correções e ao não aceitar uma dispensa preguiçosa.

**`stage.id` não é uma chave inteira.** A auditoria dispensou
`onStageSearch('${stage.id}')` como "id interno". Ao conferir o modelo:
`StageSchema.id: str = Field(...)` — sem `pattern`, sem `max_length` — escolhido
pelo CLIENTE em `POST/PUT /api/pipeline/funnels` e guardado no JSON
`funnels.etapas`. O board interpolava esse valor em NOVE lugares, e em SEIS
deles **cru dentro de atributo** (`data-stage`, `id="cnt-…"`, `q-`, `per-`,
`body-`), onde uma aspa dupla fecha o atributo e injeta HTML direto.

**`allDestinos` não é vocabulário fechado.** `GET /api/leads/destinos` devolve
`sorted(set(DESTINOS_PRINCIPAIS) | {todo destino de todo lead})`, e destino de
lead é JSON livre escrito pelo n8n. Em `segmentacao.html` o TEXTO do chip já
passava por `esc()` e o valor no `onclick` do mesmo elemento não passava por
nada.

**O envenenamento da ORM foi verificado, não deduzido.** `conversas/app/seed.py`
grava `role="admin"`. Reproduzido nos dois sentidos num SQLite limpo: com
`'admin'` na coluna, `query(User)` do CRM levanta
`LookupError: 'admin' is not among the defined enum values`; com `'ADMIN'`,
devolve `UserRole.ADMIN`. Não é um usuário quebrado — é toda consulta do CRM que
retorne aquela linha.

**Uma flag de dados decidia o esquema de senha.** O login local do Conversas
(SHA-256 sem sal) era escolhido por `CONVERSAS_SEED_DEV_DATA`. Ligar essa flag em
produção para popular dados de demonstração trocaria, junto e sem aviso, toda a
autenticação do serviço.

**O CI arquivava um teste no job errado.** `test_conversas_security.py` era o
único teste do Conversas sem o literal `CONVERSAS_DIR`, que é o discriminador de
job — rodava no job do CRM e passava por acidente.

**Um `git stash` concorrente reverteu 49 arquivos.** Ver a observação de método
em `RELEASE_READINESS.md` §8: propriedade exclusiva de ARQUIVO não basta quando o
índice do git é global.

**Cinco testes não mediam comportamento.** Um terminava em `or True` (sempre
verdadeiro); dois fatiavam uma função em `[:1200]` e `[:600]` caracteres; um
exigia diff zero contra `origin/main` para sempre; um dependia da ausência de um
UNIQUE. E 16 chamadas `subprocess.run(text=True)` sem `encoding` — verdes no CI
Linux, vermelhas na máquina de quem escreve o código.

**A única autenticação do webhook nunca era exercitada.** 29 arquivos de teste
mencionam `META_APP_SECRET`; todos o definem vazio, para desligar a verificação.

---

## 8b. FASE 2 — o que a evidência externa mudou nesta auditoria (2026-08-25)

Os três workflows n8n **realmente em produção** foram fornecidos. Isso corrigiu
premissas que esta auditoria carregava desde o começo.

**A arquitetura n8n descrita nas seções acima estava desatualizada.** Em
produção existem **três** workflows, não seis. Não estão em produção:
**Notificador**, **Gerente Autônomo de Tarefas IA**, **Analista de Métricas**,
**Envio de Tarefas por Responsável** e **Notificação WhatsApp**. E existe um que
esta auditoria **nunca viu**: *Formulário do Site → CRM BnA*, com webhook público
próprio e escrita no CRM.

**Um finding CRITICAL desta auditoria estava errado sobre a produção.** "Um
webhook entrega método E URL à escolha de um LLM" descrevia o
`Gerente_Autonomo_de_Tarefas_IA`, que **não roda**. Nos três workflows atuais
toda URL de ferramenta é string fixa. O finding foi marcado OBSOLETE, junto de
outros dois do mesmo workflow, e um quarto virou FALSE_POSITIVE.

**A Bia melhorou muito e a auditoria não sabia.** O workflow passou de 7 para 14
nós, todos de defesa: portão para mensagem só de emoji, validação da saída contra
vazamento de termo interno, fallback de erro, base de conhecimento como
subworkflow. O `Agente Gerenciador de Leads`, ao contrário, **não mudou em nada
estrutural** — e é onde estão os dois CRITICAL desta fase.

**A separação entre "fila humana" e "notificar atendente" já está codificada no
produto.** O system message da Bia proíbe, em quinze formulações, afirmar que um
atendente foi notificado, e manda explicar que o atendimento é por ordem de
chegada. O nó `Tool Acionar Notificador` que sobrou no Gerenciador contradiz isso
e aponta para um workflow que não existe mais.

**E dois defeitos graves foram encontrados no trabalho da FASE 1 desta própria
auditoria:** o script de backup "corrigido" abortaria todo backup real (SIGPIPE +
`pipefail` invertendo a guarda), e a guarda anti-CR não funcionava fora do Linux.
O teste da Fase 1 não pegou porque verificava o **texto** do script e nunca o
**executava** — a mesma classe de defeito que esta auditoria mais denunciou.

Detalhe em `N8N_CURRENT_STATE_RECONCILIATION.md`, `POSTGRES_VALIDATION.md` e
`BACKUP_RESTORE_VALIDATION.md`.

---

## 9. Estado da estabilização

Ver `FULL_SYSTEM_STABILIZATION_PLAN.md` para as waves e
`RELEASE_READINESS.md` para os números finais, o que ficou aberto, e a lista
precisa de ações que **só o operador pode executar** — rotação da chave vazada,
purga do histórico, autenticação dos webhooks n8n, `NOSUPERUSER` no `crm_user`,
e as decisões de negócio pendentes na base de conhecimento da Bia.
