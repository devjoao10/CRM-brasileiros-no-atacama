# RELEASE_READINESS.md

Estado do sistema BnA ao fim da auditoria + estabilização global.

**Branch:** `audit/full-system-stabilization-2026-08-24`
**Base:** `d4831486b767988ed2b91518167d8c50fbeb636e` (HEAD de `main`)
**Commits:** 16 · `124 files changed, 11074 insertions(+), 796 deletions(-)`

> **Nenhum deploy foi feito. Nenhum dado de produção foi tocado. Nenhuma
> migration foi executada. Nenhum merge foi feito.** Este documento existe para
> que a decisão de liberar seja de quem tem autoridade para tomá-la, com os
> números reais na mão.

---

## 1. O veredito, em uma frase

O repositório está em condição **melhor e mensurável** — 0 CRITICAL fixáveis em
código continuam abertos, a suíte inteira passa, e cada correção tem teste —
mas **três coisas que só um operador pode fazer continuam pendentes, e uma delas
é uma credencial viva exposta**. Enquanto elas não forem feitas, liberar não é
uma decisão técnica de código.

O veredito formal está na seção 9.

---

## 2. Gates — antes e depois

| Gate | Baseline (antes) | Agora |
|---|---|---|
| Suíte (um processo por arquivo, como o CI) | **51/51 PASS** (51 arquivos) | **63/63 PASS** (63 arquivos) |
| Arquivos de teste | 51 | 63 (**+12**) |
| Lint | não existe | não existe |
| Typecheck | não existe | não existe |
| E2E / navegador | não existe | não existe |
| Security scan | não existe | não existe |
| Mutation | não existe | não existe |
| Build Docker | não executado (sem daemon nesta máquina) | não executado (mesma limitação) |
| Portão de sintaxe (py + Jinja2 + JSON + NUL) | não existia | limpo — 389 arquivos |

O baseline de 51/51 foi **corrigido durante a auditoria**: `tests/test_hub.py`
aparecia vermelho por um timeout de 300 s meu contra um `import
google.generativeai` de **36,2 s medidos**, não por defeito do produto. Com
orçamento de 900 s ele passa. Registrar um vermelho falso teria contaminado toda
a comparação seguinte.

---

## 3. Findings — estado depois da reauditoria

588 findings brutos. Estado atual:

| Estado | Qtd | O que significa |
|---|---:|---|
| **RESOLVED** | 64 | corrigido **e** com teste de regressão nomeado |
| **BLOCKED_OPERATOR** | 62 | correção fora do repositório (produção, n8n, Traefik, banco, histórico git) |
| **ADDRESSED_UNVERIFIED** | 151 | a região de código mudou, mas **não afirmo** que aquele defeito específico acabou |
| **OPEN** | 311 | não tocado |
| **FALSE_POSITIVE** | 0 | (o único derrubado foi absorvido pela regra de região) |

Por severidade, o que **não** está resolvido nem bloqueado:

| Severidade | Não resolvidos | Total |
|---|---:|---:|
| **CRITICAL** | **0** | 29 |
| HIGH | 117 | 159 |
| MEDIUM | 247 | 286 |
| LOW | 98 | 114 |

**Como esses estados foram atribuídos — leia antes de citar o número.**
Não houve revisão manual dos 588, e dizer que houve seria mentira. A regra é
mecânica, reproduzível e está escrita no cabeçalho de `adjudicar.py`:

- a "região que mudou" é calculada nas linhas do **lado antigo** do diff contra
  o merge-base — a mesma numeração usada quando os findings foram escritos;
- `RESOLVED` exige região alterada **e** arquivo coberto por teste desta
  auditoria, **ou** um override explícito meu, com a razão e o teste gravados na
  coluna `adjudicacao` do `FINDINGS.csv`;
- `ADDRESSED_UNVERIFIED` é deliberadamente conservador: quer dizer "o código
  daquele ponto não é mais o mesmo", não "está consertado".

Os 29 CRITICAL foram tratados **um a um**, com override nomeado. Os 22 que
constam como `BLOCKED_OPERATOR` estão na seção 4.

---

## 4. O que só o operador pode fazer

Em ordem de urgência. Nenhum item abaixo foi executado por esta missão.

### 4.1 — URGENTE: uma API key do CRM está viva e exposta

