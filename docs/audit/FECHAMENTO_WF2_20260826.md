# FECHAMENTO — Reconciliação, correção dos pendentes e veredito

Branch `audit/full-system-stabilization-2026-08-24`. Data: 2026-08-26.
Este documento fecha a missão. Não repete o inventário — ele vive em
`MASTER_FUNCTIONAL_BUG_MATRIX.md` — e não repete as instruções de n8n, que vivem
em `N8N_MANUAL_CHANGES.md`. Aqui está o que foi reconciliado, o que sobrou, e
por quê.

---

## 1. Escopo e método

A rodada anterior fechou com 110 sintomas classificados. Esta rodada **não**
abriu uma auditoria nova. Fez três coisas:

1. **Reconciliou** cada pendência contra o código atual — o código é a fonte de
   verdade, não o relatório anterior.
2. **Rodou cinco revisores adversariais** sobre o branch inteiro, um por eixo:
   correção funcional, dados/concorrência/PostgreSQL, segurança,
   FastAPI/operação, e o contrato de funil. Cada um recebeu instrução explícita
   de **tentar quebrar**, e de descartar qualquer achado para o qual não
   conseguisse escrever um cenário de falha concreto.
3. **Corrigiu** tudo o que era real, reproduzível, corrigível localmente,
   seguro, não dependente de decisão humana e não dependente de produção.

Validação com PostgreSQL 16.14 real, em container descartável de auditoria
(`bna-postgres-audit`, porta 55432). Nenhum outro banco foi tocado. Nenhum
acesso a produção, VPS, n8n ou banco de produção. Nenhum push, PR, merge ou
migration em produção.

---

## 2. O que a reconciliação encontrou

O relatório anterior tinha três problemas de integridade, todos corrigidos:

- **Dois bytes NUL literais** — um em `MASTER_FUNCTIONAL_BUG_MATRIX.md` e um em
  `RELEASE_READINESS.md`. Vieram da ferramenta que escreveu a linha do F-043 (o
  bug é sobre a *sequência* `\u0000`, e ela virou o byte de verdade no arquivo).
  O git passou a tratar os dois como binários: sem diff, sem grep, sem review.
- **O M6 continuava vivo na matriz.** Três linhas (W2-05, W2-06, W5-05)
  atribuíam a ele a causa em produção. O M6 não procedia — era erro meu de
  leitura do export, já corrigido nos outros dois documentos. As três passaram a
  `RESOLVED`.
- **F-108 estava `RESOLVED` citando a linha errada.** A adjudicação apontava
  para a igualdade de dígitos em Python, que nunca foi o furo; o furo era o
  pré-filtro `LIKE` sobre a coluna crua. O `RESOLVED` se sustenta, mas pela
  evidência certa — sem isso a próxima reauditoria repetiria a validação pela
  metade.

Os outros dois itens que a missão pediu para conferir (**"109 classificados" vs
110 categorias**, e o histórico do smoke **17 → 19 → 22**) já estavam
consistentes: a matriz tinha 110 linhas com 110 status, e
`POSTGRES_VALIDATION.md` registrava 22/22.

---

## 3. Os quatro cartões "Tarefa sugerida" do Claude App

Investigados sem criar worktree separado, como pedido.

| Cartão | Veredito |
|---|---|
| `ai_tools.py create_lead missing funnel entry` | Já estava corrigido (`b5b2abd`): `create_lead` delega para `criar_lead(..., origem="ia")`, que garante a entrada no funil. Reconciliado contra o código atual, não contra o relatório. |
| `add_lead_to_funnel` corrida → 500 | **Real.** Corrigido (W8-08). O `SELECT` de `existing` e o `INSERT` não são atômicos; o `IntegrityError` subia cru em vez do 409 que o próprio endpoint já devolve. |
| `apply_tag` descartava o resultado do CRM | **Real.** Corrigido (W8-09). Como o espelho de tags roda a cada abertura da conversa, a tag aplicada só localmente sumia sozinha depois. |
| `get_database_schema()` descrevendo colunas inexistentes | **Real.** Corrigido (`cc6edce`): `funnels` não tem `is_default` nem `descricao`, e a lista fixa de etapas era invenção. A IA gerava SQL contra esse schema. |

