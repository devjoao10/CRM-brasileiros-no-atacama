# ROOT_CAUSES.md — causas estruturais por trás dos 588 findings

588 findings brutos (29 CRITICAL, 159 HIGH, 286 MEDIUM, 114 LOW) de 22 agentes
independentes, com sobreposição deliberada. Depois de agrupar, **14 causas raiz
explicam a grande maioria**. Corrigir o sintoma sem corrigir a causa reabre a
mesma classe de bug no próximo módulo — foi exatamente o que aconteceu com os
três incidentes que o próprio código documenta em comentário
(`app/limiter.py:2-11`, `app/auth.py:210-215`, `conversas/app/auth.py:121-149`):
cada um foi corrigido **no local do incidente**, nunca na fronteira compartilhada.

---

## ROOT-001 — Dois serviços, um banco, sem schema nem caminho de escrita compartilhado

**Descrição.** `conversas/` alcança tabelas que pertencem ao CRM de duas formas
incompatíveis: redeclarando um model ORM (`users`) e escrevendo SQL cru
(`leads`, `funnel_entries`, `lead_history`, `tags`, `lead_tags`). Não existe
definição de schema compartilhada nem função de escrita compartilhada, então
**todo invariante do CRM é reimplementado ou pulado do lado do Conversas**.

**Evidência.** `users` é a única tabela declarada pelos dois serviços (verificado:
29 tabelas no CRM, 15 no Conversas, uma colisão). As duas declarações divergem em
quatro colunas — `nome` 100 vs 200, `role` `Enum` vs `String(20)`, `api_key`
unique+index vs nenhum, e `email_verified` **existe só no CRM**, `NOT NULL` sem
`server_default`. Ambos chamam `create_all()` no startup sem ordenação entre si.

**Blast radius.** Em volume novo (DR, staging, ambiente novo) quem sobe primeiro
define a tabela. Se o Conversas ganhar, `users.email_verified` nunca existe e
**todo `db.query(User)` do CRM levanta `UndefinedColumn` — login incluído**.
Se o CRM ganhar, `conversas/app/seed.py` não consegue inserir (enum + NOT NULL).

**Arquivos.** `conversas/app/auth.py:17-30`, `conversas/app/seed.py:50-58`,
`conversas/app/services/crm.py:117-295`, `app/models/user.py:14-26`,
`app/main.py:64`, `conversas/app/main.py:26`, `migrations/` (nenhuma migration toca `users`).

**Findings associados.** ~35, incluindo 4 CRITICAL.

**Correção estrutural.** O CRM passa a ser o dono exclusivo do schema de `users`;
o Conversas para de declarar a tabela no seu `Base` e para de semear usuários.
A criação de lead ganha **uma** função compartilhada.

**Risco da correção.** Médio — mexe no bootstrap de ambos os serviços.

---

## ROOT-002 — Um `SECRET_KEY` para duas origens, com tratamento assimétrico e sem `aud`

**Descrição.** Os dois serviços assinam e validam o mesmo JWT HS256 com a mesma
chave, sobre a mesma tabela `users`, sem `typ`, `aud` ou `iss`. O CRM **falha ao
subir** sem a chave; o Conversas **cai silenciosamente** num literal commitado.

**Evidência.** `conversas/app/config.py:17` `os.getenv("SECRET_KEY", "dev-secret-key-change-me")`
vs `app/config.py:16-23` que levanta `RuntimeError`. `docker-compose.yml:57` usa
`${SECRET_KEY:?...}` para o CRM e `:115` usa `${SECRET_KEY}` puro para o Conversas.
`app/auth.py:64-75` valida apenas `sub` e `exp`.

**Blast radius.** Qualquer caminho de deploy que alcance o Conversas sem a
variável assina e aceita tokens com uma constante pública — **bypass total de
autenticação nos dois serviços**. Independentemente disso, o token
`type: verify_email` emitido em `app/routers/users.py:124` (entregue em query
string) é uma sessão CRM completa e válida.

**Findings associados.** ~12, incluindo 2 CRITICAL.

**Correção estrutural.** Guard fail-closed idêntico no Conversas, `:?` no compose,
e claim `typ` obrigatória validada no consumidor de sessão.

