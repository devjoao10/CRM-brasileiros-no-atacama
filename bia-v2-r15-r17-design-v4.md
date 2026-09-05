# Desenho — Fechamento de R-B (R15/R16/R17 permanecem como na v3, não reabertos)

**Status:** DESENHO FECHADO / APROVADO PARA IMPLEMENTAÇÃO — depois de 4
rodadas de revisão/auditoria (§11.1-11.6), as duas últimas (§11.5, §11.6)
auditorias de COMPLETUDE especificamente sobre correções de rodadas
anteriores, não descoberta aberta. Este status cobre SÓ a arquitetura e
sua especificação: **AINDA NÃO IMPLEMENTADO** (nenhum código de produção,
migration real, worker, ou edição do plano nesta rodada) e **AINDA NÃO
APROVADO PARA CANARY/PRODUÇÃO** (isso depende de implementação + E2E
distribuído verde + os demais gates da matriz de prontidão — R10,
R1/R2, R3, R8, R9, R11, R13 — nenhum dos quais é tocado por este
documento).

Histórico de revisão, preservado: a versão originalmente despachada para
a primeira rodada de revisão obrigatória foi **BLOQUEADA** pelo revisor de
concorrência (1 CRITICAL) e recebeu mais 2 HIGH (um do próprio revisor de
concorrência, um do revisor de arquitetura), além de MEDIUM/LOW dos 4
revisores — todos incorporados nesta revisão do texto (seção 11.1), com
o achado CRITICAL re-verificado empiricamente de forma dedicada antes de
qualquer declaração de fechamento (seção 11.2). A seção 11.3 registra uma
SEGUNDA rodada de closure, rodada depois da matriz exaustiva de erro da
seção 6 ter sido incorporada — que por sua vez encontrou mais achados
(incluindo 1 HIGH que a própria correção introduziu, seção 2.1.1). A
seção 11.5 registra uma TERCEIRA rodada — auditoria de completude sobre
as correções da segunda rodada — que encontrou, entre outras coisas, que
a correção do HIGH de §2.1.1 tinha, ela mesma, uma regressão própria
contra uma garantia documentada do código real. A seção 11.6 registra
uma QUARTA rodada — checagem estreita, direcionada só a essa síntese
específica — que confirmou o núcleo correto mas achou mais 1 HIGH
bloqueante e uma cauda de precisão, incluindo textos residuais que ainda
descreviam a ordem já rejeitada (um deles afirmando falsamente que já
tinha sido corrigido). Tudo corrigido antes desta declaração de status.

**HEAD de referência:** `54f160f136643fc51e12dd659ecdb41e498c2728`,
congelada durante toda a análise (confirmada antes e depois de cada
despacho).

## Ferramentas e agentes usados nesta rodada

- **Graphify**: consultado para orientação inicial.
- **Subagentes (mecanismo `Agent`)**: harness de investigação. **Nota de
  transparência, repetida de propósito**: não existe neste ambiente uma
  ferramenta ou hook chamado "AIA Harness" invocável por nome — não há
  evidência de tal coisa para mostrar porque a coisa não existe aqui. O
  mecanismo real usado é o de subagentes especializados.
- **`database-architect`** (`opus`, 2 despachos): desenho e prova empírica
  inicial da arquitetura de R-B; revisão de concorrência que bloqueou a
  primeira versão e forneceu a correção.
- Revisão obrigatória de 4 tipos (database/concurrency, architecture,
  security, Python/contracts) — ver seção 11.

---

## 0. Ponto de partida

R-B (v3, seção 10c): duas operações V2 diferentes, `event_id` distintos,
podem atravessar qualquer exclusão local e alcançar
`POST /api/v2/lead-domain/handoff` com `target_user_id` diferentes — o CRM
decide por ordem de chegada HTTP, sem exclusão própria. **Qualquer solução
exclusivamente do lado Conversas é insuficiente** — provado na v3.

---

## 1. Hipótese do índice único parcial — REFUTADA (dilema de dois chifres, provado)

**Hipótese testada**: índice único parcial em `lead_handoff_operations` por
`(lead_id) WHERE status='ACTIVE'`.

- **H1 — claim nunca liberado**: fecha os Casos 1-5, mas o Caso 6 (release
  legítimo, nova atribuição) trava o lead PERMANENTEMENTE — sem TTL nem
  edição manual (ambos proibidos pelo escopo), não há saída.
- **H2 — claim liberado na mesma transação**: não fecha nada além do
  comportamento de hoje.

**O índice único parcial no lado CRM está descartado.** Ver
`bia-v2-rb-proof.md` para a evidência bruta.

---

## 2. Mecanismo — compare-and-swap sob lock de linha, ESPECIFICAÇÃO CORRIGIDA

### 2.1 O que fecha o problema, em princípio

Para uma operação NOVA (nunca vista): re-checagem do livro-razão (sem
lock) → `SELECT`/refresh do lead **sob lock** → compare-and-swap contra
`expected_responsavel_id` → `INSERT` no livro-razão + `UPDATE` do lead +
`LeadHistory` → **um único** `db.commit()`. Para um REPLAY (`event_id`
já visto): re-checagem do livro-razão → leitura fresca do lead SEM lock
de escrita, só para comparar dono atual contra o registro (§2.1.1) — o
lock de escrita só serve à operação que vai realmente escrever. Zero
chamada de rede no meio, zero lifecycle, zero claim para expirar.

### 2.1.1 ACHADO da revisão de fechamento — o replay precisa detectar staleness, SEM copiar o lock de escrita do caminho de primeira aplicação

A primeira versão desta seção (e a ordem de transação do §8) tratava
"replay por `event_id` já visto" como um atalho ANTES da query com lock —
devolvia o resultado histórico sem tocar `leads`. **Isso reabre exatamente
a classe de problema que R-B fecha**, por uma via nova: o worker de
retomada (R16, §5) existe especificamente para reenviar `event_id`
antigos, e nada garante que o dono do lead ainda seja o mesmo de quando
esse `event_id` aplicou pela primeira vez — um release legítimo (Caso 6)
ou uma operação mais recente pode ter movido o lead no meio do caminho.
Um replay que devolve cegamente o resultado histórico faz o Conversas
aplicar localmente um `target_user_id` que já não é mais verdade no CRM,
sem que nenhum dos dois lados perceba — os dois relatam sucesso, e
`CRM.leads.responsavel_id != Conversas.conversations.atendente_id`
(violação direta do critério global do §8.5).

**Correção, revisada nesta rodada** (a primeira tentativa desta correção
tinha um problema próprio — ver a nota "Ordem revisada" abaixo): o
código real (`app/routers/v2_lead_domain.py:205-239`) consulta o
livro-razão ANTES de validar lead/usuário, de propósito, com um
comentário de 35 linhas explicando por quê — se um handoff já aplicou e
DEPOIS o alvo foi desativado (ou o lead apagado; `LeadHandoffOperation`
não tem FK em `lead_id`, de propósito, sobrevive à deleção), um replay
do MESMO `event_id` não pode virar 404/422 sobre uma operação que já
teve sucesso. Essa garantia **precisa ser preservada** — a correção de
R-B não pode reabri-la por outra porta. A chave é perceber que ela e a
checagem de staleness (Caso 2b) não competem pela MESMA posição na
ordem: a consulta ao livro-razão não depende de o lead existir/ser
válido; só a comparação de staleness depende de uma leitura fresca do
lead, e só quando ele ainda existe.

```python
registro = _buscar_registro(db, event_id)  # sem lock — não depende do lead

try:
    if registro is not None:  # replay — NUNCA revalida usuário/422 (garantia preservada)
        # todo caminho deste bloco termina em `return` ou `raise` — nenhuma
        # execução cai para a seção "primeira vez" abaixo quando registro
        # existe; um implementador que troque isto por um `if`/fall-through
        # sem os três terminadores explícitos reabre o bug do achado G1
        lead = (
            db.query(Lead).filter(Lead.id == lead_id)
            .populate_existing()  # leitura fresca; SEM with_for_update() — não vai escrever
            .first()
        )
        atual = lead.responsavel_id if lead is not None else None
        _conferir_parametros(registro, lead_id, target_user_id, current_responsavel_id=atual)
        # 409 IDEMPOTENCY_PAYLOAD_CONFLICT se payload não bater — AGORA com o campo
        # preenchido, seja qual for a origem do 409 (achado G1 da checagem final:
        # a leitura do lead precisa vir ANTES desta checagem, não depois, senão o
        # campo que a seção 7 promete "sempre presente" fica ausente aqui)
        if lead is None:
            # lead apagado desde que o evento aplicou — sem dado pra comparar,
            # devolve o resultado histórico (mesma garantia do código real)
            return HandoffResponse(replayed=True, current_responsavel_id=None, ...)
        if lead.responsavel_id == registro.target_user_id:
            return HandoffResponse(replayed=True, current_responsavel_id=lead.responsavel_id, ...)
        # dono MUDOU desde então (inclusive para None, se foi só liberado e ainda
        # não reatribuído — ver nota "Sub-caso: lead sem dono" abaixo) —
        # replayar cegamente aplicaria um alvo já superado
        raise LeadJaAtribuido(current_responsavel_id=lead.responsavel_id, ...)  # 409 LEAD_ALREADY_ASSIGNED

    # primeira vez — validação normal, ESTA sim precisa do lead existir
    lead = (
        db.query(Lead).filter(Lead.id == lead_id)
        .with_for_update().populate_existing()  # vai escrever — precisa do lock
        .first()
    )
    if lead is None:
        raise LeadNaoEncontrado(...)
    _carregar_usuario_ativo(target_user_id)  # 422 se ausente/inativo
    if lead.responsavel_id != expected_responsavel_id:  # CAS — forma exata na seção 2.1.2
        raise LeadJaAtribuido(current_responsavel_id=lead.responsavel_id, ...)  # 409 LEAD_ALREADY_ASSIGNED
    # aplica: INSERT no ledger, UPDATE lead, LeadHistory, um único commit
except SQLAlchemyError:
    raise HandoffIndeterminado(...)  # cobre AMBOS os ramos agora — fecha G6, ver §8
```

**Ordem revisada, nota de transparência**: a primeira tentativa desta
correção (dentro desta MESMA rodada de fechamento) colocava a consulta ao
livro-razão DEPOIS da query com lock — o que teria movido 404/422 para
ANTES do replay, reabrindo exatamente o bug que o comentário de 35 linhas
do código real existe para evitar, só que por um caminho diferente do
achado original. Pego pela auditoria de completude desta mesma rodada,
antes de qualquer implementação — corrigido acima antes de sair do
desenho.

**Correção ao próprio texto desta seção, achada na verificação seguinte
(G1, a única tratada como bloqueante)**: a versão anterior chamava
`_conferir_parametros` ANTES de ler o lead — mas esse 409 precisa de
`current_responsavel_id` no corpo (§7 promete "sempre presente" nos DOIS
casos de 409), e não havia lead lido ainda naquele ponto. Corrigido acima:
a leitura do lead sobe para ANTES de `_conferir_parametros`, dentro do
mesmo ramo de replay — isto não fere a garantia de nunca revalidar (a
leitura aqui é só para popular um campo de diagnóstico, nunca vira um
404/422 novo; um lead ausente já tem tratamento próprio logo abaixo).

**Por que não usa `with_for_update()`, com a ressalva que faltava**: o
replay é leitura pura para comparação, não seguida de escrita — o lock de
escrita tem benefício real (serializa contra um escritor CONCORRENTE em
voo, então um `INSERT`+`UPDATE` que ainda não commitou é esperado antes
de responder), mas **não fecha o TOCTOU por completo**: um escritor que
comece DEPOIS que a transação do replay já terminou produz a mesma
divergência com ou sem lock — nenhuma forma de lock de linha segura
através de uma resposta HTTP já enviada. Por isso o Caso 2b (abaixo) é
garantia forte no cenário SEQUENCIAL que o teste E2E M exercita (release
já commitado antes do replay começar), não uma garantia absoluta sob
concorrência genuína entre um replay e uma primeira aplicação em voo —
esse resíduo específico é nomeado no §10.