---

## 4. O contrato de funil — a exigência central

O contrato declarado: WhatsApp / Bia / Gerenciador / Conversas / ferramenta de
IA entram em **Vendas: Principal**, etapa **Sem Contato**; o formulário do site
entra no funil próprio dele. E a solução **não pode** depender de menor id,
ordem de criação, substring "whatsapp", apagar funis, nem manter só dois funis
ativos.

O que foi feito, e o que foi medido:

- A resolução é por **igualdade sobre o nome normalizado** (`funnels.nome` é
  `UNIQUE`, então é identificador estável de domínio). Não é substring:
  `Vendas WhatsApp` não casa com `Vendas: Principal`.
- `DEFAULT_FUNNEL_ID`, se configurado, **tem** de apontar para um funil ATIVO.
  Se não apontar, o lead nasce sem funil com `ERROR` no log — nunca cai em outro
  funil em silêncio.
- Ambiguidade (dois funis ativos que normalizam para o mesmo nome) é
  **recusada**, não resolvida por chute.
- A etapa **deixou de ser `etapas[0]`**. "Primeira etapa" é a ordem em que
  alguém arrastou cartões na tela de configuração, não contrato de negócio.
  `id` tem precedência sobre `nome`, e empate desempata pelo próprio id —
  **reordenar as etapas não muda mais onde o lead nasce**.
- `_normalizar` aplica NFC, trata `_` como espaço e ignora caixa e espaço nas
  bordas. Foi assim que o `etapa_id` real de produção deixou de precisar ser
  adivinhado: `sem_contato` e `Sem Contato` casam os dois.
- A **mesma precedência** está espelhada em `conversas/app/services/crm.py`
  (SQL cru, processo separado, pacote homônimo — não dá para importar).

Uma tabela de decisão com 24 cenários foi executada nas **duas** implementações
lado a lado, incluindo: `Vendas: Principal` com o **maior** id entre 11 funis
ativos, funil principal inativo com um homônimo ativo, dois ativos que
normalizam igual, e oito formas diferentes de `DEFAULT_FUNNEL_ID` inválido. As
duas decidem **igual** em todos.

**Um defeito real ficou de fora do repositório:** `docker-compose.yml` não
passava `DEFAULT_FUNNEL_ID`, `DEFAULT_FUNNEL_NOME` nem `DEFAULT_ETAPA_NOME` para
nenhum dos dois serviços — e esse bloco passa tudo uma a uma, de propósito. Os
`ERROR`/`WARNING` mandavam o operador configurar uma variável que não chegava ao
container: diagnóstico correto, remediação sem efeito. Corrigido (W8-06).

---

## 5. Regressões que esta rodada introduziu — e corrigiu

Registradas como tal, não escondidas:

- **W8-01** — a correção anterior trocou "funil com whatsapp no nome" por "funil
  ativo de MENOR id". Era o mesmo acidente com outro nome: o funil certo vencia
  por ter sido criado primeiro. Com dez funis ativos, ou com o Principal tendo o
  maior id, o lead ia para o lugar errado.
- **W8-02** — ao passar a casar a etapa por `id` **ou** `nome`, o primeiro
  casamento **na ordem da lista** vencia. Pedir `etapa_id="triagem"` num funil
  `[{id:novo,nome:Triagem},{id:triagem,nome:Novo}]` devolvia `"novo"`: um id que
  existe, respondido com outra etapa, por posição.
- **W8-14** — a correção do F-341 fez `POST /api/leads` sempre colocar o lead no
  funil padrão, o que deu ao formulário do site **duas** entradas de funil. Sem
  sinal de origem do lado do servidor (o formulário e o Gerenciador chamam a
  mesma rota), a correção completa depende de uma mudança no n8n — M11.

---

## 6. Erro meu, preservado em vez de apagado