**Risco da correção.** Baixo.

---

## ROOT-003 — A sessão tem três decisores no cliente e dois no servidor

**Descrição.** Quem decide "estou logado?" são: (1) o servidor, via cookie;
(2) `layout.js`, via *presença* de uma chave no localStorage; (3) `Auth.apiRequest`,
anexando o token do localStorage como `Bearer` em toda chamada. E
`app/auth.py:179-195` **falha com 401 num Bearer inválido em vez de cair para o
cookie válido**.

**Blast radius.** Um token velho no localStorage envenena toda chamada de API
mesmo com cookie bom → logout forçado no meio da sessão, disparado inclusive
pelo poller de 60s. No Conversas, o portão de página checa só *presença* de
cookie — exatamente o defeito que o CRM já corrigiu (AUTH-LOOP-01) — e o logout
não apaga o cookie que ele mesmo criou.

**Sintoma mais grave.** Em `static/js/login.js:30-53`, todo caminho de falha
chama `clearAuth()` e cai no `return` da linha 51, **pulando o registro do
listener do formulário**. O `<form>` de `templates/login.html:68` não tem `action`
nem `method` — o submit vira `GET /login?email=...&password=...`, com a
**senha em texto claro na URL**, no histórico e no log de acesso.

**Findings associados.** ~18, incluindo 1 CRITICAL.

**Risco da correção.** Baixo a médio (mexe no fluxo de login).

---

## ROOT-004 — A superfície de ferramentas do LLM é ilimitada e confiável

**Descrição.** `call_internal_api` aceita **qualquer método contra qualquer
caminho** `/api/`, com um guard que só rejeita `startswith("http")` e `".."`.
`run_select_query` não tem allowlist de tabelas. Dois geradores de arquivo aceitam
um `filename` escolhido pelo modelo. Quatro ferramentas escrevem direto no ORM,
contrariando o comentário de política três linhas acima delas.

**Evidência verificada por mim.** `urlsplit("http://127.0.0.1:8000@evil.example.com/steal").hostname`
resolve para `evil.example.com` — o guard passa. `os.path.join(UPLOAD_DIR, "/etc/x")`
descarta a base. `SELECT email, hashed_password, api_key FROM users` passa em todos
os checks, e `crm_readonly` tem `SELECT` em todas as tabelas.

**Blast radius.** Texto de lead (que chega do WhatsApp via n8n) é lido pelo
`run_select_query` e devolvido ao modelo. Uma nota envenenada pode fazer a
Perpétua chamar `POST /api/auth/token` — que devolve uma **API key permanente,
de privilégio total, em texto claro** — e enviá-la para um host externo com as
headers HMAC internas junto.

**Findings associados.** ~25, incluindo 4 CRITICAL.

**Risco da correção.** Baixo (allowlists são aditivas).

---

## ROOT-005 — n8n é um control plane público, sem autenticação, segurando credenciais de produção

**Descrição.** Três webhooks públicos (`/webhook/agent-bia`,
`/webhook/gerenciador-leads`, `/webhook/notificacao`) sem nenhuma autenticação,
publicados pelo Traefik, acionam agentes LLM cujas tools carregam a API key do CRM
e o token da Meta. `N8N_RESTRICT_ENVIRONMENT_VARIABLES_ACCESS=false` no mesmo
container onde vive `META_ACCESS_TOKEN`.

**Agravante.** `Gerente_Autonomo_de_Tarefas_IA` existe **só no live export** (sem
fonte revisada no repo), tem `isArchived: false`, e seu nó `CRM API Tool` recebe
**método e URL do próprio LLM** com a credencial do CRM anexada.

**Findings associados.** ~20, incluindo 5 CRITICAL.

**Correção.** **Fora do meu alcance** — exige mudança na instância n8n viva e no
Traefik, ambos proibidos pelo escopo desta missão. Documentado como ação do operador.

---

## ROOT-006 — Erros são engolidos e falhas viram sucesso

**Descrição.** O padrão se repete em três camadas.