**Invariante de isolamento, nomeada explicitamente (G3 da checagem
final)**: a correção acima depende de READ COMMITTED — sem lock, cada
`SELECT` vê o último valor COMMITADO no momento em que roda, o que é
exatamente "fresco o bastante" para a comparação. Sob REPEATABLE
READ/SERIALIZABLE o snapshot da transação ficaria congelado no primeiro
`SELECT` da request (que é do `get_current_user`, antes de qualquer
código desta rota rodar) — e a leitura do lead voltaria obsoleta **em
silêncio**, sem nenhum erro, ao contrário do caminho com `FOR UPDATE`
(que falharia ruidosamente sob RR/SERIALIZABLE com "could not serialize
access"). Hoje o `create_engine(...)` do CRM (`app/database.py:19`) não
passa `isolation_level` e o Postgres usa READ COMMITTED por padrão — mas
isso é implícito, não testável pelo CI (SQLite não tem o mesmo conceito
de nível de isolamento) e vulnerável a uma mudança futura no engine.
**Correção obrigatória**: `create_engine(..., isolation_level="READ COMMITTED")`
explícito, removendo a dependência de um default nunca declarado.

**Sub-caso: replay contra lead sem dono (G5 da checagem final)**: entre um
release legítimo (Caso 6) e uma reatribuição, `lead.responsavel_id` é
`None` enquanto `registro.target_user_id` nunca é (coluna `NOT NULL`,
`app/models/lead_handoff_operation.py:98`) — a comparação sempre bate
"diferente" nessa janela, e o replay corretamente recusa com 409
`LEAD_ALREADY_ASSIGNED`, `current_responsavel_id=None`. **Isto é
intencional, não um bug**, mas o nome do token engana à primeira leitura
("já atribuído" com valor `None` soa contraditório) — na verdade comunica
"o valor que este replay esperava não é mais verdade; o valor real,
inclusive quando esse valor real é 'ninguém', está no corpo". Qualquer
código que compare `current_responsavel_id` (worker de reconciliação,
§4.1/§6.2) precisa tratar `None` como um valor legítimo de comparação,
não como ausência de dado.

A prosa "compare-and-swap contra `expected_responsavel_id`" tem duas
leituras possíveis para `None`: (A) "sem expectativa, aplica incondicional"
ou (B) "espero que o dono atual seja Nulo". **A leitura correta É a (B)** —
(A) reabriria a corrida que R-B inteiro existe para fechar (um chamador
que não manda expectativa venceria sempre, incondicionalmente). Isso nunca
tinha sido mostrado como código, só como prosa — e a ambiguidade tem uma
consequência real de deploy que a seção 7 (nota de runbook) só cobria na
direção OPOSTA: durante a janela em que o CRM já subiu esta mudança mas o
Conversas ainda não (ordem de deploy do §8), um Conversas ANTIGO nunca
manda `expected_responsavel_id` — e sob a leitura (B) aplicada
ingenuamente a um campo **ausente**, todo handoff para um lead que já tem
dono (o cenário "cliente que volta", motivador central desta arquitetura)
falharia CAS espuriamente com 409, quando o comportamento de produção
ANTES desta mudança sempre aplicou incondicionalmente nesse caso.

**Correção**: distinguir campo AUSENTE de campo presente como `null`,
via `model_fields_set` do Pydantic v2 (não `.get()`/default, que não
diferencia os dois):

```python
if "expected_responsavel_id" not in request.model_fields_set:
    # chamador antigo, pre-R-B — nao sabe que este campo existe.
    # comportamento antigo preservado: aplica sem checar CAS.
    cas_ok = True
else:
    # chamador novo — expected_responsavel_id explicito, mesmo que None
    # (None aqui SIGNIFICA "espero que o lead esteja sem dono").
    cas_ok = (lead.responsavel_id == request.expected_responsavel_id)
if not cas_ok:
    raise LeadJaAtribuido(current_responsavel_id=lead.responsavel_id, ...)
```

Isto fecha a janela de deploy sem enfraquecer o CAS para nenhum chamador
que já conhece o campo — só o comportamento ANTIGO (que já não tinha CAS
nenhum) continua sem CAS até o Conversas ser atualizado, exatamente como
era antes desta mudança começar a existir. **Nota de runbook adicional**,
paralela à já existente na seção 7 (que cobre a direção oposta —
Conversas novo + CRM velho → 422 total por `extra="forbid"`): durante
CRM-novo + Conversas-velho, o CAS fica inerte (sempre aplica), não
bloqueado — é uma regressão de PROTEÇÃO durante a janela, não de
disponibilidade; a ordem de deploy (migrations → CRM → Conversas → worker)
já minimiza essa janela, e ela é o motivo pelo qual o worker só sobe por
último.

### 2.2 ACHADO CRÍTICO da revisão — a leitura sob lock, como descrita originalmente, é um no-op silencioso

**A primeira versão deste documento dizia apenas "`SELECT ... FOR UPDATE`
no lead", sem especificar a chamada exata.** Isso é insuficiente até o
ponto de ser perigoso: o handler real (`_carregar_lead`,
`app/routers/v2_lead_domain.py:116-120`) já carrega o `Lead` no **identity
map** do SQLAlchemy, ANTES de qualquer lock, para produzir o 404. Se o
lock subsequente for tomado com `.with_for_update().first()` — a forma
mais natural de "adicionar `FOR UPDATE` numa query existente" — o
PostgreSQL adquire o lock de linha DE VERDADE, mas o SQLAlchemy devolve o
MESMO objeto Python já em memória, com `responsavel_id` desatualizado. O
CAS compara contra esse valor obsoleto e **passa quando não deveria**.

**Provado, não hipotético**: com essa forma literal, 39/40 rodadas com 2
threads e 8/8 rodadas com 3 PROCESSOS REAIS produziram exatamente o
resultado que R-B existe para impedir — as duas operações aplicam,
`replayed=False` para ambas, dono final decidido por acaso.

### 2.3 Especificação corrigida, sem ambiguidade

**Elimine a leitura pré-lock separada.** A consulta que produz o 404 do
lead PASSA A SER, ela mesma, a consulta com lock:

```python
lead = (
    db.query(Lead)
    .filter(Lead.id == lead_id)
    .with_for_update()
    .populate_existing()   # obrigatório — sem isto, o identity map vence
    .first()
)
if lead is None:
    raise LeadNaoEncontrado(...)
```

`.populate_existing()` força o SQLAlchemy a re-hidratar o objeto com os
valores FRESCOS lidos sob o lock, mesmo que já exista no identity map.
Alternativa equivalente, se a implementação preferir manter
`_carregar_lead` como está e adicionar uma etapa separada: `db.refresh(lead,
with_for_update=True)` — o MESMO idioma que `conversas/app/v2/handoff.py:485`
(módulo irmão, `_aplicar_estado_local`) já usa corretamente hoje.
**Qualquer uma das duas formas é aceitável; a forma SEM
`populate_existing()`/`refresh()` explícito NÃO é.**

**Verificado, com a correção**: 0 falhas em 40 rodadas de threads + 8
rodadas de 3 processos reais — ver seção 11.2 para a re-verificação
dedicada.

**Regra de implementação obrigatória, para além do texto**: o teste de
aceitação dos Casos 4/5 (seção 8) precisa incluir uma variante que rode a
forma SEM `populate_existing()` e prove que ela FALHA — não basta um teste
que só prove que a forma correta funciona, porque a forma incorreta também
parece funcionar em cenários não-concorrentes (é exatamente isso que
tornou o erro fácil de introduzir sem perceber).

---

## 3. Os casos — sem "provavelmente" (6 da primeira versão + Caso 2b desta rodada)

| Caso | Comportamento | Mecanismo |
|---|---|---|
| **1** — primeiro handoff, A(10) | `200 replayed=false`, `current_responsavel_id=10` | CAS passa contra o baseline lido |
| **2** — replay, mesmo `event_id` A(10), dono AINDA é 10 | `200 replayed=true`, mesmo resultado lógico, zero efeito novo | Livro-razão encontra o registro → leitura fresca do lead SEM lock de escrita (§2.1.1) — dono lido bate com o registro |
| **2b** — replay, mesmo `event_id` A(10), dono JÁ NÃO é mais 10 (release/reatribuição legítima aconteceu depois de A aplicar) | `409 LEAD_ALREADY_ASSIGNED`, `current_responsavel_id`=dono real | Mesmo mecanismo do Caso 2 — a leitura fresca (sem lock, garantida por READ COMMITTED, §2.1.1) detecta que o alvo do replay está superado e RECUSA aplicar localmente algo que já não é verdade; fecha a lacuna que tornava R16 perigoso. Garantia forte no cenário SEQUENCIAL (release já commitado antes do replay); sob concorrência genuína com uma primeira aplicação em voo, ver residual em §10 |
| **3** — mesmo `event_id`, target diferente | `409 IDEMPOTENCY_PAYLOAD_CONFLICT`, owner intocado | `_conferir_parametros`, já existente, inalterado |
| **4** — `event_id` B concorrente, mesmo lead, target diferente | Exatamente uma aplica; a perdedora recebe `409 LEAD_ALREADY_ASSIGNED` | `FOR UPDATE .populate_existing()` serializa; CAS da perdedora falha contra o valor JÁ FRESCO |
| **5** — B chega depois que A já completou | Idêntico ao Caso 4 do ponto de vista do CRM (monofásico, sem distinção temporal) | Reatribuição só aceita com o baseline correto |
| **6** — release legítimo, nova atribuição depois | Aceita, sem TTL | O release zera `leads.responsavel_id`; nova operação lê esse baseline e o CAS passa |

Token `LEAD_ALREADY_RESERVED_BY_OTHER_OPERATION` descartado — confirmado
pela revisão de arquitetura que o estado que ele descreveria é literalmente
inobservável nesta arquitetura (MVCC do PostgreSQL garante que uma
transação concorrente só vê "antes do commit" ou "depois", nunca
"reservado mas não aplicado" com 2 fases). `current_owner_event_id`
devolve o dado real, e não perde informação em relação ao token — ver
ressalva de confiabilidade na seção 7.

---

## 4. R15 — política (C) híbrida determinística, com a correção de derivação de `operation_id`

Mantido da primeira versão: política pura (A) impossível quando um humano
já respondeu; pura (B) errada (tiraria o lead do humano ativo). As duas
direções são chamadas ORDINÁRIAS ao mesmo endpoint.

**ACHADO da revisão (CRITICAL-adjacente, corrigido)**: o `operation_id`
proposto originalmente (`uuid5(NS, "correcao:"+event_id)`, direção B) era
função SÓ do `event_id`, mas o livro-razão do CRM amarra
`event_id → (lead_id, target_user_id)` PERMANENTEMENTE
(`_conferir_parametros`). Se o `target_user_id` local mudar entre
tentativas de correção (outro humano assume a conversa no meio do
caminho), a MESMA chave derivada tenta reaplicar um `target_user_id`
diferente do que está gravado — `409 IDEMPOTENCY_PAYLOAD_CONFLICT`
(**terminal**, por definição da seção 6.2), a reconciliação nunca converge,
`reconciled_at` fica NULL para sempre, violando "nenhum caminho exige
edição manual".

**Correção**: derivar de `event_id` **e** `target_user_id` juntos:

```python
NS_RECONCILIACAO = uuid.UUID("<constante fixa, definida uma vez, nunca recalculada>")
operation_id = uuid.uuid5(NS_RECONCILIACAO, f"correcao:{event_id}:{target_user_id}")
```

Continua determinístico e crash-safe (mesma entrada sempre produz a mesma
saída); um alvo diferente agora produz uma OPERAÇÃO diferente em vez de um
conflito terminal. **`NS_RECONCILIACAO` precisa ser uma constante nomeada
e fixa no código, escrita uma única vez** — dois implementadores
escolhendo valores diferentes quebram silenciosamente a propriedade de
reprodutibilidade (achado LOW da revisão, incorporado aqui por ser
trivial de fechar).

| Campo | Direção A (converge Conversas → CRM) | Direção B (converge CRM → Conversas) |
|---|---|---|
| Estado de origem | `FAILED`/`CRM_LEAD_JA_ATRIBUIDO` | `FAILED`/`ESTADO_LOCAL_RECUSADO`, `atendente_id != target_user_id` |
| Estado alvo | `WAITING_HUMAN`, campos alinhados ao CRM | `lead.responsavel_id = conversation.atendente_id` |
| `operation_id` | `uuid5(NS, "convergencia:"+event_id)` | `uuid5(NS, "correcao:"+event_id+":"+target_user_id)` — CORRIGIDO |
| Mecanismo de escrita | **`handoff_to_human()`/o endpoint HTTP — NUNCA `sync_responsavel_to_crm`** (ver achado de segurança abaixo) | idem |
| CAS recusa a correção, `current_responsavel_id` == `conversation.atendente_id` | — | `RECONCILIADO_JA_DIVERGIU`, informativo, NÃO sobrescreve, sem ticket |
| CAS recusa a correção, `current_responsavel_id` != `conversation.atendente_id` | — | divergência NOVA, não resolvida — trata como `AMBIGUO_RETRIAVEL`, gera ticket (§6.2) |

`RECONCILIADO_JA_DIVERGIU` não é um token que o CRM emite — é a
reinterpretação, só dentro do código do worker de reconciliação, de um
`409 LEAD_ALREADY_ASSIGNED` comum (o MESMO token do Caso 4/5), **e só
depois da comparação explícita da linha acima** (achado HIGH da revisão
de fechamento — uma falha de CAS sozinha não prova convergência, só
prova que o dono mudou; ver §6.2 para o raciocínio completo, inclusive
por que essa distinção não pode viver no lado do CRM).

### ACHADO DE SEGURANÇA, incorporado — a direção B NÃO PODE usar `sync_responsavel_to_crm`

A revisão de segurança encontrou que `conversas/app/services/crm.py:182-255`
(`sync_responsavel_to_crm`) já existe, já é usado hoje
(`conversas/app/routers/conversations.py:391-429`, reatribuição manual
pela UI legada), e escreve `lead.responsavel_id` **direto no Postgres
compartilhado, sem `FOR UPDATE`, sem CAS, sem gravar no livro-razão** — é
exatamente o nome/padrão "óbvio" a que um implementador chegaria para
"sincronizar responsavel com o CRM", e usá-lo aqui **contornaria
silenciosamente todas as garantias que este documento prova**. **Proibido
explicitamente**: a direção B só pode escrever `lead.responsavel_id`
através de `handoff_to_human()`/o endpoint HTTP protegido por CAS — nunca
por `sync_responsavel_to_crm` nem qualquer outro caminho de escrita direta.
Recomenda-se um teste que garanta que o worker de reconciliação nunca
importa essa função.

### 4.1 `_repair_responsavel_cache` — achado real, correção REVERTIDA por decisão explícita (permanece residual sob R10)

`conversas/app/routers/conversations.py` (`_repair_responsavel_cache`,
chamado em toda listagem do inbox e toda abertura de conversa) escreve
`Conversation.responsavel_id` a partir do CRM via `UPDATE` cru, e **não
toca `atendente_id`**. O `CHECK ck_conversations_estado_humano`
(`conversas/app/models/conversation.py`) exige, para
`state IN ('WAITING_HUMAN','HUMAN_ACTIVE')`: `atendente_id = responsavel_id`.
Antes de R15, um lead com `responsavel_id != atendente_id` numa conversa
em estado humano era uma divergência RARA e transitória. **R15, por
desenho, torna essa divergência uma condição ROTINEIRA e PERSISTENTE**
durante toda a janela entre a falha original e o worker de reconciliação
rodar — é literalmente a premissa de operação da Direção B (`ESTADO_LOCAL_RECUSADO`,
`atendente_id != target_user_id`). Um `_repair_responsavel_cache` que roda
nessa janela, contra uma conversa `WAITING_HUMAN`/`HUMAN_ACTIVE`
divergente, viola o CHECK — `IntegrityError` no meio do `db.execute`, que
quebra a listagem inteira do inbox (não só aquela conversa), repetido a
cada ciclo de polling até o worker corrigir. **Este achado continua real**
— nada abaixo o desmente.

**Uma rodada de implementação chegou a corrigir isto** (pular conversas
com `state IN ('WAITING_HUMAN', 'HUMAN_ACTIVE')` dentro de
`_repair_responsavel_cache`) **e a correção foi REVERTIDA por decisão
explícita do usuário**, não por ter se provado errada tecnicamente. Motivo:
`conversations.py` é um dos arquivos com hash SHA-256 fixado (Família A) e
sob prova AST/grep (Família B.1) de que nenhum código V1 referencia
`state`/colunas V2 — uma garantia fundacional de todo o rollout faseado da
V2 (dry-run → shadow → canary → produção), estabelecida ANTES desta rodada
e fora do mandato desta rodada para alterar. A correção, embora tecnicamente
válida, fazia exatamente o que a Família B.1 existe para impedir: um writer
V1 passando a ramificar sobre estado V2. **Decisão**: `_repair_responsavel_cache`
permanece SEM a correção — `conversations.py` está de volta byte-a-byte ao
que a Família A/B.1 esperam, confirmado (`python tests/test_v2_v1_intacta.py`
verde, sem exceção nominal, sem hash atualizado). **R15/R16/R17/R-B NÃO
corrigem writers V1** — esse é o limite de escopo correto, e a promoção
deste item de "residual" (onde já estava, seção 10, item 3, desde a
primeira versão desta v4) para "correção obrigatória" foi um erro de
escopo desta rodada de implementação, não uma decisão do desenho original.

**Registrado explicitamente, não descartado**: o risco documentado acima
continua real e **bloqueante para canary/produção** — permanece sob o
guarda-chuva de R10 (writers V1 pré-existentes fora do alcance do CAS),
precisa de HOTFIX/desenho próprio antes de R10 poder ser considerado
fechado, e NÃO deve ser tratado como resolvido nem como bug oculto. Ver
seção 10, item 3, para o texto final do residual.

### 4.2 Partição de fila entre R15 e R16 — nenhuma das duas tem dono exclusivo hoje, precisa ter

A m019 (§8) cria UM índice compartilhado
(`(next_attempt_at) WHERE status IN ('REQUESTED','FAILED')`) e §5 define
`FOR UPDATE SKIP LOCKED` só para R16. Sem uma regra de partição explícita,
uma linha `FAILED`/`ESTADO_LOCAL_RECUSADO` — fonte de R15 Direção B — cai
TAMBÉM dentro da elegibilidade de R16, e as duas tentam corrigir o MESMO
lead com `event_id`s diferentes e intenções contraditórias (R16 quer
restaurar o alvo ORIGINAL; R15-B quer convergir para o dono LOCAL atual)
— nenhuma é idempotente em relação à outra, e o CAS decide por
escalonamento, não por desenho.

**Regra obrigatória**: R16 é elegível SOMENTE para
`status='REQUESTED'` OU (`status='FAILED'` AND `error_class IN
('NAO_APLICADO_RETRIAVEL','AMBIGUO_RETRIAVEL')` — coluna própria, §8/m019;
o `error_code` sozinho NÃO determina a classe, o mesmo token pode ser
retriável ou ambíguo dependendo do status que o originou). R15 é dono
exclusivo de `FAILED` com `error_code IN ('CRM_LEAD_JA_ATRIBUIDO', 'ESTADO_LOCAL_RECUSADO')`
— essas linhas NUNCA entram na fila de retomada simples de R16, porque
reenviar o `event_id` original (o que R16 faz) não é a correção certa para
elas; precisam da lógica de convergência de R15 (`operation_id` derivado,
não o `event_id` original). R15 reclama sua fatia pelo MESMO protocolo de
R16 (`FOR UPDATE SKIP LOCKED` + `next_attempt_at`), para que as duas nunca
disputem a mesma linha mesmo rodando no mesmo ciclo de 30s.

---

## 5. R16 — worker de retomada, com a correção de timing de `attempt_count`

Caminho de retomada inalterado: chama `handoff_to_human` diretamente, MESMA
identidade, sem LLM/triagem/`event_id` novo.

**ACHADO HIGH da revisão, corrigido**: a especificação original incrementava
`attempt_count` na MESMA transação curta que reclama a linha (antes da
chamada de rede) — mas `attempt_count < 5` é o gate de dead-letter,
tornando o contador uma variável de CORREÇÃO, não só de eficiência. Se o
worker morrer entre reclamar e efetivamente ligar para o CRM (crash loop,
OOM-kill, rollout), 5 reinícios consecutivos **esgotam o teto sem nunca
terem tentado uma única vez** — abandonando permanentemente um handoff que
nunca foi tentado, e classificado (pela taxonomia da seção 6) como
"definitivamente não aplicado" → vira TICKET, o sinal mais fraco,
exatamente para o caso mais urgente.

**Correção**: separar os dois papéis.
- **Transação curta de reclamação** (`FOR UPDATE SKIP LOCKED`): só adia
  `next_attempt_at` (serve exclusivamente para espaçamento/exclusão entre
  workers). NÃO incrementa `attempt_count`.
- **Transação de desfecho**, DEPOIS que a chamada de rede retornou
  (sucesso ou falha): incrementa `attempt_count` e grava o resultado.

Com isso, um crash entre a reclamação e a chamada de rede simplesmente
devolve a linha à fila no próximo ciclo, sem consumir orçamento de
tentativas — preservando "crash → volta à fila" sem o efeito colateral de
esgotamento fantasma.

**Especificação restante, inalterada**: processo interno separado, 30s,
`FOR UPDATE SKIP LOCKED` para exclusão entre instâncias (provado: 2
instâncias, partição correta, 0 sobreposição), backoff via
`clock_timestamp()` do BANCO (nunca `datetime.now()` do processo — fecha
R-E da v3), teto de 5 tentativas (agora contando só tentativas que
DE FATO chamaram a rede), alerta por classe (ambígua → página; terminal
→ ticket).

**ACHADO da revisão de fechamento — 429 queima orçamento de tentativas
que nunca foram julgadas pelo negócio**: a correção anterior (mover
`attempt_count` para a transação de desfecho) fecha "nunca tentou". Fica
aberto o espelho: um 429 (`CRM_LIMITE_TAXA`, §6.1) É uma tentativa que
tocou a rede — mas foi recusada pelo `SlowAPIMiddleware`, ANTES de
`aplicar_handoff` sequer rodar, sem que a lógica de negócio jamais tenha
visto a operação. Sob backlog grande (CRM voltando de indisponibilidade,
fila cheia), o worker pode sozinho gerar 5 ciclos de 429 consecutivos
contra o mesmo teto global (`200/min/IP`, compartilhado com todo o
tráfego Conversas→CRM) e mandar uma operação para dead-letter sem que
ela tenha sido genuinamente julgada nenhuma vez.

**Correção, duas camadas**:
1. **Orçamento próprio do worker, abaixo do teto do CRM**: o worker
   processa no máximo N operações por ciclo de 30s, dimensionado para que
   sua taxa de requisições fique confortavelmente abaixo de `200/min`
   mesmo sob backlog cheio (deixando margem para o resto do tráfego
   Conversas→CRM) — 429 vira anomalia genuína (outro tráfego saturando o
   teto compartilhado), não modo de falha rotineiro autoinduzido.
2. **Defesa em profundidade**: `CRM_LIMITE_TAXA`/429 especificamente é
   excluído do gate de dead-letter (não conta como tentativa julgada) —
   mesmo raciocínio já aplicado a "nunca tocou a rede": da perspectiva da
   operação, um 429 é estruturalmente idêntico a nunca ter sido tentada,
   só descoberto uma camada mais funda (gate de transporte, não ausência
   de chamada).

---

## 6. Reclassificação de erro do CRM — 4 classes, com as correções da revisão

Vocabulário mantido: `NAO_APLICADO_TERMINAL`, `NAO_APLICADO_RETRIAVEL`,
`AMBIGUO_RETRIAVEL`, `SUPERADO_TERMINAL`.

**Achados incorporados**:
- **429 → `CRM_LIMITE_TAXA`** (token explicitamente nomeado agora — a
  versão anterior classificava a CLASSE mas nunca cunhava o token
  concreto). `NAO_APLICADO_RETRIAVEL` — `SlowAPIMiddleware` roda ACIMA do
  router, o handler nunca executa (confirmado no código), e rate limiting
  é transitório por natureza.
- **6 subclasses de `httpx.RequestError` antes não classificadas**
  (`DecodingError`, `TooManyRedirects`, `ProxyError`, `UnsupportedProtocol`,
  `LocalProtocolError`, `CloseError`) — confirmadas como subclasses reais
  da hierarquia instalada (`httpx==0.28.1`). Classificadas
  conservadoramente como `AMBIGUO_RETRIAVEL` (preserva o comportamento
  atual do `except` genérico para esses casos raros). **Regra de
  implementação obrigatória**: os `except` precisam vir na ordem
  específico→genérico — uma cláusula de classe-base (`RequestError`)
  colocada ANTES de uma mais específica (`ConnectError` etc.) rouba a
  exceção e a mais específica nunca dispara.
- **Datas em corpo de resposta**: `current_owner_applied_at` vem de
  `LeadHandoffOperation.created_at` (`DateTime(timezone=True)`,
  hidratado pelo SQLAlchemy como objeto `datetime` vivo, não string).
  **`json.dumps` puro (usado pelo `JSONResponse` do Starlette, sem
  `default=`) não serializa `datetime` — estoura `TypeError` DENTRO do
  próprio handler de exceção**, que não é recapturado, degradando pra um
  500 genérico do Starlette e destruindo o corpo estruturado prometido
  exatamente no caso mais importante. **Correção obrigatória**: todo
  campo de data no corpo de erro precisa de `.isoformat()` explícito antes
  de entrar em `detail=`.

**Achado de arquitetura, corrigido — separação do dict de payload e do dict de eco em `_chamar_crm`**:
`_chamar_crm` hoje usa o MESMO dict `corpo` tanto para montar a requisição
quanto para validar o eco da resposta (`any(dados.get(chave) != valor for
chave, valor in corpo.items())`). Adicionar `expected_responsavel_id` a
esse dict sem excluí-lo da checagem de eco QUEBRA o cenário motivador
central deste documento: a resposta 200 nunca ecoa
`expected_responsavel_id` (seção 7), então `dados.get("expected_responsavel_id")`
é sempre `None`; se o valor enviado for não-nulo (exatamente o caso
"cliente que volta", `lead.responsavel_id` já existente), a checagem de
eco falha espuriamente e `_chamar_crm` reporta `_ERRO_RESPOSTA_INVALIDA`
mesmo com o CRM tendo aplicado corretamente — a conversa nunca vai para
`WAITING_HUMAN`, e o CRM diverge do Conversas. **Correção obrigatória**:
`expected_responsavel_id` entra no corpo da REQUISIÇÃO, mas o dict usado
para validar o ECO da resposta contém só `{event_id, lead_id,
target_user_id}` — os dois dicts precisam ser nomeados e mantidos
separados no código, não o mesmo objeto reaproveitado.

**Correção a uma afirmação da versão anterior deste documento**: o texto
anterior desta seção dizia que `422` precisava de discriminação por corpo
porque cobre "schema" e "usuário inativo" com classificações diferentes.
**Isto está errado, verificado contra o código real** (seção 6.1): as
duas causas de 422 desta rota colapsam na MESMA classe
(`NAO_APLICADO_TERMINAL`) e no MESMO token Conversas (`CRM_RECUSADO`) —
não precisam de tratamento diferente, então não precisam de discriminação
por corpo. **O único status HTTP desta rota que genuinamente precisa de
discriminação por `detail.error` é o 409** — ver seção 6.2. `404` também
tem uma causa só nesta rota (não duas, como o texto anterior sugeria por
analogia genérica) — ver seção 6.1.

**Tipo Python recomendado** (achado da revisão de contratos, com o
escopo completado nesta rodada): `_chamar_crm` passa a devolver um
`@dataclass(frozen=True)` + `StrEnum` local, definidos DENTRO de
`handoff.py` — seguindo o precedente já estabelecido no módulo IRMÃO
`pipeline.py:245,264` (`EtapaFinal`/`ResultadoPipeline`), não em
`contratos.py` (cujo próprio docstring documenta, e um teste AST protege,
que o módulo é deliberadamente inerte a import de httpx/SQLAlchemy/config
— um tipo carregando semântica de erro HTTP não pertence lá). Campos do
dataclass, seguindo o precedente de `ResultadoPipeline` (que carrega o
enum MAIS campos de contexto, não só o enum sozinho):
`error_code: TokenErroCrm | None`, `current_responsavel_id: int | None`,
`current_owner_event_id: str | None`, `current_owner_applied_at: str | None`
(já `.isoformat()`, nunca `datetime` cru — ver achado de datas acima).
Esses três últimos campos são só para o caminho de diagnóstico/log do
worker de reconciliação (§4) — `_registrar_falha` continua recebendo só
`error_code: str` como hoje (`handoff.py:572`); os campos extra NÃO
propagam para `HandoffOperation`/o banco, ficam só na memória da chamada
que os recebeu.

**Onde moram os tokens do lado CRM** — achado da revisão de contratos:
a recomendação acima cobre só o lado Conversas. Os tokens que o CRM
EMITE (`CREDENCIAL_INVALIDA`, `CHAMADOR_NAO_AUTORIZADO`, `LEAD_NOT_FOUND`,
`TARGET_USER_NOT_FOUND`, `TARGET_USER_INACTIVE`, `CRM_RECUSADO_CORPO`,
`HANDOFF_NAO_APLICADO`, `IDEMPOTENCY_PAYLOAD_CONFLICT`,
`LEAD_ALREADY_ASSIGNED` — seção 7) vivem em `app/routers/v2_lead_domain.py`,
um app Python 3.11 diferente, sem nenhum precedente de `StrEnum` local
hoje (todo `detail=` do arquivo é string solta — seção 6.1, Legenda D).
Esta implementação introduz o primeiro: um `StrEnum` local **dentro de
`v2_lead_domain.py`** (mesmo padrão de módulo-local que `pipeline.py`
já estabelece do lado Conversas — não um módulo `constants.py`
compartilhado, que este projeto não usa para este tipo de vocabulário).

---

## 6.1 Matriz exaustiva de erro — estado ATUAL, verificado linha a linha contra o código real

Produzida por leitura direta de `app/routers/v2_lead_domain.py` (`aplicar_handoff`
e todas as suas funções auxiliares), `app/auth.py`, `app/main.py`,
`app/limiter.py`, `conversas/app/v2/handoff.py` (`_chamar_crm`,
`_TOKEN_POR_STATUS`), os dois models de livro-razão, e o código-fonte
efetivamente instalado de `fastapi==0.115.x`, `starlette==0.38.6`,
`httpx==0.28.1` e `slowapi` — não por suposição. Cobre TODOS os status
pedidos (400, 401, 403, 404, 405, 409, 413, 415, 422, 429, 500, 502, 503,
504, 507), as 6 subclasses de `httpx.RequestError` já nomeadas mais a
hierarquia completa restante, e os 3 casos ambíguos (timeout ambíguo, 2xx
com corpo inválido, 2xx com eco incompatível). Onde um
status não é alcançável por esta rota hoje, a matriz diz isso
explicitamente em vez de inventar um cenário.

**Leitura importante antes da tabela**: isto descreve o comportamento
ATUAL do endpoint, pré-implementação desta arquitetura — não o
comportamento pretendido. A seção 6.2 lista exatamente o que muda.

### Legendas (evita repetir a mesma resposta linha a linha)

- **Legenda A — "Mantém REQUESTED?" é "Não" em toda linha.** `_registrar_falha`
  (`conversas/app/v2/handoff.py:572`), chamado dentro da MESMA execução de
  `handoff_to_human` que fez a tentativa, sempre resolve
  `REQUESTED → FAILED` antes de retornar. Uma linha só fica presa em
  `REQUESTED` se o PROCESSO morrer no meio da chamada de rede (kill, OOM,
  queda de energia) — nenhuma exceção/resposta HTTP desta matriz, por si
  só, produz esse rastro. É esse cenário de crash — não qualquer linha
  desta tabela — que o worker de retomada (R16) existe para encontrar.
  "Elegível para o worker pegar depois" (coluna 5) não é sinônimo de
  "ficou em REQUESTED": é "ficou em `FAILED` com um `error_code` que vale
  a pena reapresentar automaticamente".
- **Legenda B — "Vira FAILED?" é "Sim" em toda linha, com uma ressalva de
  corrida.** `_reservar_desfecho_ou_ceder_para_vencedor`
  (`conversas/app/v2/handoff.py:499-569`) reserva o desfecho de forma
  condicional (`UPDATE ... WHERE status != 'COMPLETED'`). Se outra
  tentativa concorrente já completou esta MESMA operação enquanto esta
  chamada estava em voo, o commit de `FAILED` é descartado e o resultado
  REAL é devolvido no lugar.
- **Legenda C — "`attempt_count` incrementa?" é "Sim" em toda linha desta
  matriz, COM UMA EXCEÇÃO** (a exceção é nova nesta rodada, seção 5 —
  429/`CRM_LIMITE_TAXA` NÃO conta, porque o `SlowAPIMiddleware` recusa
  ANTES de `aplicar_handoff` rodar; a lógica de negócio nunca julgou a
  operação, estruturalmente igual a "nunca chamou a rede", só descoberto
  uma camada mais funda). Em todas as demais entradas, a chamada de rede
  de fato aconteceu (resposta HTTP real ou exceção de rede genuína). A
  única outra saída de `_chamar_crm` que NÃO conta como tentativa é
  `_ERRO_SEM_CREDENCIAL` (retorna antes do `try`/`httpx.Client`) — fora do
  escopo desta matriz, mas é o próprio exemplo do repositório da regra
  "reclamação que nunca chamou a rede não conta". `attempt_count` ainda
  não existe como coluna hoje (seção 6.2) — a resposta "Sim"/exceção
  aplica a regra de desenho, não um comportamento observável no banco
  atual.
- **Legenda D — "Corpo do CRM hoje".** Nenhum `HTTPException` de
  `v2_lead_domain.py` usa `detail=` como dict — todos usam string simples,
  logo o corpo é `{"detail": "<string>"}`, exceto: o 422 de validação
  Pydantic (`detail` é uma LISTA) e o 429 (não passa pelo `HTTPException`
  do FastAPI — vem do `SlowAPIMiddleware`, formato raiz
  `{"error": "<string>"}`, sem `detail`). **Nenhuma resposta desta rota,
  hoje, carrega um `detail.error` estruturado** — a coluna 1 pedida
  originalmente é N/A em toda linha do estado atual. Ver seção
  6.2 para o que passa a existir.

### Autenticação/autorização real desta rota

`Depends(get_current_user)` — para uma chamada servidor-a-servidor, o
mecanismo real é `X-API-Key` (hash SHA-256 contra `User.api_key`).
Autenticar não basta: `_conferir_chamador_autorizado` exige que o
`User.id` resolvido seja exatamente `V2_LEAD_DOMAIN_SERVICE_USER_ID` —
qualquer outro usuário autenticado, mesmo admin, recebe 403. Rate limit:
só o teto GLOBAL `200/minute` por IP via `SlowAPIMiddleware` — não há
`@limiter.limit(...)` específico nesta rota.

**Correção de premissa, achada na revisão de segurança**: esta rota é
"servidor-a-servidor" por INTENÇÃO de projeto, não por isolamento de
rede — `docker-compose.yml:110-114` roteia o CRM publicamente pelo
Traefik (`Host(crm.crmbrasileirosnoatacama.cloud)`), sem `PathPrefix` nem
allowlist de IP específico para `/api/v2/lead-domain/*`. A garantia real
contra abuso não é a rota estar escondida — é a entropia da credencial
(`X-API-Key`, `secrets.token_urlsafe(48)` ≈ 384 bits) mais a ausência de
qualquer campo controlável pelo atacante que o servidor confirme
existência (username, e-mail) — o padrão clássico de enumeração via
401/403 não se aplica aqui por falta desse campo, não por a rota estar
inacessível.

### 1. Autenticação/Autorização

| Caso | Corpo CRM hoje | Token Conversas hoje | Classe interna | Elegível p/ worker? | Próxima ação | "Não aplicou" seguro? | Consultar canônico antes? |
|---|---|---|---|---|---|---|---|
| **401** — sem credencial reconhecida ou `X-API-Key` inválida | `{"detail": "Credenciais invalidas"}` | `CRM_NAO_AUTORIZADO` | `NAO_APLICADO_TERMINAL` | Não | Ticket humano | Sim | Não |
| **403** — autenticado, mas não é o usuário de serviço | `{"detail": "Acesso restrito ao usuario de servico do handoff"}` | `CRM_NAO_AUTORIZADO` (mesmo token do 401) | `NAO_APLICADO_TERMINAL` | Não | Ticket humano | Sim | Não |

401/403 são deterministicamente irrecuperáveis por retry mecânico — a
correção é humana (rotacionar `BIA_V2_CRM_API_KEY`, ou conferir se
`V2_LEAD_DOMAIN_SERVICE_USER_ID` ainda aponta para o usuário certo).
`_TOKEN_POR_STATUS` já funde os dois no mesmo token — a ação corretiva é
idêntica nos dois casos.

### 2. Validação de entrada e forma do corpo

| Caso | Corpo CRM hoje | Token Conversas hoje | Classe interna | Elegível p/ worker? | Próxima ação | "Não aplicou" seguro? | Consultar canônico antes? |
|---|---|---|---|---|---|---|---|
| **400** — corpo ilegível (fallback genérico do FastAPI) | `{"detail": "There was an error parsing the body"}` | `CRM_HTTP_ERRO` (fallback) | `NAO_APLICADO_RETRIAVEL` | Sim | Retry automático; se persistir, ticket | Sim | Não |
| **405** — verbo != POST | `{"detail": "Method Not Allowed"}` | `CRM_HTTP_ERRO` (fallback) | `NAO_APLICADO_TERMINAL` | Não | Ticket humano | Sim | Não |
| **413** — corpo grande demais | **Não alcançável hoje** — sem middleware de tamanho em `app/main.py` nem label Traefik | N/A | N/A | N/A | N/A | N/A | N/A |
| **415** — Content-Type não suportado | **Não alcançável hoje** — FastAPI não checa Content-Type antes de validar (verificado em `fastapi/routing.py`); cai em 422, não 415 | N/A | N/A | N/A | N/A | N/A | N/A |
| **422-schema** — `HandoffRequest` malformado (campo faltando, IDs ≤0, `event_id` inválido, campo extra) | `{"detail": [{...Pydantic...}]}` (lista, não string) | `CRM_RECUSADO` | `NAO_APLICADO_TERMINAL` | Não | Ticket humano | Sim | Não |

400 exige corrupção de bytes em trânsito (JSON sintaticamente inválido já
vira 422, não 400, por um `except json.JSONDecodeError` dedicado no
FastAPI) — não alcançável por uso correto OU defeituoso-plausível de
`_chamar_crm`, que sempre manda `json=corpo` limpo. 405 exige bug de verbo
no cliente ou reescrita por intermediário. 422-schema é quase sempre
sintoma de drift de contrato entre os dois serviços — a própria validação
do Conversas deveria impedir isso antes da chamada de rede.

### 3. Recurso não encontrado e conflitos de negócio

| Caso | Corpo CRM hoje | Token Conversas hoje | Classe interna | Elegível p/ worker? | Próxima ação | "Não aplicou" seguro? | Consultar canônico antes? |
|---|---|---|---|---|---|---|---|
| **404** — `lead_id` OU `target_user_id` não existe (causa única desta rota, não duas) | `{"detail": "Lead nao encontrado"}` ou `{"detail": "Usuario alvo nao encontrado"}` | `CRM_NAO_ENCONTRADO` | `NAO_APLICADO_TERMINAL` | Não | Ticket humano | Sim | Não |
| **409** — `event_id` já processado com parâmetros diferentes (única causa hoje — futura `IDEMPOTENCY_PAYLOAD_CONFLICT`, seção 6.2) | `{"detail": "event_id ja processado com parametros diferentes"}` | `CRM_CONFLITO` | `NAO_APLICADO_TERMINAL` | Não | Ticket humano | Sim | **Sim** |
| **422-inativo** — `target_user_id` existe mas `is_active=False` | `{"detail": "Usuario alvo esta inativo"}` | `CRM_RECUSADO` (mesmo token do 422-schema) | `NAO_APLICADO_TERMINAL` (só p/ retry automático — reativar é decisão humana) | Não | Ticket humano (reativar ou escolher outro responsável) | Sim | Não |

409 é o único caso do estado atual onde "consultar canônico antes" é
inequivocamente Sim: significa que as duas ledgers divergiram entre si
sobre os parâmetros do mesmo `event_id` — vale inspecionar com que
parâmetros o `event_id` foi de fato aplicado antes de decidir qualquer
coisa. `event_id` não é consumido nem por 404 nem por 422 — reenviar o
mesmo `event_id` depois de corrigida a causa continua válido, só que como
ação manual, não retry automático.

### 4. Erros de servidor e rede

| Caso | Corpo CRM hoje | Token Conversas hoje | Classe interna | Elegível p/ worker? | Próxima ação | "Não aplicou" seguro? | Consultar canônico antes? |
|---|---|---|---|---|---|---|---|
| **429** — rate limit global (200/min/IP) | `{"error": "Rate limit exceeded: 200 per 1 minute"}` — formato PRÓPRIO do slowapi, ver 6.2 | `CRM_HTTP_ERRO` hoje (fallback — token novo na seção 6.2) | `NAO_APLICADO_RETRIAVEL` | Sim | Retry automático com backoff | Sim | Não |
| **500** — `HandoffIndeterminado` OU exceção não tratada em outro ponto | `{"detail": "Nao foi possivel concluir..."}` OU texto puro `"Internal Server Error"` | `CRM_HTTP_ERRO` (fallback) | `AMBIGUO_RETRIAVEL` | Sim | Retry via R16, alerta de classe ambígua | **Não** | **Sim** |
| **502/503/504** | **Não gerados pelo código do CRM** — só alcançáveis se um proxy estiver no caminho; não confirmável a partir do repo se produção atravessa Traefik ou vai direto pela rede Docker (ver nota) | `CRM_HTTP_ERRO` (fallback) | 502/503 → `NAO_APLICADO_RETRIAVEL`; 504 → `AMBIGUO_RETRIAVEL` | Sim | 502/503: retry via R16; 504: Retry via R16, alerta de classe ambígua | 502/503: Sim; 504: Não | 502/503: Não; 504: Sim |
| **507** | **Não alcançável** — nenhum código do stack emite 507 (RFC 4918/WebDAV) | N/A | N/A | N/A | N/A | N/A | N/A |
| **`httpx.ConnectError` / `ConnectTimeout` / `PoolTimeout`** | N/A — nenhuma resposta recebida | `CRM_INDISPONIVEL` | `NAO_APLICADO_RETRIAVEL` | Sim | Retry automático | Sim | Não |
| **`httpx.ReadTimeout` / `WriteTimeout`** | N/A | `CRM_INDISPONIVEL` | `AMBIGUO_RETRIAVEL` | Sim | Retry via R16, alerta de classe ambígua | Não | Sim |
| **demais `httpx.RequestError`** (`DecodingError`, `TooManyRedirects`, `ProxyError`, `UnsupportedProtocol`, `LocalProtocolError`, `CloseError`, `ReadError`, `WriteError`, `RemoteProtocolError`) | N/A | `CRM_INDISPONIVEL` | `AMBIGUO_RETRIAVEL` | Sim | Retry via R16, alerta de classe ambígua | Não | Sim |

**Correção à versão anterior desta matriz — `InvalidURL` e
`CookieConflict` NÃO são subclasses de `httpx.RequestError`** (verificado
ao vivo contra `httpx==0.28.1`: ambas herdam direto de `Exception`, não
de `RequestError`/`HTTPError`). Um `except httpx.RequestError`, em
qualquer granularidade, não as alcança — por isso saíram da linha acima.
`InvalidURL` só é relevante se `CRM_BASE_URL` (env var não versionada em
produção — mesma incerteza da nota de infraestrutura abaixo) estiver
malformada; isso é um erro de CONFIGURAÇÃO, não uma falha de request
individual, e mais apropriado para uma checagem de sanidade na
inicialização do processo do que para o `except` desta função.
`CookieConflict` não é alcançável por este código hoje (`_chamar_crm`
nunca acessa `resposta.cookies[...]` por nome). Nenhuma das duas entra
no vocabulário de classificação desta matriz.

`_chamar_crm` hoje tem um ÚNICO `except httpx.RequestError` — todas as
variantes de rede caem no mesmo `CRM_INDISPONIVEL`, sem a granularidade
desta matriz (isso é o que a implementação precisa corrigir, não uma
inconsistência do desenho). 500 tem duas origens indistinguíveis por
status (`HandoffIndeterminado` deliberado vs. exceção não tratada em
`_carregar_lead`/`_carregar_usuario_ativo`/`_buscar_registro`, que rodam
fora de qualquer try/except) — como não dá para provar de fora qual
ocorreu, a linha inteira leva a classificação mais conservadora:
`AMBIGUO_RETRIAVEL`. `WriteTimeout` é `AMBIGUO_RETRIAVEL` por precaução
(não há garantia formal de que zero bytes chegaram ao servidor), enquanto
`ConnectError`/`ConnectTimeout`/`PoolTimeout` são seguros como
`NAO_APLICADO_RETRIAVEL` porque a conexão nunca chegou a se estabelecer.

**Nota de infraestrutura não confirmável a partir deste repositório**: em
dev, `CRM_BASE_URL` aponta direto para `127.0.0.1:8000` — sem proxy, então
502/503/504 são impossíveis nesse ambiente. Em produção, o valor real de
`CRM_BASE_URL` vem de uma env var não versionada; não é possível confirmar
pelo código se a chamada Conversas→CRM atravessa o Traefik do
`docker-compose.yml` ou vai direto pela rede Docker interna. **Isto não
bloqueia a implementação** — a classificação acima já cobre os três status
de forma conservadora independente da resposta — mas deveria ser
confirmado por quem administra o deploy antes de canary.

### 5. Casos ambíguos / 2xx inválido

| Caso | Corpo CRM hoje | Token Conversas hoje | Classe interna | Elegível p/ worker? | Próxima ação | "Não aplicou" seguro? | Consultar canônico antes? |
|---|---|---|---|---|---|---|---|
| **Timeout ambíguo** (moldura conceitual — une `ReadTimeout` e um eventual 504) | N/A | `CRM_INDISPONIVEL` ou `CRM_HTTP_ERRO` | `AMBIGUO_RETRIAVEL` | Sim | Retry via R16, alerta de classe ambígua | Não | Sim |
| **2xx com corpo JSON inválido/campos faltando** | 200 real do CRM — o que chega ao Conversas é que não é isso | `CRM_RESPOSTA_INVALIDA` | `AMBIGUO_RETRIAVEL` | Sim | Retry via R16, alerta de classe ambígua | **Não** | **Sim** |
| **2xx com eco incompatível** | Idem — 200 real, eco não bate | `CRM_RESPOSTA_INVALIDA` (mesmo token) | `AMBIGUO_RETRIAVEL` | Sim | Retry via R16, alerta de classe ambígua | **Não** | **Sim** |

A rota só constrói e devolve `HandoffResponse` DEPOIS de `db.commit()` —
não existe caminho onde o CRM devolve 200 sem ter committado. Um 200 com
corpo quebrado ou eco errado quase certamente significa que o commit
aconteceu e o problema é de transporte/serialização — tratar como "não
aplicou" seria exatamente o erro que a decisão de classificação existe
para evitar.

---

## 6.2 O que esta implementação muda — explícito, para não confundir "hoje" com "desenhado"

**429/`CRM_LIMITE_TAXA` resolvido**: a versão anterior deste
documento dava o exemplo `429 -> {"detail": {"error": "CRM_LIMITE_TAXA"}}`
como se o CRM fosse emitir esse corpo. **Isso não é necessário e não deve
ser implementado como tal** — o `SlowAPIMiddleware` roda ACIMA do
roteamento e nunca passa pela pilha de exceções do FastAPI; forçá-lo a
emitir um corpo estruturado exigiria envolver/substituir a resposta do
middleware, acoplando este mecanismo aos internals do slowapi para nenhum
ganho real. **`_chamar_crm` já despacha 401/403/404/422 só por status
code, sem nunca ler o corpo** — `CRM_LIMITE_TAXA` é simplesmente o valor
que `_TOKEN_POR_STATUS[429]` passa a ter no Conversas, no mesmo padrão dos
demais. O corpo real do CRM para 429 continua sendo o formato padrão do
slowapi (`{"error": "..."}` , sem `detail`) — isso é aceitável porque
nada depende de lê-lo.

**409 é o ÚNICO status que precisa de `_chamar_crm` passando a ler
`.json()` em resposta de erro** (mudança de comportamento real —
hoje `_chamar_crm` nunca chama `.json()` fora do caminho 200). Dois
sub-casos, discriminados por `detail.error`:
- **`IDEMPOTENCY_PAYLOAD_CONFLICT`** — o conflito que já existe hoje
  (`_conferir_parametros`, mesmo `event_id` com parâmetros diferentes).
  Muda o FORMATO do corpo (`detail={"error": "IDEMPOTENCY_PAYLOAD_CONFLICT",
  "current_responsavel_id": ..., "registered_lead_id": ...,
  "registered_target_user_id": ...}` em vez de string solta — os dois
  últimos campos respondem à pergunta que a nota da §6.1 já promete,
  "com que parâmetros o `event_id` foi de fato aplicado", ver adiante
  nesta seção); a classe (`NAO_APLICADO_TERMINAL`) e o token Conversas
  (`CRM_CONFLITO`, mantido) não mudam.
- **`LEAD_ALREADY_ASSIGNED`** — novo, com **dois emissores de semântica
  diferente** (correção da checagem final, achado G2 — a versão anterior
  descrevia só o primeiro): (a) a operação de primeira aplicação perdeu o
  CAS (seção 2.1.2) — `expected_responsavel_id` não bateu com o valor
  real sob lock; a escrita do chamador GENUINAMENTE não aconteceu. (b) um
  REPLAY (seção 2.1.1, Caso 2b) detecta que o dono mudou desde que este
  `event_id` aplicou pela primeira vez — aqui a escrita **aconteceu**, no
  passado (a linha do livro-razão é a prova), só foi superada por algo
  mais recente. Os dois casos usam o MESMO token e o MESMO
  `NAO_APLICADO_TERMINAL` — o comportamento resultante (não aplicar
  localmente, parar de tentar) é idêntico — mas "a escrita não
  aconteceu" só é literalmente verdade no caso (a); no caso (b) o certo é
  "esta operação específica não é mais a fonte de verdade". Corpo
  carregando `current_responsavel_id`/`current_owner_event_id`/`current_owner_applied_at`
  (seção 7) — nos dois casos; `current_owner_event_id`/`current_owner_applied_at`
  fazem sentido nos dois (quem tem o lead agora, e desde quando), mas
  `expected_responsavel_id` no corpo (seção 7) só é significativo no caso
  (a) — no caso (b) o CAS nunca chegou a ser avaliado, então ecoar esse
  campo no caso (b) não tem valor de diagnóstico; mantido no corpo por
  uniformidade de schema, não por ter sempre significado. **Token
  Conversas, nomeado explicitamente aqui (achado da
  revisão de fechamento — 3 dos 4 revisores independentemente notaram que
  a versão anterior nunca dava esse nome, e a seção 4 já assumia sua
  existência)**: `CRM_LEAD_JA_ATRIBUIDO`, DISTINTO de `CRM_CONFLITO` —
  os dois sub-casos do 409 precisam de tokens Conversas diferentes porque
  a seção 4 usa `CRM_LEAD_JA_ATRIBUIDO` como predicado de seleção da
  Direção A da reconciliação; se colapsassem no mesmo token que
  `IDEMPOTENCY_PAYLOAD_CONFLICT` (que é terminal-ticket, nunca
  reconciliação), essa seleção nunca encontraria nada e a Direção A
  ficaria morta em silêncio. Acrescentar `_ERRO_LEAD_JA_ATRIBUIDO =
  "CRM_LEAD_JA_ATRIBUIDO"` ao vocabulário fechado de `handoff.py:211-218`.

**Guard obrigatório para a leitura do corpo do 409** (achado da revisão
de fechamento, duas camadas de proteção que a versão anterior descrevia
só pela metade): `resposta.json()` sobre corpo não-JSON levanta
`json.decoder.JSONDecodeError`, que É `ValueError` — o MESMO tipo que o
caminho 200 já captura (`handoff.py:374-377`), sem exceção nova a
prever. Mas JSON *válido* com formato errado não é coberto por esse
guard sozinho: o 409 de HOJE (Legenda D, seção 6.1) tem `detail` como
STRING solta, não dict — `dados.get("detail", {}).get("error")` sobre
esse formato levanta `AttributeError` (`.get` num `str`), não capturado
em lugar nenhum. Alcançável em produção real: rollback do CRM depois do
Conversas já ter subido (fora da ordem recomendada, mas não impedido por
nada em runtime). **Correção**: `isinstance(dados.get("detail"), dict)`
explícito ANTES de acessar `.get("error")`, no mesmo espírito do guard já
existente no caminho 200 (`isinstance(dados, dict)`, `handoff.py:379`).
Só com os dois guards (`ValueError` E `isinstance`) o fallback
(`CRM_CONFLITO`, `NAO_APLICADO_TERMINAL`) é realmente alcançado em vez de
escapar cru. **Correção à justificativa** (achado G2 da checagem final —
o fallback continua certo, a razão dada para ele não cobria os dois
emissores de `LEAD_ALREADY_ASSIGNED` que a seção 6 agora documenta): um
409 sempre garante que a ESCRITA **desta chamada específica** não
aconteceu — verdade tanto quando o CAS recusa antes de qualquer commit
(emissor a) quanto quando um replay detecta staleness (emissor b, onde a
escrita aconteceu no PASSADO, sob outra chamada, e este 409 só reporta
que ela foi superada). Nos dois casos o chamador ATUAL não escreveu nada
nesta chamada — é isso que sustenta o fallback terminal, não a garantia
mais forte (e, no emissor b, falsa) de que "nenhuma escrita aconteceu em
momento nenhum". A ambiguidade de um corpo malformado aqui é só sobre o
MOTIVO exato, nunca sobre se ESTA chamada aplicou algo, então o fallback
terminal (não
`AMBIGUO_RETRIAVEL`) continua correto mesmo com o corpo quebrado.

**`SUPERADO_TERMINAL`/`CRM_OPERACAO_SUPERADA` — correção de mecanismo
em relação à v4 anterior**: o texto anterior implicava que o CRM emitisse
um token dedicado para "operação superada". **Isso é impossível de
implementar como descrito**: o CRM recebe um `event_id` opaco (UUID) e não
tem como saber, só a partir dele, se veio de uma tentativa de handoff
primária ou de uma correção de reconciliação (R15, direção B) — UUID5 não
é reversível, e o CRM não conhece as entradas candidatas para recomputar e
comparar. **Mecanismo corrigido**: o CRM só precisa saber emitir
`LEAD_ALREADY_ASSIGNED` (o MESMO token) sempre que o CAS falhar, não
importa quem perguntou. É o CONVERSAS — que sempre sabe se a chamada que
acabou de fazer foi uma tentativa de handoff primária ou uma correção de
reconciliação, porque foi ele quem escolheu qual fazer — que reinterpreta
essa MESMA resposta de forma diferente conforme o caminho de código que a
recebeu: no caminho de handoff primário, `LEAD_ALREADY_ASSIGNED` vira
ticket-se-necessário (comportamento já descrito); no caminho do worker de
reconciliação (direção B), a mesma resposta PODE ser lida como
`RECONCILIADO_JA_DIVERGIU` (seção 4) — informativa, não é erro — **mas
só sob uma condição que a versão anterior deste documento não
verificava** (achado HIGH da revisão de fechamento): um 409
`LEAD_ALREADY_ASSIGNED` para a correção — seja por perda de CAS na
primeira tentativa, seja por staleness detectada num retry/replay da
MESMA correção (§2.1.1) — só prova que o dono ATUAL do lead DIFERE do
que a correção esperava. NÃO prova que ele BATE com o que a correção
estava tentando estabelecer (`conversation.atendente_id`). Entre a
divergência original ser
detectada e a tentativa de correção rodar, um terceiro escritor — em
especial `sync_responsavel_to_crm`, que continua acessível pela UI
legada mesmo com a proibição da seção 4 (a proibição é só para o worker
de reconciliação, não remove o caminho para outros usos) — pode ter
movido `lead.responsavel_id` para um valor DIFERENTE tanto do antigo
quanto do que a correção buscava. Suprimir o ticket incondicionalmente
nesse caso esconderia uma divergência nova, permanentemente.

**Correção obrigatória**: antes de classificar como
`RECONCILIADO_JA_DIVERGIU`/suprimir o ticket, o worker de reconciliação
compara `current_responsavel_id` (do corpo do 409, seção 7) com
`conversation.atendente_id` (o valor local que ele estava tentando
estabelecer):
- **Batem** → convergência real, confirmada — suprime o ticket.
- **Não batem** → divergência NOVA, não resolvida — trata como
  `AMBIGUO_RETRIAVEL`/gera ticket, exatamente como qualquer outra
  inconsistência não explicada.

**Não existe token `CRM_OPERACAO_SUPERADA` do lado do CRM —
`SUPERADO_TERMINAL` é uma classificação aplicada só dentro do Conversas,
só dentro do código do worker de reconciliação, sobre uma resposta
`LEAD_ALREADY_ASSIGNED` comum, e só depois da comparação acima.** Isto
simplifica o contrato HTTP (2 tokens novos de 409, não 3) sem perder
nenhuma distinção de comportamento.

**Classificação fina de `httpx.RequestError`, confirmada, não é
inconsistência**: a distinção `NAO_APLICADO_RETRIAVEL` vs.
`AMBIGUO_RETRIAVEL` entre subclasses é trabalho NOVO desta implementação
— `_chamar_crm` hoje trata todas uniformemente. Esperado para um WP em
desenho; não aponta nenhum erro de especificação.

**Correção a uma afirmação da versão anterior desta seção** (achado
MEDIUM da auditoria de completude): o texto anterior dizia que o EIXO
"classe" (retriável vs. ambíguo) e o token Conversas persistido serem
independentes "pode ser intencional, não uma lacuna". **Deixou de ser
verdade assim que a regra de partição de fila entre R15/R16 (§4) foi
escrita nesta mesma rodada** — essa regra seleciona R16 por CLASSE, não
por token, então a classe PRECISA ser persistida para o predicado ser
expressável. Corrigido: `handoff_operations` ganha `error_class` (§8/m019),
gravada por `_registrar_falha` junto do `error_code`, na mesma transação.

**Ausência de endpoint de consulta separado, confirmada, deixada
explícita para não ser mal-lida depois**: não
existe, e esta implementação não cria, um endpoint de "consulta de status"
separado. "Consultar resultado canônico" (colunas com "Sim" na matriz da
seção 6.1) significa, na prática, reenviar o MESMO `event_id` (ou, no
caso de reconciliação, o `operation_id` derivado — seção 4) para este
MESMO endpoint — a idempotência de `aplicar_handoff` (replay via
`_buscar_registro`) É o mecanismo de leitura.

**Correção a uma afirmação imprecisa da versão anterior** (achado MEDIUM
da revisão de fechamento): esta seção dizia que isso "é suficiente porque
o endpoint já responde com o estado canônico completo... tanto no replay
quanto no conflito" — verdadeiro para `LEAD_ALREADY_ASSIGNED` (que
carrega `current_responsavel_id`/`current_owner_event_id`/`current_owner_applied_at`),
mas **não** para `IDEMPOTENCY_PAYLOAD_CONFLICT`: a nota da seção 6.1 para
essa linha promete que vale a pena "inspecionar com que parâmetros o
`event_id` foi de fato aplicado" — só `current_responsavel_id` (o dono
ATUAL do lead) não responde isso; a pergunta é sobre o registro do
livro-razão (`lead_id`/`target_user_id` gravados para aquele
`event_id`), não sobre o estado atual do lead. **Correção**: o corpo de
`IDEMPOTENCY_PAYLOAD_CONFLICT` (seção 7) também carrega
`registered_lead_id`/`registered_target_user_id` — dado que o próprio
chamador autenticado já tem direito de saber (é o registro DELE, sob o
`event_id` que ELE está reenviando), sem divulgação nova.