O **M6**: afirmei que a aplicação da D3 tinha quebrado o formulário em produção,
lendo `"jsonBody": "=={{ ... }}"` no export e concluindo, por analogia com o M1,
que havia um `=` sobrando. O operador conferiu no editor visual: o campo mostra
`{{`, e o `=` extra é a marcação com que o n8n serializa um campo em modo
expressão.

A analogia falhou porque no M1 os dois sinais estavam dentro do **valor** de um
parâmetro, e no M6 no marcador do **campo**. O sinal que eu deveria ter visto: o
nó irmão `Criar novo lead` tem corpo do mesmo formato e funciona. Eu tinha esse
contra-exemplo à vista e o usei como evidência **a favor** da tese errada.

Está registrado em `N8N_RECONCILIACAO_20260826.md` § 2 e no cabeçalho da matriz.

---

## 7. Segurança

Quatro achados exploráveis, todos corrigidos:

- **XSS armazenado (CRITICAL).** `leads.destinos` e `leads.idades_criancas` são
  texto livre que o n8n grava a partir do que o LLM extrai da conversa de
  WhatsApp — ou seja, o payload pode vir de um **cliente não autenticado**. Iam
  crus para `innerHTML`, enquanto a função irmã no mesmo arquivo já escapava.
  Com `script-src 'unsafe-inline'` na CSP, `onerror=` executa no contexto do
  admin que abrir a lista. A varredura achou mais três sinks do mesmo tipo,
  incluindo um `onclick` montado por concatenação de strings — que escapou da
  varredura anterior porque o detector procura a forma `` '${ ``.
- **Vazamento de credenciais pela IA interna (HIGH).** A denylist casava o
  **nome** `users`; as views de catálogo do PostgreSQL entregam o **conteúdo
  amostrado** sem que a palavra apareça. `SELECT most_common_vals FROM pg_stats`
  devolvia os hashes bcrypt dos admins e os hashes SHA-256 das API keys.
  Denylist perde para concatenação, aspas, `search_path`, CTE e subquery — virou
  **allowlist** com tokenizador de posição de tabela, fail-closed.
- **Negação de serviço no login do inbox (MEDIUM).** O login do Conversas é
  proxy servidor-a-servidor e não repassava o IP do cliente; o limite de 5/min
  do CRM chaveava no IP do container. Cinco tentativas com credencial lixo por
  minuto, sem conta, e nenhum atendente entrava.
- **CORS `*` com credenciais no CRM (MEDIUM).** O F8 tirou o curinga do
  Conversas e o CRM ficou de fora — mesmo default (`ENVIRONMENT` não definido já
  vale "development"), no serviço que guarda os leads e as chaves de API.

Dois itens reais ficaram **`DEFERRED`**, nomeados em `RELEASE_READINESS.md` em
vez de sumirem num `RESOLVED` genérico: a CSP com `unsafe-inline`, e a ausência
de allowlist de rotas em `call_internal_api`.

---

## 8. Dados, concorrência e PostgreSQL

O eixo que mais rendeu, e o que mais exigiu banco real:

- **Uma linha derrubava o filtro de campo personalizado de todos os leads.** O
  guard do F-043 cobria só o escape de NUL; `{"orcamento": 1e1000000}` é JSON
  válido, a coluna `json` aceita, e o cast para `jsonb` morre com
  `NumericValueOutOfRange`. O cast **saiu**, e o guard virou allowlist que falha
  **fechando**. Uma terceira causa (substituto solto, `\uD800`) foi encontrada
  por fuzz e já é coberta.
- **Lead duplicado no primeiro contato.** `lookup_lead_by_whatsapp` decidia por
  igualdade de dígitos em Python, mas os candidatos vinham de um `LIKE` sobre a
  coluna **crua**: lead gravado `+55 11 98765-4322` nunca entrava na lista.
- **`GET /api/leads/by-whatsapp` devolvia 404 para lead formatado** — nem a
  busca pela string idêntica à gravada casava. É a rota do formulário e da Tool
  do Gerenciador: o 404 fazia criar lead novo, e o próprio formulário grava
  formatado. O defeito se alimentava.