`docs/n8n-toolHttpRequest-guia.md:180` continha uma chave de API do CRM em texto
claro, no formato exato de `generate_api_key()`, commitada desde `7fd122b`. O
arquivo foi corrigido nesta branch.

**Isso não contém o vazamento.** A chave continua válida e continua no histórico
do git. Chaves **não expiram**: `API_KEY_EXPIRY_DAYS` é lido em `config.py` e
não é usado em lugar nenhum. Qualquer `bna_...` válido autentica em **todas** as
rotas `/api/*` do CRM **e** do Conversas, porque os dois compartilham a tabela
`users`.

Ações, nesta ordem:
1. **Rotacionar** a chave (revogar a atual, emitir outra, atualizar as
   credenciais do n8n que a usam).
2. **Purgar o histórico** (`git filter-repo` ou equivalente) e forçar a
   reescrita nos clones.
3. Implementar de fato o `API_KEY_EXPIRY_DAYS`, ou remover a variável — hoje ela
   promete uma proteção que não existe.

### 4.2 — n8n é um control plane público sem autenticação

Três webhooks `POST` **abertos à internet, sem autenticação nenhuma**, acionam
agentes que carregam a API key do CRM e o token da Meta. Oito findings CRITICAL
vivem aqui (F-019, F-021 a F-026).

- `Agente_Gerenciador_de_Leads_BnA.json` expõe a superfície de **escrita** de 14
  ferramentas do CRM a quem chamar o webhook.
- `Gerente_Autonomo_de_Tarefas_IA.json` entrega **o método HTTP e a URL inteira**
  à decisão do LLM (`$fromAI`), com a credencial anexada pelo nó — e o prompt do
  agente é montado a partir de `titulo`/`descricao` de tarefa, que o outro agente
  escreve. É uma cadeia de injeção de prompt terminando em requisição
  autenticada arbitrária.
- `WF-01_Agente_Bia.json` não tem **nenhuma** defesa de prompt injection, e o
  parâmetro `whatsapp` da ferramenta é string livre escolhida pelo LLM, sem
  allowlist.

Ações:
1. Autenticar os três webhooks (header secreto no mínimo; idealmente HMAC).
2. **Arquivar `Gerente_Autonomo_de_Tarefas_IA`** — ele existe **somente** no
   export de produção `live_exports/20260708_1443/`, não está versionado como
   workflow mantido, e é o de maior poder. Se ainda estiver ativo, desligar.
3. Substituir método+URL vindos do LLM por ferramentas de escopo fixo.

> Os arquivos em `n8n/workflows/live_exports/` **não foram editados de
> propósito**: são um snapshot do que rodava em 2026-07-08. Alterá-los não muda
> nada na instância viva e adulteraria um registro histórico.

### 4.3 — Banco: privilégio e allowlist

- `docker-compose.yml:53` — a aplicação conecta como `POSTGRES_USER`, que a
  imagem do postgres cria como **SUPERUSER**. Trocar por um papel restrito exige
  **criar o papel e migrar privilégios no banco**: operação de produção. Mudar só
  o compose derrubaria a aplicação no próximo restart, com um usuário inexistente.
- `docker/postgres/init.sql:21` concede `SELECT ON ALL TABLES` a `crm_readonly`,
  incluindo `users.hashed_password` e as API keys. A denylist que esta auditoria
  colocou em `run_select_query` é camada de **aplicação**; o que fecha de fato é
  revogar o `SELECT` no banco.
- `docker/postgres/init-hardening.sh` só roda em volume novo — em produção ele
  **nunca rodou**. Precisa ser aplicado à mão.
- `DATABASE_READONLY_URL` precisa apontar para um DSN `crm_readonly` real. Sem
  isso, a ferramenta SQL da IA fica **desligada** em produção — fail-closed
  deliberado desta auditoria (`app/services/ai_tools.py`, F5).

### 4.4 — Backup

`scripts/backup_postgres.sh` estava corrompendo **todo** dump: `docker exec -t`
aloca um pseudo-TTY, cujo ONLCR traduz LF→CRLF **antes** do gzip, e o formato
plain emite os dados como blocos `COPY ... FROM stdin`, onde a linha termina o
registro — cada valor da última coluna ganhava um `\r`. O restore funciona e
suja os dados em silêncio.