- **Webhook** (`conversas/app/routers/webhook.py:130-149`): um `except Exception`
  envolve os três loops aninhados e devolve **200 incondicional** à Meta. Uma
  falha em qualquer mensagem **descarta todas as restantes do lote** e diz à Meta
  que a entrega deu certo — a Meta nunca reenvia. **Perda permanente de mensagem de cliente.**
- **Outbound** (`conversas/app/services/whatsapp.py:108-111` + `outbound.py:48-50`):
  sem credenciais, o cliente devolve `{"simulated": True}`, que vira `ok=True` e
  é persistido como `status="sent"`. Um token da Meta expirado marca **toda
  mensagem como entregue enquanto nada é enviado**.
- **UI**: `btnCloseConv`/`btnToggleBot` mostram toast de sucesso sem olhar a resposta.

**Findings associados.** ~45.

**Risco da correção.** Médio — mexe no contrato com a Meta (o que a Meta reenvia).

---

## ROOT-007 — Check-then-act em todo lugar, sem constraint no banco por trás

**Descrição.** O padrão `SELECT ... if exists: 409 ... INSERT/UPDATE` aparece em
pelo menos 12 lugares, **sem lock de linha e sem UNIQUE correspondente**.

**Instâncias.** `claim`/`release`/`handoff`/`retry` de conversa; criação de
conversa por `whatsapp` (indexado, **não** unique); `FunnelEntry(lead_id, funnel_id)`;
`OperationalCardAssignee(card_id, user_id)`; `OperationalCardFieldValue(card_id, definition_id)`;
`tags.nome`/`teams.nome`/`segments.nome`/`funnels.nome`/`users.email` (esses *têm*
UNIQUE, então a corrida vira **500 em vez do 409 documentado**).

**Caso mais grave.** O envio outbound **não tem garantia de idempotência
nenhuma** — nem no banco nem em Python. O único UNIQUE (`messages.whatsapp_msg_id`)
é NULL exatamente no caso de falha (timeout depois de a Meta aceitar) que o botão
de retry então reenvia. **O cliente recebe a mesma mensagem duas vezes.**

**Findings associados.** ~30.

---

## ROOT-008 — Rate limiting e identidade do cliente quebrados atrás do proxy

`get_remote_address` lê o peer socket; o uvicorn sobe **sem `--proxy-headers`**;
o único ingresso é o Traefik. Resultado: **um único balde global**. Os 5/min do
login valem para a internet inteira somada — 5 requisições travam todos os
funcionários. O Conversas não tem limiter nenhum e **proxia todo login de produção
para o mesmo endpoint do CRM**, consumindo o mesmo balde.

**Findings associados.** ~6.

---

## ROOT-009 — O helper de escape é aplicado de forma inconsistente e está errado para o contexto em que mais é usado; e não há CSP

`esc()` está duplicado literalmente em 8 templates e **escapa aspas corretamente
para contexto de atributo** — mas é usado majoritariamente dentro de
`onclick="fn('${esc(v)}')"`, onde o parser HTML **decodifica `&#39;` de volta para
uma aspa real antes de o JS ser compilado**. A proteção não existe nesse contexto.

Somado a isso: `tarefas.html:437` injeta `JSON.stringify(t)` num atributo,
`relatorios.html` não tem helper nenhum, e o Conversas abre mídia de cliente como
`blob:` **herdando o Content-Type declarado pelo próprio cliente** — um WhatsApp
com mime `text/html` executa script na origem do inbox.

**Não existe `Content-Security-Policy` em lugar nenhum do repositório** — logo
todo sink vira roubo de sessão, porque o JWT também vive no localStorage.

**Findings associados.** ~20, incluindo 3 CRITICAL.

---

## ROOT-010 — A suíte roda em SQLite e uma parte grande dela afirma texto-fonte, não comportamento

**Descrição.** 51 arquivos, 50 passam, ~60 min de wall clock. Mas:
- **Todo** comportamento é validado em SQLite; o Postgres é coberto **só** por
  `assert "jsonb_each_text" in sql`. `FOR UPDATE` é no-op, `lower()` é ASCII-only,
  `TIMESTAMPTZ` volta aware em prod e naive em CI, violação de UNIQUE nunca acontece.
- Seis condições de **skip silencioso** (node ausente, `origin/main` ausente num
  clone raso — que é o default do job `crm` —, propriedade CSS ausente…).