- **Histórico da conversa fora de ordem.** `Message.created_at` usava só
  `server_default=func.now()`, e no PostgreSQL `now()` é o início da
  **transação**. Com a transação do debounce aberta por minutos, a resposta da
  Bia aparecia antes da pergunta. A suíte local não pegava: no SQLite
  `CURRENT_TIMESTAMP` é por statement.
- **409 mentindo sobre violação de FK.** O `except IntegrityError` capturava
  qualquer constraint; funil apagado por outra requisição virava "Lead já está
  neste funil".

---

## 9. Operação do inbox

Dois HIGH que só apareceriam sob carga:

- **Pool esgotado pela Bia.** `_debounce_then_forward` segurava uma conexão do
  pool durante os 240 s da chamada ao agente — e o `AGENT_TIMEOUT` subiu de 60 s
  para 240 s nesta rodada, quadruplicando a retenção. O pool é de 15. Numa
  rajada de inbound, a 16ª requisição recebe `TimeoutError`, que está em
  `_INFRA_ERRORS`, então o webhook devolve 503, a Meta reentrega, e o pool não se
  recupera. **O inbox dos atendentes cai junto, pelo mesmo pool.**
- **Mesma mensagem enviada duas vezes à Bia.** O debounce tirava a conversa do
  registro **antes** de chamar o agente, então não havia guarda de lote em voo.
  Cliente impaciente que manda "alô?" recebia duas respostas, e as tools do
  Gerenciador rodavam duas vezes sobre a mesma mensagem — incluindo
  `Alterar Responsavel` e `Criar Lead`.

E um que aparecia todo dia:

- **Conversa retomada sumia de todas as abas.** O envio humano gravava atendente
  e `primeira_resposta_humana_at` mas deixava `status='encerrada'`, e todo
  predicado do inbox exige aberta/aguardando. Pior: na resposta seguinte do
  cliente, o ramo de reabertura zerava o atendente e **a Bia reassumia** a
  conversa que um humano tinha retomado.

---

## 10. Migrations

- **O F5 da m011 (puro DDL) rodava atrás de quatro verificações dependentes de
  dado.** Uma duplicata em qualquer tabela abortava a run e deixava o F5 sem
  aplicar, com o schema já parcialmente migrado — o defeito "INSERT de
  psql/n8n/COPY rejeitado" ficava aberto por um dado sem relação com ele. Agora
  o F5 vem primeiro, as tabelas limpas ganham o índice, e as sujas são
  reportadas **todas numa rodada só**.
- **Os gates mentiam no banco errado.** Apontadas para um PostgreSQL vazio, as
  duas imprimiam "OK — NO-OP (já estava aplicada)" e saíam 0, afirmando um
  estado que não verificaram. Operador com `DATABASE_URL` errado marcava o
  runbook como feito e produção seguia sem a coluna e sem os índices. Agora
  recusam com exit 1 e explicam.
- **O backfill da m012 deixava `queued_at`**, produzindo o estado que
  `aplicar_estado_humano` declara impossível. O `UPDATE` separado também
  **repara** linhas que uma rodada anterior já deixou assim.

Cada migration alterada foi executada **duas vezes seguidas** contra PostgreSQL
16 com schema de produção. Idempotência confirmada nas duas.

---

## 11. Contagem

**Matriz** (`MASTER_FUNCTIONAL_BUG_MATRIX.md`): **144 linhas**, 110 da rodada
anterior + 34 da Wave 8.

| Status | Quantidade |
|---|---:|
| `RESOLVED` | 84 |
| `FIXED_PENDING_MANUAL_N8N` | 17 |
| `DUPLICATE_ROOT_CAUSE` | 16 |
| `NOT_REPRODUCED_WITH_EVIDENCE` | 15 |
| `BLOCKED_OPERATOR` | 6 |
| `DEFERRED` | 5 |
| `OPEN` | 1 |
| **Total** | **144** |