O script foi corrigido e ganhou guarda de regressão. O que falta ao operador:
1. **Considerar todo backup existente suspeito** e conferir um restore num
   ambiente descartável.
2. Não há **upload offsite** — o backup mora no mesmo host do volume do banco.
3. Não há **agendamento versionado** (o cron do cabeçalho é comentário).
4. Um backup nunca restaurado não é um backup: estabelecer teste periódico.

### 4.5 — Consequência operacional das correções de sessão

Tokens emitidos antes desta branch **não têm o claim `typ`** e passam a ser
recusados. **Todo usuário logado fará um re-login.** É deliberado: aceitar `typ`
ausente manteria aberto o bypass em que o token de verificação de e-mail valia
como sessão.

### 4.6 — Decisões de negócio, não de engenharia

Preço, prazo de reembolso e regra de altitude para menores de 7 anos aparecem
inconsistentes entre a base de conhecimento da Bia e a documentação. A auditoria
verificou **consistência entre arquivos**, não correção factual. Se 68.000 CLP é
o preço certo do Valle de la Luna é pergunta para a operação.

---

## 5. O que mudou de fato no código

Agrupado pelo que o usuário final sente.

**Perda de dados parou.**
Um `except Exception` envolvia os três laços do webhook e devolvia **200
incondicional** à Meta: uma falha numa mensagem descartava as irmãs do lote *e*
dizia à Meta que deu certo — e a Meta nunca reenvia. Mensagem de cliente perdida,
sem rastro.

**A Bia voltou a responder o primeiro contato.**
A saudação automática gravava um outbound **antes** de o debounce ser agendado; o
lote pendente é "inbound mais novo que o último outbound", então voltava vazio.
Como o auto-reply é deduplicado por hora, a falha parecia intermitente.

**Envio sem credencial deixou de parecer entrega.**
`{"simulated": True}` virava `status="sent"`. Agora existe `status='simulated'`
e, fora de development, a ausência de credencial **falha alto**.

**Cliente errado deixou de receber dados de outro cliente.**
O lookup de lead casava pelos 10 últimos dígitos do telefone e o primeiro
resultado vencia. Em número brasileiro isso não identifica ninguém:
`5511987654321` e `5521987654321` terminam iguais. Como o `lead_id` fica gravado
**permanentemente** na conversa e as variáveis de template saem dele, um
casamento errado manda nome e e-mail do cliente B para o cliente A dentro de um
template aprovado.

**XSS armazenado fechado, e virou regra.**
`esc()` produz `&#39;`, o que é correto em conteúdo e em atributo — e **inútil
dentro de `on*`**, porque o parser decodifica a entidade antes de o JavaScript
compilar. Nomes de lead vêm do webhook do WhatsApp e do n8n, sem passar por
operador. Oito lugares tinham o padrão. Além deles, a verificação descobriu que
`stage.id` **não é uma chave inteira**: é `StageSchema.id: str` sem validação,
escolhido pelo cliente, interpolado **cru em seis atributos**; e `allDestinos`
agrega o campo `destinos` de todo lead. `tests/test_frontend_injection_contract.py`
transforma isso em regra que vale para arquivos que ainda não existem.

**O link de verificação de e-mail deixou de ser uma sessão do Conversas.**
Achado na **reauditoria**, não na leitura: os dois serviços assinam com a mesma
`SECRET_KEY`, e `app/routers/users.py` emite um token de verificação de e-mail
que viaja na **query string** de um link — logo, em log de acesso, histórico e
`Referer`. O CRM passou a exigir `typ: "access"` (W1-A); o `decode_token` do
Conversas não olhava propósito nenhum. Enquanto isso valeu, **aquele link era
uma sessão válida do Conversas** — o inbox de WhatsApp inteiro, todas as
conversas de todos os clientes. Cada wave olhou só o seu serviço; o buraco vivia
exatamente no meio.

**Sessão parou de ser forjável.**
O Conversas tinha `SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-me")`
**versionado**. Como os dois serviços validam com a mesma chave e compartilham
`users`, qualquer pessoa com acesso ao repo assinava `{"sub": "<email de admin>"}`
e era admin **nos dois**.