- Testes **tautológicos** que constroem a própria query e afirmam sobre ela.
- Um `or True` que mata a asserção de XSS em `test_pipeline_inline_lead_edit.py:525`.
- Testes que **bloqueiam correções**: `test_conversas_service_window.py:588` exige
  `py.count("_require_open_window(conversation)") == 3` (adicionar uma quarta rota
  *correta* fica vermelho); `test_conversas_outbound_integrity.py:378` **exige**
  que `simulated` vire `status="sent"`; `test_conversas_service_window.py:418`
  ancora a janela de 24h num timestamp epoch `"1"`, **travando o bug de âncora**.

**Consequência.** A suíte é o único portão antes da produção e é cega exatamente
nos três eixos onde o sistema quebra: autenticação do webhook, concorrência em
Postgres e qualquer coisa que o navegador execute.

**Findings associados.** ~56.

---

## ROOT-011 — Evolução de schema sem ledger, sem transação e com dois donos concorrentes

`create_all()` (que nunca faz ALTER) convive com 10 scripts manuais sem registro
de execução. `m001` roda todo o DDL numa transação **engolindo cada exceção** —
no Postgres o primeiro erro aborta a transação, todo o resto falha em silêncio, o
commit vira ROLLBACK e o script **imprime OK**. `m009`/`m010` reportam
`uq_...:AUSENTE (verificar manualmente)` e **saem com 0**. As três migrations do
Conversas resolvem o alvo pelo config do Conversas, cujo `DATABASE_URL` **default
é um arquivo SQLite local** — rodar sem exportar a variável migra um arquivo
descartável e imprime sucesso.

**Findings associados.** ~20.

---

## ROOT-012 — O bootstrap do Postgres está quebrado e a aplicação roda como superusuário

`init.sql` é montado em `/docker-entrypoint-initdb.d/01-init.sql` **e** re-executado
por `02-hardening.sh`. O entrypoint roda o `.sql` primeiro, **sem** a variável de
senha; `CREATE USER` não tem `IF NOT EXISTS`. Numa inicialização limpa o container
**não termina de inicializar**. Além disso a senha é **duplamente aspada** (o shell
envolve em aspas e `:'VAR'` aspa de novo), então a senha real do `crm_readonly`
contém apóstrofos que a URL de conexão não tem.

E `POSTGRES_USER` é criado pela imagem como **SUPERUSER** — todo `CONNECTION LIMIT`
e todo `REVOKE` do init.sql são decorativos, e qualquer SQL injection alcança
`COPY ... TO PROGRAM`.

**Findings associados.** ~14, incluindo 2 CRITICAL.

**Correção.** Parcialmente fora do alcance: o volume `pgdata` já existe, então
editar o `init.sql` **não muda nada em produção**. Documentado como ação do operador.

---

## ROOT-013 — Documentação e exports de workflow derivaram do sistema, e um deles contém uma credencial viva

`docs/n8n-toolHttpRequest-guia.md:180` contém uma **API key do CRM em texto claro**,
no formato exato de `generate_api_key()`, commitada desde `7fd122b` e presente no
histórico. Confirmada independentemente por dois agentes e re-verificada por mim.

Junto disso: `docs/arquitetura_workflows_n8n.md` descreve dez workflows que não
existem; o checklist de segurança de produção marca como ABERTO um item já
corrigido; e o prompt live da Bia **removeu** as seções de LGPD, cancelamento,
reembolso e restrições de saúde/altitude que a versão no repo tem.

**Findings associados.** ~18, incluindo 1 CRITICAL.

---

## ROOT-014 — A base de conhecimento do agente contradiz a si mesma em preço, reembolso e segurança

`04_precos/regras_de_preco.md:25` manda a Bia **cotar** preços marcados
`[PENDENTE_VALIDACAO]`; o guardrail e o README da mesma pasta mandam **recusar e
escalar**. São 43 preços 2026 não validados. O exemplo few-shot canônico
hard-codeia um desses preços. A regra de altitude para menores de 7 anos aparece
de **três formas incompatíveis** em três arquivos — proibição absoluta, "não
recomendado", e "não recomendado mas permitido em privativo".