**Suíte**: `85/85 PASS`, um processo por arquivo, como o CI faz
(`scratch/final2.txt`). Depois dessa medição entrou um último delta em dois
arquivos de teste (`0d4c9a6`), reverificados individualmente nos dois backends:
`test_postgres_dialect_divergence` e `test_segments_sql_count`, exit 0 nos dois. Baseline da rodada anterior: 83/83 — os dois arquivos a
mais são `tests/test_pipeline_funnel_race.py` e `tests/test_conversas_lead_link.py`,
nascidos nesta rodada.

**Smoke e2e contra PostgreSQL 16**: `22/22 PASS`, zero falhas, com limpeza
confirmada ao final. Mesmo número da rodada anterior; a cadeia de handoff
(`PUT /api/leads/{id}/responsavel` → ponte → conversa sai da Bia e entra na
fila, inclusive no **segundo** handoff do mesmo lead) continua íntegra depois de
todas as correções desta rodada.

**Três testes precisaram ser reescritos** porque travavam o *mecanismo*
(`jsonb_each_text`, `jsonb_typeof`, `AS JSONB`, "CASE aninhado") em vez da
propriedade. Foi exatamente isso que os tornou frágeis: a correção do W8-28
removeu o cast de propósito, e eles quebraram sem que nada tivesse regredido. As
asserções passaram a exigir a propriedade — uma linha ruim não derruba a
consulta das outras — que é o que o F-043 sempre foi.

O único `OPEN` é **W2-21**, e apenas na parte que é refatoração: consolidar os
cinco caminhos que escrevem `responsavel_id`. A parte **funcional** dele — o
caminho que pulava o histórico e a ponte — foi corrigida (W8-14).

---

## 12. Tetos declarados

Reais, medidos, e **não** corrigidos. Nenhum é sintoma relatado pelo operador;
todos exigem decisão de produto ou mudança ampla que esconderia regressão se
misturada aqui.

| Item | Por que fica |
|---|---|
| CSP com `script-src 'unsafe-inline'` | Tirar exige remover todo handler inline de todos os templates. WP próprio. |
| Sem allowlist de rotas em `call_internal_api` | A allowlist certa precisa da lista de operações que a Perpétua legitimamente executa — decisão de produto. |
| Guarda de lote em voo vale por **um processo** | Com 2+ workers uvicorn o defeito volta inteiro. Para valer entre workers tem de virar marcador no banco. |
| `conversations.updated_at` com carimbo de início de transação | O inbox ordena por ele, então a conversa que a Bia acabou de responder aparece até ~4 min mais velha. Trocar o `onupdate` atinge toda rota que faz UPDATE. |
| `ix_conversations_whatsapp` legado não é dropado | A m011 é auditável **por ser puramente aditiva**, e a docstring dela proíbe `DROP INDEX`. DDL para remoção manual está em `migrations/README.md`. |
| Eixo DDI na busca por WhatsApp | `11987654322` continua não casando com `5511987654322`: é outro conjunto de dígitos. Ambiguidade responde 409 — nada aqui adivinha o país. |
| Sem índice de expressão para a busca normalizada | Cada lookup é Seq Scan de ~12 ms com 19k leads. Criá-lo é migration; com ele, 0,19 ms. |
| Sessão aberta durante o envio à Meta | Até 48 s por parte, janela ~20× menor que a do agente, mas real. |
| Linha com escape de NUL continua invisível ao filtro | Troca deliberada: some **uma linha**, não a funcionalidade. Só `jsonb` na coluna resolve, e é migration. |
| `google-generativeai` deprecado | Importa e funciona (0.8.6). Migrar para `google-genai` é projeto, não correção. |

---

## 13. O que depende do n8n (você, à mão)

Instruções completas em `N8N_MANUAL_CHANGES.md`. Nenhuma foi aplicada — não
tenho acesso ao n8n de produção.