---

## 7. Contrato de resposta do CRM — com as correções de tipo e serialização

**Request**: `expected_responsavel_id: int | None = None` — **o `= None`
é obrigatório na assinatura Python, não apenas na prosa**. Achado HIGH da
revisão: `int | None` SEM o default, sob Pydantic v2 com `extra="forbid"`,
é um campo OBRIGATÓRIO-porém-anulável, não opcional — confirmado ao vivo
pelo revisor. Sem o `= None` explícito, todo payload antigo do Conversas
(que nunca manda essa chave) quebraria com 422 assim que o CRM subisse,
independente de qualquer ordem de deploy.

**Nota de runbook, obrigatória (achado da revisão de contratos)**: mesmo
com o default correto, um payload NOVO (Conversas já mandando o campo)
contra um CRM AINDA NÃO atualizado falha 422 em TODA chamada —
`extra="forbid"` não tem meio-termo. Isso não é só "ordem de deploy
preferida" (como a seção 8 da versão anterior descrevia) — é uma
INVARIANTE cujo modo de falha é apagão total do handoff se violada. O
runbook de deploy precisa nomear esse modo de falha explicitamente, não só
a ordem correta.

**200**: `{lead_id, target_user_id, event_id, replayed, current_responsavel_id}`.

**Não-2xx**, corpo estruturado, com datas sempre via `.isoformat()`:

```json
409 -> {"detail": {"error": "IDEMPOTENCY_PAYLOAD_CONFLICT", "current_responsavel_id": 10,
                   "registered_lead_id": 42, "registered_target_user_id": 7}}
409 -> {"detail": {"error": "LEAD_ALREADY_ASSIGNED", "current_responsavel_id": 10,
                   "expected_responsavel_id": null, "current_owner_event_id": "...",
                   "current_owner_applied_at": "2026-09-04T12:00:00+00:00"}}
500 -> {"detail": {"error": "HANDOFF_NAO_APLICADO"}}
404 -> {"detail": {"error": "LEAD_NOT_FOUND" | "TARGET_USER_NOT_FOUND"}}
401 -> {"detail": {"error": "CREDENCIAL_INVALIDA"}}
403 -> {"detail": {"error": "CHAMADOR_NAO_AUTORIZADO"}}
422 -> {"detail": {"error": "TARGET_USER_INACTIVE" | "CRM_RECUSADO_CORPO"}}
```

**429 fica de fora deste bloco de propósito**: seu corpo continua sendo o
formato nativo, não estruturado, do `SlowAPIMiddleware` — ver seção 6.2
para o porquê (nada no Conversas lê esse corpo; a classificação é
100% por status code). Listar `429 -> {"detail": {"error": "CRM_LIMITE_TAXA"}}`
aqui, como a versão anterior deste documento fazia, sugeria incorretamente
que essa string aparece no corpo real da resposta.