**A linha que envenenava a ORM do CRM.**
`conversas/app/seed.py` gravava `role="admin"` minúsculo; o CRM persiste o **nome
do membro** do enum ("ADMIN"). Reproduzido nos dois sentidos: com `'admin'`,
`query(User)` levanta `LookupError` em **toda** consulta que retorne aquela linha.

**Filtro que nunca encontrava.**
`_ESPACOS` afirmava, em comentário, ser "o mesmo conjunto que `str.strip()`
remove". Não era: faltavam NBSP, `\x1c-\x1f`, `\x85` e o bloco U+2000. Uma chave
colada do Excel/Word/WhatsApp com NBSP na borda ficava **permanentemente**
impossível de filtrar, sem erro nenhum.

**Um NUL derrubava a listagem inteira.**
`campos_personalizados` é `Column(JSON)` (o tipo `json` do Postgres aceita NUL),
mas `query_filters` faz `cast(coluna, JSONB)` linha a linha — e `jsonb` **rejeita**
NUL. Uma única linha envenenada dava 500 na listagem de leads e em todo segmento
com campo personalizado, para todos os usuários.

---

## 6. Qualidade da suíte — o que foi corrigido nela

Cinco testes **afirmavam o defeito** ou não afirmavam nada. Nenhum foi removido;
todos foram reancorados no comportamento, e cada mudança está justificada no
commit.

| Teste | O que havia | O que passou a afirmar |
|---|---|---|
| `test_conversas_outbound_integrity.py` | exigia `status == "sent"` para envio simulado | exige `'simulated'` e que nunca seja lido como entregue |
| `test_conversas_service_window.py` | fixture com `timestamp: "1"` (epoch 1970) | epoch real **+ 3 checks novos** para a âncora da Meta |
| `test_pipeline_inline_lead_edit.py` | asserção terminada em `or True` (sempre verdadeira) | escrita de HTML no card só via `renderLeadCard` — verificada quebrando o template |
| `test_pipeline_inline_lead_edit.py` | exigia diff **zero** contra `origin/main` em 4 arquivos | o **contrato** que o editor consome (rotas + campos do schema) |
| `test_conversas_mobile_pwa.py` / `test_conversas_notifications.py` | fatiavam `loadChat` em `[:1200]` / `[:600]` | leem o corpo da função contando chaves |
| `test_conversas_media_storage.py` | criava N conversas com o mesmo número | reutiliza a conversa, como a aplicação faz |

Mais três defeitos estruturais da suíte:

1. **`test_conversas_security.py` rodava no job errado do CI.** Era o único teste
   do Conversas sem o literal `CONVERSAS_DIR`, que é o discriminador de job.
2. **O guard de "seleção vazia" do CI era código morto**: `run:` roda com
   `bash -e` e `grep -L` sai 1 sem casamento, então o step morria na atribuição,
   antes do guard.
3. **16 chamadas `subprocess.run(text=True)` sem `encoding`.** Sem ele a
   decodificação usa o codec da plataforma — cp1252 no Windows. Verde no CI
   (Linux), vermelho na máquina de quem escreve o código.

E a lacuna mais grave: **a única autenticação do webhook Meta nunca era
exercitada.** 29 arquivos de teste mencionam `META_APP_SECRET`; todos o definem
vazio, justamente para desligar a verificação. `tests/test_conversas_webhook_signature.py`
cobre isso com o segredo ligado, incluindo o check que prova que a assinatura
cobre os **bytes** e não o JSON.

---

## 6a. Cobertura da REAUDITORIA

A auditoria perguntava "todo arquivo do escopo foi lido?" — 345/345, registrado
em `AUDIT_COVERAGE.csv`. A reauditoria pergunta outra coisa: **todo arquivo que
MUDOU foi conferido, e por quem?** A resposta está em `REAUDIT_COVERAGE.csv`,
linha a linha.

Dos 124 arquivos alterados, 33 são artefatos desta própria auditoria
(`docs/audit/*`, escritos por mim). Sobram **91 arquivos de código**:

| Evidência | Arquivos | % |
|---|---:|---:|
| Diff lido por mim, linha a linha | 72 | 79% |
| Coberto por teste desta auditoria | 61 | 67% |
| **Ambos** | 42 | 46% |
| **Ao menos uma das duas** | **91** | **100%** |