| Item | O que é | Status |
|---|---|---|
| **M11** | O nó `Criar novo lead` do formulário precisa mandar `funnel_nome=Vendas: Formulário` e `etapa_id=nova_oportunidade`. Sem isso, todo lead do formulário ganha **duas** entradas de funil. | `FIXED_PENDING_MANUAL_N8N` |
| **M12** | O nó `Lead existe?` testa `statusCode === 200`, então o **409** de ambiguidade cai no mesmo ramo do 404 e o workflow cria uma **terceira** linha. | `FIXED_PENDING_MANUAL_N8N` |
| **D2** | Ativar *Header Auth* em `/webhook/agent-bia` — **depois** de subir o Conversas com `N8N_WEBHOOK_AUTH_*`. A ordem inversa corta a Bia. | `FIXED_PENDING_SYNCHRONIZED_DEPLOY` |
| **M8 / M9 / M10** | Follow-up por inatividade, item de baixa prioridade, e consolidação de duplicados. | `DEFERRED` por decisão do operador |

---

## 14. O que depende de produção

Nada disto foi feito, e nada disto pode ser feito daqui.

| Ação | Por quê | Como saber que deu certo |
|---|---|---|
| Definir `CONVERSAS_BASE_URL` e `CONVERSAS_API_KEY` no CRM | Sem a chave a ponte de handoff é no-op silencioso — é o comportamento de hoje, e nada regride. **Não gere nem me mande a chave.** | O cabeçalho `X-Conversa-Handoff` passa a responder `movida` em vez de `pendente`. |
| Definir `DEFAULT_FUNNEL_NOME` / `DEFAULT_ETAPA_NOME` se os nomes de produção diferirem dos defaults | Os defaults são `Vendas: Principal` e `Sem Contato`. Se o funil real tiver outro nome, o lead nasce sem funil com `ERROR` no log. | Criar um lead de teste pelo WhatsApp e conferir o funil e a etapa. |
| Rodar `migrations/m011` e `migrations/m012` | Com backup e aprovação, como manda `migrations/README.md`. As duas são idempotentes e foram validadas em PostgreSQL 16 descartável, duas execuções seguidas. | Exit 0. Se sair 1, o alvo está errado — o gate novo recusa em vez de mentir "NO-OP". |
| Subir o Conversas com `N8N_WEBHOOK_AUTH_HEADER` e `N8N_WEBHOOK_AUTH_VALUE`, **e só então** ligar o Header Auth no n8n | A ordem inversa derruba a Bia para todos os clientes. | Mandar uma mensagem de teste no WhatsApp entre os dois passos. |
| Considerar `CREATE INDEX ix_leads_whatsapp_digitos ON leads ((regexp_replace(whatsapp,'[^0-9]','','g')))` | A mesma expressão serve **as duas** rotas de busca por WhatsApp. Sem ele, 12 ms por lookup com 19k leads. | `EXPLAIN ANALYZE` passa de Seq Scan para Index Scan. |
| Consolidar as duplicatas de lead já existentes | Alteração de dado de produção, caso a caso. A correção que impede **novas** já está no repositório. | — |
| Decidir o **W8-16** | Trocar o responsável no Kanban desliga a Bia e põe a conversa na fila. É desejado? O comportamento **não** foi alterado; a tela agora avisa. | Decisão de produto. |

---

## 15. Veredito

Não afirmo que o sistema está validado — nada aqui foi executado em produção, e
a maior parte das correções desta rodada nunca viu tráfego real.

O que posso afirmar, com evidência: as causas raiz estão nomeadas e corrigidas
com teste; cada regressão que esta rodada introduziu foi encontrada e corrigida
dentro dela; os defeitos que dependiam de PostgreSQL foram reproduzidos e
verificados contra PostgreSQL de verdade; e o que não foi corrigido está
nomeado, com o motivo, em vez de escondido atrás de um status verde.

**VEREDITO: B — `READY FOR CONTROLLED DEPLOY — PENDING PRODUCTION VALIDATION`.**

Condicionado à ordem da seção 14. Em particular: as migrations antes do código
que depende delas, e o Conversas antes do Header Auth no n8n.