**Escopo explícito da migração para corpo estruturado**: hoje, TODOS os
`HTTPException` de `v2_lead_domain.py`/`_carregar_lead`/`_carregar_usuario_ativo`/
`_conferir_chamador_autorizado`/`_conferir_parametros` usam `detail=` como
STRING simples (seção 6.1, Legenda D) — nenhum já emite o formato acima.
Esta implementação precisa converter cada um desses call sites para
`detail={"error": "<TOKEN>"}` (mais os campos extra nos dois casos de 409).
Não é uma mudança nova além do já especificado — é tornar explícito que
"corpo estruturado" (regra do usuário: "nunca parsing de prosa") vale para
TODOS os não-2xx desta rota, não só os dois 409 novos. **Lista incompleta
na versão anterior (achado MEDIUM da revisão de fechamento)**: o 500 de
`HandoffIndeterminado` não é levantado dentro dessas funções — é
produzido por `_handoff_indeterminado_handler`
(`app/main.py:121-131`, um exception handler registrado na instância
FastAPI raiz, corpo hoje `{"detail": "Nao foi possivel concluir..."}`
string solta). Esse call site entra na mesma conversão. Impacto em
runtime é baixo (seção 6.1 confirma que nenhum código Conversas lê o
corpo do 500, só o status) — mas o exemplo `500 -> {"detail": {"error":
"HANDOFF_NAO_APLICADO"}}` acima só é honesto se este handler também for
convertido.