As duas colunas são deliberadamente separadas porque valem coisas diferentes: um
teste prova comportamento e não vê intenção; uma leitura vê intenção e não prova
nada em runtime. A coluna de leitura é uma **lista curada, escrita à mão** —
inferi-la de heurística seria exatamente a cobertura fictícia que esta missão
proíbe.

A primeira geração dessa planilha acusou 17 arquivos de código sem evidência
nenhuma. Eles foram lidos na segunda passada, e é por isso que a linha final é
100% — não porque a régua foi afrouxada.

**Achados dessa segunda passada** (a leitura pagou o custo dela):

- O buraco do token de verificação de e-mail (§5) foi encontrado lendo o diff de
  `app/auth.py` e perguntando o que acontece do outro lado da chave compartilhada.
- O guard anti-SSRF foi **sondado**, não aceito: 13 tentativas de bypass —
  `@`-prefixo (a original), `//`, `%2e%2e`, `\`, fragmento com `#@host`, esquema
  absoluto — todas recusadas; caminhos legítimos passam.
- A afirmação de W1-E de que `--forwarded-allow-ips=*` é seguro foi **conferida**
  contra o `docker-compose.yml`: nenhum serviço publica `ports:`, todos usam
  `expose:`, então de fato não há caminho até eles que não passe pelo Traefik.
- Os `async def` trocados por `def` em analytics/tags foram verificados quanto a
  chamadas internas com `await` — não há nenhuma; são apenas route handlers.

---

## 6b. Impacto estrutural

O baseline do Graphify (3.182 nós / 7.148 arestas) **não pôde ser refeito**: o
ambiente Python do graphify não está disponível nesta sessão, e reinstalá-lo
trocaria uma medida por outra sem ganho. No lugar, o risco que aquele grafo
existia para vigiar foi medido diretamente por AST, comparando o merge-base com
`HEAD` (`docs/audit/impacto_estrutural.txt`, reproduzível):

| | Antes | Depois |
|---|---:|---:|
| Módulos internos | 129 | 130 (`+1` — a migration `m011`) |
| Arestas de import interno | 343 | 349 (**+6**) |
| Arestas removidas | — | **0** |
| Violações de fronteira entre os dois serviços | 0 | **0** |

As seis arestas novas são todas explicáveis e desejáveis:

- `app.routers.ai → app.limiter` — havia **dois** limitadores e o do router não
  compartilhava contagem com o resto do app.
- `app.routers.auth → app.config` — parou de ler env cru (era o que fazia
  `ENVIRONMENT="Production"` emitir cookie sem `Secure`).
- `app.routers.tasks → app.models.lead` — `lead_id` inexistente batia na FK e
  virava 500 com transação abortada.
- `app.schemas.user → app.models.user` — o enum passou a vir do dono.
- `conversas.app.routers.pages → conversas.app.{auth,database}` — o portão de
  página passou a **validar** o token, não só constatar que o cookie existe.

Nenhum módulo de alto blast radius cresceu mais que `+1` de in-degree
(`app.database` 41, `+0`; `app.auth` 22, `+0`). Não houve deriva arquitetural, e
**os dois serviços continuam sem se importar** — a fronteira que a auditoria
mapeou segue de pé.

---

## 7. Limitações — o que esta entrega NÃO prova

- **Nenhum acesso ao banco de produção.** Todo enunciado sobre o schema real vem
  de model e migration. Não sei qual serviço criou `users` em produção, se
  `crm_readonly` existe, nem se a chave vazada ainda está ativa.
- **Nenhum teste em PostgreSQL.** A suíte inteira roda em SQLite. Divergências de
  dialeto — `lower()` ASCII-only, `FOR UPDATE` no-op, `TIMESTAMPTZ` aware vs
  naive, violação de UNIQUE — são **estruturalmente invisíveis** para ela.
- **Cross-browser não foi executado.** Não existe Playwright nem qualquer runner
  de navegador no repositório, e instalá-lo seria funcionalidade nova fora do
  escopo. As correções de front foram verificadas por leitura e, onde possível,
  **executando o JavaScript sob `node` com stub de DOM** — o que cobre a lógica
  de escape, não o comportamento de renderização de cada engine.
- **Nenhuma corrida executada.** Os findings de concorrência continuam sendo
  raciocínio estático sobre sequências check-then-act sem lock.