**Consequência.** Preço errado e orientação de saúde errada dadas a clientes
pagantes, decididas por qual chunk o RAG recuperar.

**Findings associados.** ~30.

**Correção.** Exige **decisão de negócio** (qual preço vale, qual regra de
altitude vale) — não é inferível do código. Documentado como bloqueio de produto.

---

## Mapa causa → escopo de correção

| Causa | Corrigível no repo por mim | Exige ação do operador / decisão de negócio |
|---|---|---|
| ROOT-001 | schema owner, seed, `dados` faltante, normalização de telefone | migration de dados em produção |
| ROOT-002 | guard fail-closed, `:?`, claim `typ` | rotação da chave |
| ROOT-003 | fallthrough Bearer→cookie, wiring do login, portão do Conversas, logout | — |
| ROOT-004 | allowlists, sanitização de path, denylist de tabela | revogar grants no Postgres |
| ROOT-005 | — | **tudo** (n8n vivo + Traefik) |
| ROOT-006 | try por mensagem, `simulated`, ordem auto-reply/debounce | — |
| ROOT-007 | UNIQUEs + migration, claim atômico, chave de idempotência | dedupe de dados existentes |
| ROOT-008 | `--proxy-headers` | confirmar o CIDR do Traefik |
| ROOT-009 | sinks de XSS, CSP, mime de mídia | — |
| ROOT-010 | destravar os testes que bloqueiam correções, regressões novas | job Postgres no CI |
| ROOT-011 | ledger, transação, guard de dialeto | rodar as migrations |
| ROOT-012 | corrigir os scripts | re-executar hardening / `NOSUPERUSER` |
| ROOT-013 | remover o literal + guard | **rotacionar a chave, purgar o histórico** |
| ROOT-014 | — | **decisão de negócio** |


---

## FASE 2 (2026-08-25) — causas raiz acrescentadas pela evidência externa

**ROOT-015 — o repositório não é fonte de verdade sobre o n8n.**
Os workflows versionados em `n8n/workflows/` divergem do que roda. Três dos seis
snapshots não estão em produção, e um workflow de produção não estava versionado.
Toda conclusão da Fase 1 sobre n8n herdou esse erro. Findings que dependem dela:
F-019 a F-026, e a descrição inteira da arquitetura de automação.
*Correção estrutural:* versionar o export a cada alteração, ou aceitar que
qualquer auditoria de n8n exige export fresco como entrada.

**ROOT-016 — a suíte roda num dialeto e a produção em outro, e a diferença não é
só de sintaxe.** Não é apenas "faltam testes em PostgreSQL": há código que a
suíte **não pode executar** (`NOW()`, `::jsonb` em `crm.py`), classificação de
erro que muda de classe entre dialetos e decide se a Meta reentrega uma mensagem,
e defaults de ordenação **opostos**. Três dos nove achados dessa família eram
falhas silenciosas em produção com a suíte verde.
*Correção estrutural:* um PostgreSQL no CI. Enquanto não houver, toda afirmação
de cobertura sobre esses caminhos é sobre o SQLite, não sobre o produto.

**ROOT-017 — verificação por grep chamada de teste.**
`test_filter_normalization_and_backup.py` afirmava a integridade do backup
conferindo se a string `"gzip -t"` existia no arquivo. O script, quando
executado, abortava todo backup real. Esta causa é a mesma de F-172, F-173,
F-182, F-184 e F-185 — e desta vez ela produziu um defeito no trabalho da própria
auditoria. *Correção estrutural:* um teste que não executa o artefato não pode
afirmar nada sobre o comportamento dele.

**ROOT-018 — string vazia e null tratados como sinônimos numa API com dois
consumidores que os usam com sentidos opostos.** O n8n manda `""` para "não
coletei"; a interface manda `null` para "limpe". `LeadUpdate` colapsava os dois em
`None`, e `exclude_unset` não podia mais distinguir. Resultado: 422 no `nome`,
apagamento silencioso de `whatsapp` e `destinos`. *Correção estrutural:* quando
dois clientes falam a mesma API, a semântica de "ausente" tem de ser explícita no
contrato, não emergente do validador.