`current_responsavel_id` — **fonte única, documentada**: sempre uma
leitura fresca de `lead.responsavel_id` no momento da resposta, nos DOIS
casos de 409 (`IDEMPOTENCY_PAYLOAD_CONFLICT` e `LEAD_ALREADY_ASSIGNED`) —
nunca `registro.target_user_id` nem qualquer outro campo por engano
(achado LOW da revisão, fechado por especificação explícita).

**`current_owner_event_id` — ressalva de confiabilidade, documentada
(achado MEDIUM da revisão de arquitetura)**: é obtido por busca reversa
por VALOR (`lead_id`, `target_user_id=responsavel_id atual`) em
`lead_handoff_operations`, que é append-only e não tem coluna apontando
"esta é a operação que produziu o valor atual". Se o mesmo
`target_user_id` já foi atribuído a este lead mais de uma vez ao longo do
tempo por caminhos diferentes (V2 e depois V1 direto, que não grava no
livro-razão), a busca pode devolver a operação V2 antiga errada. **Isto é
aceitável** porque o campo é estritamente diagnóstico/auxiliar — o
Conversas nunca ramifica decisão sobre ele, só sobre `detail.error` — mas
a limitação precisa estar documentada no docstring da função que faz essa
busca, não perdida na implementação.

Campo `expected_responsavel_id` **nunca aparece no corpo de resposta de
sucesso** — ver seção 6 para por que isso importa para `_chamar_crm`.

---

## 8. Arquitetura recomendada — schema, com correções de escopo de transação

**Simplificação mantida**: índice único parcial do lado Conversas
descartado como invariante de correção. **Justificativa fortalecida pela
revisão de arquitetura** (positiva, não um achado a corrigir): o
"caso intercalado" que esse índice fechava na v3 já é fechado, de forma
sobredeterminada, por um mecanismo que existe por outro motivo —
`_registrar_falha` garante que a operação perdedora NUNCA aplica estado
local, e mesmo se aplicasse, `autorizar_envio` (Fase 7, `gate.py`) relê
`conversations` fresco e recusaria por 3 condições independentes
(`state_version` divergente, `state` divergente, `atendente_id is not
None`) antes de qualquer resposta da IA sair. Se mantido, o índice segue
opcional (evita 1 round-trip HTTP numa corrida rara), nunca obrigatório.

**Migrations** (2, aditivas — nomes corrigidos: são m018/m019, não seguem
um "molde de m017/m018" pré-existente como a versão anterior deste
documento dizia por engano — `m018` ainda não existe no repositório antes
desta implementação):
- **CRM (m018)**: `lead_handoff_operations` ganha
  `previous_responsavel_id INTEGER NULL`.
- **Conversas (m019)**: `handoff_operations` ganha
  `expected_responsavel_id INTEGER NULL`, `attempt_count INTEGER NOT NULL
  DEFAULT 0`, `next_attempt_at TIMESTAMPTZ NULL`, `reconciled_at TIMESTAMPTZ
  NULL`, **`error_class VARCHAR(32) NULL`** (achado MEDIUM da auditoria de
  completude — a regra de partição de fila do §4 seleciona R16 por
  CLASSE, `NAO_APLICADO_RETRIAVEL`/`AMBIGUO_RETRIAVEL`, mas o `error_code`
  sozinho não determina a classe: o MESMO token pode ser retriável ou
  ambíguo dependendo do status que o originou — `CRM_HTTP_ERRO` é
  retriável no 400, terminal no 405, ambíguo no 500; `CRM_INDISPONIVEL` é
  retriável em `ConnectError`/`PoolTimeout`, ambíguo em
  `ReadTimeout`/`WriteTimeout`. Sem uma coluna própria, o predicado do
  §4 não é expressável em SQL. `_registrar_falha` grava a classe junto
  do `error_code` a partir de agora, uma escrita a mais na mesma
  transação que já grava os dois), mais índice parcial
  `(next_attempt_at) WHERE status IN ('REQUESTED','FAILED')`.