- **Build Docker não executado** (sem daemon nesta máquina). As mudanças de
  `Dockerfile` foram revisadas, não construídas.
- **`ADDRESSED_UNVERIFIED` não é "corrigido".** São 151 findings cujo código
  mudou sem teste desta auditoria apontando para o arquivo.
- **A instância n8n viva não foi vista.** Os workflows foram auditados pelos JSON
  versionados e pelo export de 2026-07-08.
- **O grafo do Graphify não foi refeito** (§6b). A comparação estrutural existe,
  mas é uma medida minha por AST, não a mesma métrica do baseline.

---

## 8. Observação sobre o método (para a próxima vez)

A fase de implementação rodou com múltiplos agentes editando **a mesma árvore de
trabalho** com propriedade exclusiva de arquivo. A propriedade de *arquivo*
funcionou; o **índice do git**, não. Durante a execução um agente rodou
`git stash` sobre a árvore inteira, revertendo 49 arquivos de outros agentes, e
um `git reset` concorrente esvaziou o índice entre um `git add` e o `git commit`
seguinte — dois arquivos meus ficaram de fora do commit em que deveriam estar.

Nada foi perdido (o stash é durável, e a reconciliação foi feita comparando cada
arquivo contra o stash e contra a base), mas a lição é concreta: **propriedade
exclusiva de arquivo não basta quando o índice do git é global.** Da próxima vez,
uma worktree por agente, ou nenhum comando de git nas mãos deles.

---

## 9. Veredito

**NOT READY FOR RELEASE VALIDATION**

Justificativa técnica, sem rodeios:

1. **Uma credencial de produção continua viva e exposta** (§4.1). A chave
   autentica em todas as rotas `/api/*` dos dois serviços, não expira, e está no
   histórico do git. Remover a linha do arquivo — que foi o que esta missão pôde
   fazer — **não revoga nada**. Enquanto não houver rotação, qualquer validação
   de release estaria validando um sistema com acesso administrativo distribuído
   publicamente.
2. **Três webhooks n8n públicos e sem autenticação continuam operando** (§4.2),
   um deles entregando método e URL à escolha de um LLM alimentado por texto de
   cliente. Isso não é alcançável pelo repositório.
3. **Todo backup existente deve ser considerado corrompido** (§4.4) e nenhum
   restore foi verificado. Liberar sem um backup confiável é apostar que nada
   dará errado.
4. **A suíte inteira roda em SQLite** (§7) e o alvo é PostgreSQL. Os UNIQUE
   novos, as diferenças de `lower()` e o comportamento de `TIMESTAMPTZ` não têm
   verificação empírica no dialeto real.

O que **não** motiva o "NOT READY": o código. Os 63 arquivos de teste passam, os
29 findings CRITICAL estão resolvidos com teste ou bloqueados por serem externos
ao repositório, e cada correção carrega a explicação do defeito que a motivou.

**O caminho para READY** é curto e não depende de mais programação:
rotacionar a chave e purgar o histórico (§4.1) · autenticar ou desligar os
webhooks n8n (§4.2) · executar `migrations/m011` num ambiente de teste com cópia
do dado real e conferir se há duplicatas a reconciliar · rodar a suíte contra
PostgreSQL · fazer um backup com o script corrigido e **restaurá-lo** num
ambiente descartável.

Feitos esses cinco, o veredito muda — e a mudança deve ser registrada aqui, com
a evidência de cada um.

---

## 10. Confirmações finais

- **NÃO houve merge automático.** A branch `audit/full-system-stabilization-2026-08-24`
  está local, sem PR aberto e sem merge em branch protegida.
- **NÃO houve deploy.** Nenhum `deploy.yml` foi disparado, nenhum acesso à VPS,
  nenhum container reiniciado, nenhum `git pull` em produção.
- **NÃO houve alteração de dados de produção**, nem execução de migration em
  produção, nem exclusão de dado real.
- **NÃO houve alteração de infraestrutura externa** (Traefik, n8n, Meta, VPS).
- **NENHUM segredo foi exposto ou documentado.** A chave encontrada foi
  substituída por placeholder; seu valor não aparece em nenhum artefato desta
  auditoria.
- **NENHUM teste foi removido** para ficar verde, **nenhum lint foi silenciado**,
  **nenhuma segurança foi reduzida** para fazer teste passar.