**Transação do endpoint CRM** (ordem exata, seção 2.1.1 — a ordem real do
código, replay ANTES de validar lead/usuário, é PRESERVADA; a checagem
de staleness entra DENTRO do ramo de replay, não antes dele): autorização
→ busca do registro por `event_id`, SEM lock (não depende do lead) → se
registro EXISTE (replay): leitura fresca do lead SEM `with_for_update()`
(só leitura, não segue de escrita) → `_conferir_parametros` (409
`IDEMPOTENCY_PAYLOAD_CONFLICT` se não bater, AGORA já com
`current_responsavel_id` disponível — a leitura do lead precisa vir ANTES
desta checagem, não depois, senão o campo que a seção 7 promete "sempre
presente" fica ausente nesse caminho) → lead ausente: devolve o resultado
histórico mesmo assim (garantia do código real preservada) → lead
presente: compara `responsavel_id` fresco contra `registro.target_user_id`
→ bate: `200 replayed=true`; não bate: 409 `LEAD_ALREADY_ASSIGNED` (Caso
2b) → **nunca revalida usuário/422 no caminho de replay** → se registro
NÃO EXISTE (primeira vez): `with_for_update().populate_existing()` no
lead (404 se ausente) → `_carregar_usuario_ativo` (422 se
ausente/inativo) → CAS contra `expected_responsavel_id` (§2.1.2,
distinguindo campo ausente de `null` explícito via `model_fields_set`) →
`INSERT` no livro-razão (SAVEPOINT) → `UPDATE` do lead + `LeadHistory` →
**um** `db.commit()`. O `try/except SQLAlchemyError → HandoffIndeterminado`
envolve os DOIS ramos inteiros, do início ao fim — inclusive a leitura
SEM lock do ramo de replay, que é uma etapa de banco como qualquer outra
e merece o mesmo corpo estruturado de erro em caso de falha (não só o
INSERT do código atual, nem só "a etapa de lock" do caminho de primeira
aplicação).

**`lock_timeout` explícito, achado LOW da revisão de fechamento**: sem um
`lock_timeout` de sessão no lado CRM, uma transação esperando o lock pode
ficar viva no servidor bem além do timeout de leitura do cliente
(`httpx.Timeout(10.0, connect=5.0)`, `handoff.py:202`, contra
`statement_timeout` de 30-60s configurado no Postgres) — o cliente desiste
com `ReadTimeout` (classificado `AMBIGUO_RETRIAVEL`, correto) enquanto a
requisição do lado servidor SEGUE rodando (rota síncrona em threadpool,
sem cancelamento) e pode aplicar depois que o Conversas já decidiu tratar
como ambíguo. **Correção**: `SET LOCAL lock_timeout = '3-4s'` (escopo de
TRANSAÇÃO, nunca `SET` puro — a conexão é reciclada por um pool,
`app/database.py:15-17`, `pool_size=10`+`max_overflow=20`; um `SET`
sem `LOCAL` vazaria o timeout para a PRÓXIMA request que reusar essa
conexão do pool), MENOR que o timeout de leitura do cliente, para que
uma perda de corrida pelo lock falhe rápido e deterministicamente do lado
servidor em vez de arriscar aplicar depois que o cliente já desistiu.

**PostgreSQL × SQLite — achado corrigido, mais grave que a versão
anterior descrevia**: a versão anterior deste documento afirmava que `FOR
UPDATE` sob SQLite produz `OperationalError: database is locked`. **Isso
está errado, e a realidade é pior**: o dialeto SQLite do SQLAlchemy
**descarta silenciosamente a cláusula `FOR UPDATE`** — não emite erro
nenhum. Provado: 15/15 rodadas concorrentes produziram sobrescrita
silenciosa, ZERO exceções de qualquer tipo. **Consequência para o teste de
aceitação**: NÃO pode seguir o molde de skip-gracioso de
`tests/test_v2_r12_postgres_proof.py` (que sai 0 sem `DATABASE_URL_*`) —
isso produziria um gate INERTE que nunca roda no CI (que é SQLite-only) e
nunca protegeria contra uma regressão do achado da seção 2.2. **Regra
obrigatória**: o teste de aceitação dos Casos 4/5 precisa FALHAR (não
pular) quando PostgreSQL não estiver disponível, ou o CI precisa
provisionar PostgreSQL especificamente para esse job.

**Sequenciamento de deploy** (inalterado, com a invariante de
`extra="forbid"` agora nomeada explicitamente — seção 7): migrations →
CRM → Conversas → worker por último.

**Backward compatibility**: inalterada (campo opcional, resposta aditiva,
corpo de erro não lido hoje por `_chamar_crm`).

---

## 8.5. Teste E2E distribuído — atualização da definição (cenários I-M)

Infraestrutura inalterada (FastAPI Conversas e CRM reais, PostgreSQL 16
efêmero para cada app, HTTP real). **Critério global, válido para A-M**:

> Ao final de QUALQUER cenário que termine em sucesso:
> `CRM.leads.responsavel_id == Conversas.conversations.atendente_id ==
> Conversas.conversations.responsavel_id`.

- **I** — 2 requisições HTTP reais concorrentes, mesmo `lead_id`,
  `event_id`/`target_user_id` diferentes. Exatamente uma aplica; a outra
  recebe `409` real com `current_responsavel_id`.
- **J** — 3 operações concorrentes, mesmo `lead_id`. Exatamente um
  vencedor.
- **K** — retry após timeout ambíguo em que o CRM já commitou. Retry
  recebe `replayed=true` com o mesmo target já aplicado.
- **L** — nova atribuição depois de release legítimo real. Aceita pela
  regra de baseline, não por tempo.
- **M** — NOVO (fecha o HIGH da revisão de fechamento, §2.1.1/Caso 2b):
  operação E aplica no CRM (lead → T); aplicação local falha por um
  motivo classificado `NAO_APLICADO_RETRIAVEL`/`AMBIGUO_RETRIAVEL` —
  **não** `ESTADO_LOCAL_RECUSADO` (essa classe é exclusiva de R15 pela
  regra de partição do §4; usar um motivo genérico/retriável mantém R16
  como dono legítimo desta linha, sem contradizer a partição); lead é
  legitimamente reatribuído a U (via Caso 6 ou rota V1); worker R16
  (dono legítimo desta classe) reenvia o `event_id` de E. Resultado
  esperado: `409 LEAD_ALREADY_ASSIGNED` com `current_responsavel_id=U`,
  NÃO `200 replayed=true` com `target_user_id=T` — e
  `_aplicar_estado_local` do lado Conversas NUNCA roda para esse replay.
  Sem a correção da seção 2.1.1, este cenário aplicaria T localmente
  enquanto o CRM já tem U — violação silenciosa do critério global.

Cenários A-H (v3) permanecem válidos. **G muda de "esperado vermelho" (v3,
provando que R-B estava aberto) para "esperado VERDE"** com esta
arquitetura implementada corretamente — incluindo a correção da seção
2.3, sem a qual G continuaria vermelho de um jeito mais sutil (aplicaria
sem erro, mas incorretamente).

**Teste de regressão específico, obrigatório, além de A-M**: uma variante
do mecanismo de lock SEM `populate_existing()`/`refresh()` explícito
precisa ser rodada contra o MESMO cenário de concorrência e PROVAR que
falha — não basta um teste que só valida o caminho correto (ver seção
2.3).

---

## 9. Prova empírica — ver `bia-v2-rb-proof.md`, atualizado com a re-verificação da seção 11.2

---

## 10. Residuais nomeados (item 3 revertido para residual puro, item 4 ajustado, item 7 novo — ver notas)

1. R10 permanece fora do escopo — confirmado pelo teste de deadlock: CAS
   protege V2×V2, não protege contra um writer V1 sobrescrevendo uma
   decisão V2 já commitada.
2. `ON DELETE SET NULL` em `users` — writer invisível ao mecanismo,
   mencionado, não corrigido.
3. `_repair_responsavel_cache` — writer V1 pré-existente, corre sem lock
   contra o novo mecanismo, **e viola o CHECK de estado humano durante a
   janela de divergência que R15 torna rotineira** (achado real, seção
   4.1). Uma correção chegou a ser implementada e foi **revertida por
   decisão explícita** — o arquivo (`conversas/app/routers/conversations.py`)
   é protegido por Família A (hash) e Família B.1 (prova de que V1 nunca
   referencia estado V2), estabelecidas antes desta rodada e fora do
   mandato desta rodada para alterar. Continua **residual, sob R10**,
   **bloqueante para canary/produção**, e precisa de HOTFIX/desenho
   próprio — não corrigido por R15/R16/R17/R-B, por desenho.
4. Risco ABA residual — **nota de honestidade, achado LOW da auditoria de
   completude**: a mitigação por teto de tentativas ficou mais fraca do
   que a versão anterior descrevia, não mais forte — as correções do §5
   (incrementar `attempt_count` só no desfecho, e excluir 429 do gate)
   afrouxam o teto de propósito, então ele protege menos casos do que
   antes dessas correções, ainda que por boas razões (evitar dead-letter
   de operações nunca julgadas).
5. Decisão de negócio sobre lead pré-atribuído/conversa reaberta — tornada
   visível, não criada, por este desenho. **Confirmado pela revisão de
   arquitetura como o escopo correto** (resolver tecnicamente seria
   mudança de comportamento não solicitada). Pontos de código relevantes
   para quando a decisão vier: `estados.py:313-320` (branch `AI_TRIAGE` não
   valida `responsavel_id`) e `webhook.py:643-650` (reabertura não zera
   `responsavel_id`).
6. Premissa de banco compartilhado — a leitura de baseline reusa
   `crm.get_leads_responsaveis`, que lê o banco do CRM diretamente. **Achado
   de segurança incorporado**: essa mesma equivalência de acesso é o que
   torna o corpo do 409 seguro (não concede nada que o chamador já
   autenticado não tivesse por outra via) — mas isso deixaria de valer se
   os bancos fossem um dia separados, ponto em que o corpo do 409
   precisaria ser reavaliado como divulgação de primeira classe.
7. **NOVO nesta rodada (checagem final, achado G4)** — a proteção do
   replay contra staleness (Caso 2b, §2.1.1) é garantia forte só no
   cenário SEQUENCIAL (um release já commitado antes do replay começar,
   exatamente o que o E2E M exercita). Sob concorrência GENUÍNA entre um
   replay (sem lock de escrita, de propósito) e uma primeira aplicação em
   voo para o mesmo lead, o replay pode responder `200` com um valor que
   se torna stale milissegundos depois — nenhuma forma de lock de linha
   segura através de uma resposta HTTP já enviada, então isto não é um
   defeito da implementação do lock, é um limite estrutural de qualquer
   desenho request-response sem 2PC entre os dois serviços. Mitigado, não
   eliminado, pelo próprio R16 (uma retomada subsequente do mesmo
   `event_id` pegaria a divergência na rodada seguinte) e por
   `autorizar_envio` (gate independente do lado Conversas, §8). Não é
   Caso enumerado em §3 — a janela é estreita (duração de uma transação
   de leitura) e a consequência já é coberta pelo mesmo mecanismo de
   detecção de divergência que outras partes deste desenho usam.

---

## 11. Revisão do desenho — trilha completa desta rodada

### 11.1 — As 4 revisões despachadas, e o que cada uma corrigiu

- **`database-architect` fresco (concorrência)** — **BLOQUEOU** a primeira
  versão. F1 (CRITICAL): a leitura sob lock era um no-op silencioso pelo
  motivo da seção 2.2 — CORRIGIDO (seção 2.3). F2 (HIGH):
  `attempt_count` incrementado cedo demais queimava o teto de retry sem
  nunca tentar — CORRIGIDO (seção 5). F3 (HIGH): a afirmação sobre SQLite
  estava factualmente errada (é silencioso, não barulhento) —
  CORRIGIDO (seção 8), com a regra de que o teste de aceitação não pode
  usar skip-gracioso. F4 (MEDIUM): `operation_id` de R15 podia colidir
  com um conflito terminal permanente sob mudança de alvo — CORRIGIDO
  (seção 4). F5 (LOW): namespace do `uuid5` não especificado — CORRIGIDO
  (seção 4, constante nomeada). Confirmou como corretas, sem ressalva: o
  dilema H1/H2, a ordem relativa lock→re-check→CAS, Caso 6, concorrência
  entre `lead_id`s diferentes, exclusão do worker via `SKIP LOCKED`,
  ausência de colisão do `uuid5` (100k amostras).
- **`code-reviewer` (arquitetura)** — 1 finding HIGH: `_chamar_crm`
  reusa o mesmo dict pra requisição e validação de eco, e adicionar
  `expected_responsavel_id` sem separar os dois quebraria o cenário
  motivador central — CORRIGIDO (seção 6). 1 MEDIUM: confiabilidade de
  `current_owner_event_id` sob histórico misto — documentado como ressalva
  (seção 7), não é erro, é limite conhecido de um campo diagnóstico. 3
  LOW — nomenclatura de migration, escopo do `try/except`, menção a
  `_conferir_parametros` — todos corrigidos. Confirmou, com evidência de
  código nova (não só concordância): por que descartar o índice do lado
  Conversas é seguro (cadeia `_registrar_falha`→`autorizar_envio`), por
  que `selecionar_atendente` continua intocado, por que o token
  descartado é corretamente inobservável nesta arquitetura.
- **`security-reviewer`** — 1 finding MÉDIO: a direção B de R15 não
  nomeava o mecanismo de escrita, e o repositório já tem um atalho
  (`sync_responsavel_to_crm`) que contornaria todo o CAS — CORRIGIDO
  (seção 4, proibição explícita). Demais perguntas (enumeração via
  `expected_responsavel_id`, superfície do worker, vocabulário dos
  tokens) sem achado — confirmado que a mesma identidade já tem acesso
  equivalente por vias pré-existentes, e que o worker não introduz
  superfície HTTP nova.
- **`python-reviewer` (contratos)** — 2 findings HIGH verificados ao vivo
  contra as bibliotecas reais instaladas: `int | None` sem `= None` quebra
  todo payload antigo (Pydantic v2, testado) — CORRIGIDO (seção 7);
  `datetime` cru em `HTTPException(detail=...)` quebra a serialização do
  Starlette e degrada pra 500 — CORRIGIDO (seção 6, `.isoformat()`
  obrigatório). Mais MEDIUM (tipo `@dataclass`+`StrEnum`, token de 429
  não nomeado, 6 subclasses de `httpx.RequestError` sem classificação +
  ordem dos `except`) e LOW (constraint `gt=0`, nome de coluna
  divergente entre a prova e o modelo real, fonte única de
  `current_responsavel_id`) — todos incorporados.

### 11.2 — Re-verificação dedicada da correção do achado CRITICAL (F1)

Ver `bia-v2-rb-proof.md`, seção "Re-verificação dedicada", para o
ambiente, os comandos exatos e a saída bruta completa. Resumo
quantitativo: em container PostgreSQL 16 efêmero dedicado (`rb_refix_verify`,
distinto do `rb_pg16_proof` da prova P1-P8), as duas formas corrigidas
(`.with_for_update().populate_existing().first()` como query única, e
`db.refresh(lead, with_for_update=True)` a partir de uma pré-carga)
deram **0 falhas em 50 rodadas cada** (40 com 2 threads +
`threading.Barrier`, 10 com 3 processos Python reais) — **100/100
rodadas corrigidas sem nenhuma falha**. A forma SEM a correção
(pré-carga + `.with_for_update().first()` sem `populate_existing()`),
testada em paralelo no MESMO container e schema, falhou em **50/50
rodadas** (40/40 com threads, 10/10 com processos), com sobrescrita
silenciosa do dono do lead e o livro-razão acumulando 2-3 linhas por
lead — nenhuma exceção em nenhum dos casos, confirmando que a corrupção é
lógica (lost update), não um erro que apareceria sozinho em teste.

Um experimento de controle determinístico, sem corrida (duas sessões
sequenciais, sem competir por lock), isolou a causa: o `FOR UPDATE` **é**
genuinamente emitido e o lock **é** genuinamente tomado na forma quebrada
— não é um caso de lock ausente — e mesmo assim o SQLAlchemy devolve o
objeto do identity map com o valor obsoleto (`obj_value=100` contra um
`db_value=999` já commitado, lido na MESMA conexão sob o MESMO lock).
`.populate_existing()`/`refresh()` é a variável isolada que muda o
desfecho, sem nenhum outro fator envolvido.

**Nota de precisão, registrada sem suavizar**: os números informais usados
para orientar o despacho desta verificação (~39/40, 8/8) eram uma
paráfrase da descrição textual do achado original da revisão, não uma
re-medição numérica própria. O resultado real observado foi mais
consistente, não menos — 40/40 e 10/10 (com 3 sucessos lógicos por rodada
no caso de 3 processos, não 2) — por uma causa identificada (a barreira
desta rodada força todos os workers a já carregarem o snapshot obsoleto
antes de disputar o lock, uma janela de colisão estruturalmente maior).
Isso não muda a conclusão — torna-a mais forte.

### 11.3 — Passe final de closure sobre os bytes já corrigidos (rodada seguinte, depois da matriz exaustiva de erro)

Depois de: (a) corrigir a contradição de status do documento, e (b)
acrescentar a matriz exaustiva de erro (seções 6.1/6.2), o documento foi
submetido a uma SEGUNDA rodada dos 4 revisores obrigatórios — pergunta
única, fechamento, não descoberta aberta. Instrução explícita a cada um:
tratar o mecanismo central de CAS (seções 1-5, 8) como fechado pela
rodada anterior, e focar nas seções novas.

- **`database-architect` (concorrência)** — **NÃO LIMPO**: 3 HIGH, 5
  MEDIUM, 2 LOW, nenhum reabrindo o CAS em si. O mais grave: o caminho de
  replay (evento já visto) não re-checava o dono do lead sob lock antes
  de devolver o resultado histórico — inofensivo isoladamente, mas R16
  (workflow novo desta mesma rodada) sistematiza justamente o reenvio de
  `event_id`s antigos, então um release/reatribuição legítima entre a
  aplicação original e a retomada faria o Conversas aplicar um alvo já
  superado — CORRIGIDO nesta rodada (seção 2.1.1, Caso 2b, cenário E2E M)
  **com uma correção que, ela mesma, tinha uma regressão — ver §11.5,
  não "sob o mesmo lock" como esta entrada descrevia originalmente**.
  Segundo: `_repair_responsavel_cache`
  (código pré-existente, `conversations.py`) violaria o CHECK de estado
  humano exatamente na janela operacional que R15 torna rotineira — a
  correção proposta na ocasião (pular reparo em estados humanos) foi
  implementada, mas **revertida por decisão explícita do usuário na fase
  de implementação**: `conversations.py` está sob Família A/B.1
  (V1 nunca referencia estado V2), fora do mandato desta rodada. Achado
  continua real, permanece residual sob R10 (seção 4.1, seção 10 item 3)
  — NÃO corrigido, por decisão de escopo, não por falta de solução.
  Terceiro (confirmado por mais 2 revisores independentemente): o token
  Conversas de `LEAD_ALREADY_ASSIGNED` nunca era nomeado, quebrando o
  predicado de seleção da Direção A de R15 — CORRIGIDO (`CRM_LEAD_JA_ATRIBUIDO`,
  seção 6.2). MEDIUM: guard do corpo do 409 incompleto para `detail`
  como string — CORRIGIDO; R15/R16 sem partição de fila — CORRIGIDO
  (regra de exclusividade por `error_code`/`error_class`, seção 4.2); rótulo
  "Reconciliação" sem mecanismo próprio para a classe ambígua —
  CORRIGIDO (renomeado para apontar ao R16); corpo do
  `IDEMPOTENCY_PAYLOAD_CONFLICT` não entregava o que prometia —
  CORRIGIDO (`registered_lead_id`/`registered_target_user_id`); 429
  podia queimar orçamento de tentativas nunca julgadas pelo negócio —
  CORRIGIDO (self-pacing do worker + exclusão do gate). LOW: contradição
  §3/§8 sobre lock no replay — **NÃO resolvida nesta rodada como esta
  entrada afirmava originalmente**: só §8 tinha sido atualizado; §3
  (Casos 2/2b) continuou com o texto antigo até a auditoria de completude
  pegar (§11.5); `lock_timeout` ausente permitia aplicação tardia depois
  do cliente desistir — CORRIGIDO (revisado de novo em §11.5 para
  `SET LOCAL`, não `SET`).
- **`code-reviewer` (arquitetura)** — **WARNING**: 2 HIGH, 1 MEDIUM, 1
  LOW. O mais sutil: a lógica de supressão de ticket para
  `SUPERADO_TERMINAL` inferia "convergência real" só da falha do CAS —
  mas uma falha de CAS só prova que o dono mudou, não que mudou PARA o
  valor que a correção esperava; um terceiro escritor (`sync_responsavel_to_crm`,
  ainda acessível fora do worker de reconciliação) podia produzir uma
  divergência NOVA que ficaria silenciada para sempre — CORRIGIDO
  (comparação obrigatória de `current_responsavel_id` contra
  `conversation.atendente_id` antes de suprimir, seção 6.2). Segundo
  (mesmo achado dos outros 2 revisores): `CRM_LEAD_JA_ATRIBUIDO` nunca
  nomeado. MEDIUM: semântica de `expected_responsavel_id=None` nunca
  mostrada em código, com uma janela de regressão de compatibilidade real
  (Conversas antigo + CRM novo faria toda operação sobre lead já-atribuído
  falhar CAS espuriamente) — CORRIGIDO (seção 2.1.2, distinção
  campo-ausente vs. `null` explícito via `model_fields_set`, com código
  exato). Confirmou como corretas as 3 outras perguntas feitas
  (irreversibilidade do UUID5, escopo real da migração para corpo
  estruturado, assimetria são do 429) e um LOW de rotulagem
  (F1/F2/F4 sem numeração correspondente) — CORRIGIDO (rótulos renomeados).
- **`security-reviewer`** — primeiro despacho devolveu um achado real mas
  fora de escopo (exposição de PII em log via `logging_config.py`/Fase 6
  — redescoberta independente do risco R11, já rastreado separadamente na
  matriz de prontidão; não dobrado neste documento, que fecha só
  R-B/R15/R16/R17). Um segundo despacho, re-perguntando as 4 questões
  originais de forma explícita e restrita à rota/mecanismo desta rodada,
  devolveu **LIMPO** nas 4 — com uma correção de premissa (a rota é
  "servidor-a-servidor" por intenção de projeto, não por isolamento de
  rede — o Traefik expõe o host publicamente; a garantia real é a entropia
  da credencial, não a rota estar escondida) — CORRIGIDA (seção 6.1).
- **`python-reviewer` (contratos)** — **NÃO LIMPO**: 2 HIGH, 3 MEDIUM.
  Confirmou o mesmo achado de `CRM_LEAD_JA_ATRIBUIDO` (terceira
  confirmação independente). Achado próprio, verificado ao vivo contra
  `httpx==0.28.1` instalado: `InvalidURL` e `CookieConflict`, que a matriz
  da rodada de drafting tinha listado como subclasses de
  `httpx.RequestError`, na verdade herdam direto de `Exception` — um
  `except httpx.RequestError` nunca as alcança — CORRIGIDO (removidas da
  linha, nota própria explicando por que não pertencem ao vocabulário de
  classificação desta matriz). MEDIUM: guard de `.json()` no 409
  incompleto para corpo com forma errada (mesmo achado do revisor de
  concorrência, ângulo diferente) — CORRIGIDO junto; local dos tokens do
  lado CRM e forma exata do dataclass do lado Conversas nunca
  especificados — CORRIGIDO (seção 6, parágrafo de tipo Python
  expandido); produtor real do 500 (`_handoff_indeterminado_handler`,
  `app/main.py`) faltando na lista de call sites a converter —
  CORRIGIDO. Re-verificou ao vivo, sem regressão, os 2 achados HIGH já
  fechados na primeira rodada (`= None` obrigatório, `.isoformat()`
  obrigatório) — inclusive com um round-trip completo via `TestClient`
  real do FastAPI para o segundo.

### 11.4 — Nota sobre convergência independente entre revisores

O achado `CRM_LEAD_JA_ATRIBUIDO` foi levantado, de forma independente e
sem se verem, por 3 dos 4 revisores desta segunda rodada — cada um
chegando lá por um caminho diferente (grep de token inexistente, leitura
cruzada §4×§6.2, predicado de seleção morto). Registrado aqui não como
prova de correção automática (não é), mas como o tipo de sinal que este
processo todo tem tratado como forte: quando revisores independentes,
com prompts e ângulos diferentes, convergem no MESMO ponto sem
coordenação, a probabilidade de ser um artefato do prompt (em vez de um
defeito real do documento) cai bastante.

### 11.5 — Auditoria de completude sobre as correções da segunda rodada (terceira rodada, não é revisão de descoberta)

Depois de aplicar manualmente todas as correções da §11.3 (~16 achados,
seções 2, 3, 4, 5, 6, 6.1, 6.2, 7, 8, 8.5, 11, 13 tocadas), a extensão e
interdependência das mudanças justificava uma verificação dedicada de
que cada correção genuinamente aconteceu (não só foi prometida em texto)
e de que nenhuma quebrou uma referência cruzada em outro lugar — não
descoberta aberta, uma auditoria de completude. Despachados 2 verificadores
em paralelo, cada um com a lista exata dos achados a confirmar:

- **`database-architect` (concorrência)** — confirmou os 11 achados de
  concorrência/contrato genuinamente corrigidos (não só prometidos), mas
  encontrou **14 inconsistências novas**, a maioria causada pelas
  próprias correções. A mais séria, ÚNICA tratada como bloqueante: a
  correção do replay (§2.1.1) tinha movido a consulta ao livro-razão para
  DEPOIS da query com lock — o que colocava 404/422 ANTES do replay,
  reabrindo (por uma porta diferente da original) o exato bug que um
  comentário de 35 linhas em `app/routers/v2_lead_domain.py:205-239`
  documenta e existe para evitar: um replay de operação já aplicada com
  sucesso não pode virar erro só porque o alvo mudou de estado DEPOIS.
  **CORRIGIDO nesta rodada** (§2.1.1 reescrita: a ordem real do código —
  replay antes de validar lead/usuário — é preservada; a checagem de
  staleness entra DENTRO do ramo de replay, como uma leitura sem lock de
  escrita, com um sub-caso de degradação graciosa para lead apagado
  desde então). Mais 4 MEDIUM: o cenário E2E M usava uma classe de erro
  (`ESTADO_LOCAL_RECUSADO`) que a própria regra de partição nova
  reservava para R15, contradizendo o resto do cenário (que dizia R16
  reenviar) — CORRIGIDO (classe genérica retriável no setup); a regra de
  partição de R16 selecionava por CLASSE sem nenhuma coluna que
  persistisse a classe (o mesmo `error_code` pode ser retriável ou
  ambíguo dependendo do status de origem) — CORRIGIDO (`error_class`
  acrescentada à m019); um residual da §10 ainda dizia "não corrigido"
  sobre o que virou pré-requisito de canary — CORRIGIDO (item cindido:
  corrida sem lock continua residual, violação de CHECK saiu de
  residual); uma célula da tabela §4 mostrava a supressão de ticket como
  incondicional, antes da correção do HIGH de supressão ter sido
  aplicada a ela — CORRIGIDO (célula dividida em 2, com a condição
  explícita). Mais achados LOW, todos incorporados: legenda
  desatualizada sobre o 429, snippet de código mostrando a forma ingênua
  do CAS em vez de apontar para a versão corrigida, exemplo de corpo
  JSON sem os campos novos, contagem "26 linhas" frágil a edição futura,
  cabeçalho "6 casos" desatualizado, "A-L" não atualizado para "A-M",
  referência de seção imprecisa, e o gap de numeração 11.3/11.4 (este
  próprio documento não tinha essas duas subseções — resolvido
  renumerando esta trilha inteira sequencialmente).
- **`code-reviewer` (contratos/nomenclatura)** — confirmou 6 dos 8
  achados de contrato genuinamente corrigidos. Achou 1 correção
  INCOMPLETA: das 6 linhas `AMBIGUO_RETRIAVEL` da matriz cujo rótulo de
  próxima-ação precisava apontar para R16, uma sétima linha relacionada
  (o sub-caso 504 dentro da linha combinada "502/503/504") tinha ficado
  para trás com o rótulo antigo "reconciliação" — **CORRIGIDO** (mesma
  troca aplicada a essa célula). Achou 2 problemas de nomenclatura: uma
  referência cruzada apontando para "seção 6" quando o conteúdo citado
  mora na seção 7 — **CORRIGIDO**; e confirmou o mesmo gap de numeração
  11.3/11.4 que o outro revisor achou independentemente — **CORRIGIDO**
  junto.

Nenhum dos 2 verificadores encontrou problema no mecanismo central de
CAS (seções 1, 2.2, 2.3, 8 quanto ao lock em si) — as inconsistências
encontradas foram todas nas seções novas desta rodada de correção
(replay, partição de fila, contrato de erro), exatamente onde a
correção manual, sem verificação, seria mais provável de errar.

### 11.6 — Quarta rodada: verificação estreita da síntese do replay (§2.1.1), depois da correção do achado da §11.5

A correção de §11.3 para o achado de replay (§2.1.1) foi, ela mesma,
verificada uma vez pela §11.5 (achado N1, regressão contra a garantia
documentada do código real) e corrigida. Dada a quantidade de raciocínio
NOVO nessa correção específica — e o padrão já repetido 3 vezes neste
documento de "correção plausível, furo na precisão exata" — uma quarta
rodada, desta vez de escopo estreito (não um novo full-audit), verificou
especificamente essa síntese, com 4 perguntas direcionadas.

**`database-architect`** confirmou a síntese estruturalmente correta (sem
lock de escrita no replay é seguro porque nenhuma dependency/middleware
desta rota pré-carrega `Lead` no identity map antes do handler —
confirmado por rastreamento completo da cadeia real de dependencies, não
suposição) mas achou 1 HIGH (bloqueante) + 4 MEDIUM + 3 LOW, a maioria
consequência direta da própria correção:

- **HIGH (G1)**: `IDEMPOTENCY_PAYLOAD_CONFLICT` era levantado ANTES da
  leitura do lead no ramo de replay — sem `current_responsavel_id`
  disponível, quebrando a promessa de "sempre presente" do §7. CORRIGIDO
  (leitura do lead sobe para antes dessa checagem, seção 2.1.1/§8).
- **MEDIUM (G2)**: `LEAD_ALREADY_ASSIGNED` ganhou um segundo emissor
  (staleness de replay) com semântica diferente do primeiro (perda de
  CAS), e o texto só descrevia o primeiro — CORRIGIDO (§6/§6.2, os dois
  emissores documentados explicitamente).
- **MEDIUM (G3)**: READ COMMITTED virou invariante de correção para o
  Caso 2b (sem ele, a leitura do replay degrada em SILÊNCIO sob
  REPEATABLE READ/SERIALIZABLE, ao contrário do caminho com lock que
  falha ruidosamente) — nunca nomeada, nunca garantida em código, não
  testável pelo CI (SQLite não tem o mesmo conceito) — CORRIGIDO
  (`isolation_level="READ COMMITTED"` explícito no `create_engine`, seção
  2.1.1).
- **MEDIUM (G4)**: a frase "lock de escrita seria custo sem benefício"
  estava superdimensionada — o lock protege contra um escritor EM VOO,
  só não fecha o caso de um escritor que comece DEPOIS da transação do
  replay (nenhuma forma de lock sobrevive a uma resposta HTTP já
  enviada) — CORRIGIDO (justificativa reescrita, Caso 2b explicitamente
  marcado como garantia forte no cenário sequencial, não absoluta sob
  concorrência; residual novo em §10, item 7).
- **MEDIUM (G5)**: sub-caso não enumerado — replay contra um lead
  liberado e ainda não reatribuído produz `current_responsavel_id=None`,
  que soa contraditório com o nome do token — CORRIGIDO (nota explícita
  na seção 2.1.1).
- **LOW (G6, G7)**: o novo SELECT do replay ficava fora do
  `try/except SQLAlchemyError`, e `lock_timeout` não distinguia `SET` de
  `SET LOCAL` (vazaria para a próxima request na mesma conexão do pool)
  — ambos CORRIGIDOS (§2.1.1/§8).
- **5 ecos textuais da ordem REJEITADA** ("replay sob lock"), sobrevivendo
  em §2.1 (resumo do mecanismo), no próprio TÍTULO do §2.1.1 (contradizia
  o corpo da mesma seção), em §3 (Casos 2/2b), e em DUAS afirmações do
  §11.3 — uma delas dizendo falsamente que a contradição §3/§8 já tinha
  sido resolvida. Todos CORRIGIDOS (ver as respectivas seções e a nota
  de precisão acrescentada em §11.3).

Nenhum destes achados reabre a arquitetura (CAS sob lock para primeira
aplicação, replay sem lock para comparação) — todos são precisão de
especificação dentro da síntese já correta. G1 foi o único tratado como
bloqueante antes de implementar; os demais eram lacunas de completude
que uma implementação ingênua poderia ter descoberto sozinha, do jeito
mais caro (em produção) em vez do mais barato (no desenho).

---

## 12. Não implementado

Nenhum código de produção, migration, ou edição do plano foi criado por
este documento.

---

## 13. Veredito desta rodada

Quatro rodadas de revisão/auditoria obrigatória (§11.1-11.6) resultaram
em: 1 CRITICAL, ~12 HIGH, ~21 MEDIUM, ~15 LOW — TODOS corrigidos no
texto deste documento, nenhum descartado, nenhum adiado por
conveniência. O achado CRITICAL da primeira rodada (lock no-op) foi
re-verificado empiricamente após a correção, não apenas corrigido em
prosa (§11.2). Os achados HIGH mais sérios da segunda rodada — o
caminho de replay não re-checando o dono do lead (§2.1.1, composição
perigosa com R16), e a lógica de supressão de `SUPERADO_TERMINAL`
inferindo convergência sem confirmá-la (§6.2) — foram fechados por
correção estrutural do mecanismo, não por nota de rodapé: o replay
passa a comparar o dono fresco do lead contra o registro, e a supressão
de ticket exige uma comparação explícita antes de decidir.

**Registrado sem suavizar, duas vezes seguidas nesta mesma seção do
documento**: a PRIMEIRA correção do achado do replay (rodada 2→3) tinha,
ela mesma, uma regressão nova — colocava a validação de lead/usuário
ANTES do replay, reabrindo por outra porta o exato bug (comentário de 35
linhas em `v2_lead_domain.py:205-239`) que o código real já evita hoje.
Pega pela auditoria de completude da terceira rodada (§11.5). A
correção DESSA correção (rodada 3→4) — preservar a ordem real do
código, checagem de staleness dentro do ramo de replay — foi, ela
mesma, verificada uma quarta vez (§11.6) por uma checagem estreita e
direcionada, que confirmou a síntese estruturalmente correta mas achou
mais 1 HIGH bloqueante (`IDEMPOTENCY_PAYLOAD_CONFLICT` sem
`current_responsavel_id` disponível no ramo de replay) e uma cauda de
achados MEDIUM/LOW, incluindo 5 ecos textuais da ordem já rejeitada
sobrevivendo em outras partes do documento — um deles afirmando
FALSAMENTE que já tinha sido corrigido. Contando as rodadas de
R15/R16/R17 anteriores a este arquivo (a refutação do índice único, a
correção do `operation_id`, o próprio achado CRITICAL do lock), esta é
a QUINTA vez que uma correção sintetizada precisou de uma segunda
verificação dedicada antes de ser confiável. O padrão não parou de se
repetir — o processo o pegou de novo, a cada vez, antes do código
existir, que é exatamente o ponto mais barato para pegá-lo.

3 revisores independentes, em ângulos diferentes, convergiram sem
coordenação no mesmo achado (`CRM_LEAD_JA_ATRIBUIDO` nunca nomeado) —
tratado como sinal forte de que era real, não artefato de prompt (§11.4).

**Nenhum item estrutural permanece ABERTO dentro do escopo de
R-B/R15/R16/R17.** Os residuais nomeados na seção 10 são, cada um,
explicitamente FORA do escopo de R-B (R10, writers V1 pré-existentes,
decisão de produto) — não uma lacuna dentro do próprio R-B. R11 (PII em
log, achado incidental da revisão de segurança desta rodada) permanece
onde já estava — rastreado independentemente na matriz de prontidão,
não reaberto nem fechado aqui, fora do escopo desta rodada por definição
do próprio pedido ("fechar SOMENTE R-B").

**Este documento está pronto para a etapa de implementação.** Não está,
e não se declara, pronto para canary/produção — isso depende de
implementação real + E2E verde + os demais gates nomeados na matriz de
prontidão (R1/R2, R3, R8, R9, R10, R11, R13), nenhum dos quais este
documento toca.
