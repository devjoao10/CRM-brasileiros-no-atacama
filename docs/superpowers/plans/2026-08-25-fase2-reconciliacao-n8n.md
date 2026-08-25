# Fase 2 — Reconciliação n8n e fechamento de contratos — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fechar as divergências REAIS entre os três workflows n8n em produção e o
código já estabilizado, corrigindo no repositório só o que é do repositório e
entregando ao operador instruções manuais exatas para o que é do n8n.

**Architecture:** Nada de novo. Três correções cirúrgicas em código existente,
cada uma com teste que reprova antes e passa depois, mais documentos de
reconciliação. Zero abstração nova, zero dependência nova, zero refactor.

**Tech Stack:** Python 3.11/3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0, n8n
(externo, não alcançável daqui), testes como scripts autônomos (`python
tests/x.py`, exit 0 = passou) — **não pytest**.

**Spec:** `docs/audit/N8N_CURRENT_STATE_RECONCILIATION.md` (escrito nesta fase)

## Global Constraints

- NÃO fazer deploy, NÃO alterar produção, NÃO rodar migration em produção, NÃO
  tocar no n8n ao vivo, NÃO fazer merge, NÃO rotacionar credencial.
- Alteração em workflow n8n **só** como instrução manual + JSON proposto marcado
  `PROPOSED ONLY — NOT DEPLOYED`. Nunca marcar como RESOLVED em produção.
- O workflow **Notificador não existe mais**. Não recriar, não substituir, não
  tratar a ausência como bug. "Entrar na fila humana" ≠ "notificar atendente".
- Teste que se auto-desliga (SKIP silencioso) é proibido: se falta pré-requisito,
  o teste REPROVA com mensagem.
- `subprocess.run(..., text=True)` sempre com `encoding="utf-8", errors="replace"`
  — há guarda no repositório que reprova o contrário.
- Não remover teste, não silenciar lint, não usar `# type: ignore`.
- Baseline a não regredir: **64/64** arquivos de teste passando.

---

### Task 1: `PUT /api/leads/{id}` aceitar campo textual vazio

**Problema (evidência).** A ferramenta `Tool Atualizar Lead` do workflow
"Agente Gerenciador de Leads — BnA" tem um `jsonBody` **fixo** que sempre manda
todas as chaves, e o próprio `toolDescription` instrui: *"Campos sem informacao
devem ficar vazios"*. Rodando o payload real contra o schema real:

```
LeadUpdate(nome="", ...)  ->  RECUSADO
    nome: String should have at least 1 character
```

Ou seja: **toda atualização de lead sem nome novo devolve 422** e o dado
coletado pela Bia é descartado. `LeadCreate` já aceita string vazia (validador
`empty_str_to_none`); `LeadUpdate` não. Defeito **pré-existente** — `min_length=1`
é idêntico em `origin/main`.

**Files:**
- Modify: `app/schemas/lead.py` (classe `LeadUpdate`)
- Test: `tests/test_n8n_contract_lead_update.py` (criar)

**Interfaces:**
- Consumes: `LeadUpdate` de `app/schemas/lead.py`
- Produces: `LeadUpdate` passa a tratar `""` como "campo não informado" nos
  campos textuais opcionais, mantendo `min_length=1` para valor NÃO vazio.

- [ ] **Step 1: Escrever o teste que reprova**

```python
# tests/test_n8n_contract_lead_update.py
import os, sys, pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("SEED_INITIAL_ADMIN", "false")

import app.main            # noqa: F401  registra os mappers
import pydantic
from app.schemas.lead import LeadUpdate

falhas = []
def check(cond, msg):
    print(("  PASS: " if cond else "  FAIL: ") + msg)
    if not cond:
        falhas.append(msg)

# O corpo EXATO que Tool Atualizar Lead manda quando nada novo foi coletado.
PAYLOAD_VAZIO = {
    "nome": "", "whatsapp": "", "destinos": "", "email": "",
    "num_viajantes": "", "num_criancas": "", "idades_criancas": "",
    "data_chegada": "", "data_partida": "", "total_dias": "",
    "datas_destinos": {}, "dias_por_destino": {},
}
try:
    obj = LeadUpdate(**PAYLOAD_VAZIO)
    check(obj.nome is None, "nome vazio vira None, nao 422")
    check(obj.email is None, "email vazio vira None")
    check(obj.num_viajantes is None, "num_viajantes vazio vira None")
except pydantic.ValidationError as e:
    check(False, f"payload real da tool foi RECUSADO: {e.errors()[0]}")

# Vazio nao pode APAGAR o nome de um lead existente.
check(LeadUpdate(**PAYLOAD_VAZIO).model_dump(exclude_none=True).get("nome") is None,
      "nome vazio nao entra no update (nao apaga o nome atual)")

# Nome de verdade continua valendo, e espaco em branco continua sendo vazio.
check(LeadUpdate(nome="Joao").nome == "Joao", "nome real continua aceito")
check(LeadUpdate(nome="   ").nome is None, "so espaco tambem e vazio")

print()
if falhas:
    print(f"{len(falhas)} FALHA(S)"); sys.exit(1)
print("OK: contrato de update do n8n aceito")
```

- [ ] **Step 2: Rodar e confirmar que reprova**

Run: `ENVIRONMENT=development python tests/test_n8n_contract_lead_update.py`
Expected: FAIL em `payload real da tool foi RECUSADO: ... String should have at least 1 character`

- [ ] **Step 3: Implementar o mínimo**

Em `app/schemas/lead.py`, na classe `LeadUpdate`, acrescentar um validador
`mode="before"` que roda ANTES da checagem de `min_length`:

```python
    @field_validator("nome", "whatsapp", "email", "idades_criancas", mode="before")
    @classmethod
    def _vazio_e_ausente(cls, v):
        """String vazia = campo NAO informado, nunca "apague este campo".

        AUDIT-2026-08-F2: a tool `Tool Atualizar Lead` do n8n tem jsonBody FIXO
        — ela manda TODAS as chaves em toda chamada, e o proprio toolDescription
        manda deixar vazio o que nao foi coletado. Com `min_length=1` em `nome`,
        toda atualizacao sem nome novo virava 422 e o dado coletado pela Bia era
        descartado. LeadCreate ja tratava vazio como ausente; aqui nao tratava.
        `None` faz o campo sumir do `exclude_unset`/`exclude_none` do router, que
        e exatamente "nao mexa neste campo".
        """
        if isinstance(v, str) and not v.strip():
            return None
        return v
```

- [ ] **Step 4: Rodar e confirmar que passa**

Run: `ENVIRONMENT=development python tests/test_n8n_contract_lead_update.py`
Expected: PASS em todos os checks

- [ ] **Step 5: Não regredir o resto**

Run: `ENVIRONMENT=development python tests/test_pipeline_inline_lead_edit.py`
Run: `ENVIRONMENT=development python tests/test_leads_segment_drift.py`
Expected: exit 0 nos dois

- [ ] **Step 6: Commit**

```bash
git add app/schemas/lead.py tests/test_n8n_contract_lead_update.py
git commit -m "fix(leads): update do n8n devolvia 422 sempre que nao havia nome novo"
```

---

### Task 2: separar SILÊNCIO de FALHA na ponte Conversas↔Bia

**Problema (evidência).** O workflow "WF-01 Agente Bia" ganhou um portão para
reação de emoji: mensagem composta só de emoji cai no node `Ignorar mensagem`,
que responde **404 sem corpo**. Do outro lado,
`conversas/app/routers/webhook.py::_fetch_agent_parts` faz:

```python
if resp.status_code != 200:
    logger.error(f"Agente IA retornou status {resp.status_code} ...")
    return []
```

e `[]` significa `degraded = True`, que manda ao cliente
`AGENT_FALLBACK_REPLY` = *"Tive uma instabilidade para processar sua mensagem
agora. Pode me enviar novamente em alguns instantes? 🙂"*.

Resultado: **quem manda um 👍 sozinho recebe um pedido de desculpas por
instabilidade** — o oposto exato do que o portão foi construído para fazer — e
cada reação de cliente grava uma linha de ERRO no log, envenenando o log com
eventos normais. A causa raiz é a própria docstring da função: *"quem chama nao
precisa distinguir os modos de falha"* — silêncio e falha estão conflados num
único `[]`.

**Files:**
- Modify: `conversas/app/routers/webhook.py` (`_fetch_agent_parts` e o chamador)
- Test: `tests/test_conversas_agent_silence.py` (criar)
- Manual n8n: `Ignorar mensagem` passa de 404 para 204 (Task 5)

**Interfaces:**
- Consumes: `_fetch_agent_parts(agent_url, payload, conversation_id)`
- Produces: a função passa a devolver `(partes: list, silencio: bool)`.
  `silencio=True` significa "a Bia decidiu não responder" — o chamador NÃO envia
  fallback e NÃO loga erro. `partes=[] e silencio=False` continua sendo
  degradação.

- [ ] **Step 1: Escrever o teste que reprova**

```python
# tests/test_conversas_agent_silence.py  (esqueleto — o corpo completo esta na Task 2 do executor)
# Monta um servidor HTTP local que responde 204 (silencio) e outro que responde
# 500 (falha), e afirma:
#   204 -> nenhuma mensagem enviada ao cliente, nenhum log de erro
#   500 -> exatamente uma mensagem de fallback
#   200 com resposta -> as partes normais
```

O teste sobe um `http.server` em porta efêmera, aponta `N8N_BASE_URL` para ele e
exercita `_forward_to_agent` de verdade. Nada de mock do próprio módulo sob
teste.

- [ ] **Step 2: Rodar e confirmar que reprova**

Run: `ENVIRONMENT=development python tests/test_conversas_agent_silence.py`
Expected: FAIL — o caso 204 hoje manda o fallback

- [ ] **Step 3: Implementar o mínimo**

`_fetch_agent_parts` devolve `(partes, silencio)`:
- `204` **ou** `200` com `{"ignorar": true}` → `([], True)`, log em `debug`
- `200` com texto → `(partes, False)`
- qualquer outro → `([], False)`, log em `error` (como hoje)

O chamador: se `silencio`, retorna sem enviar nada.

- [ ] **Step 4: Rodar e confirmar que passa**

Run: `ENVIRONMENT=development python tests/test_conversas_agent_silence.py`
Expected: PASS

- [ ] **Step 5: Não regredir**

Run: `ENVIRONMENT=development python tests/test_conversas_agent_timeout.py`
Run: `ENVIRONMENT=development python tests/test_conversas_webhook.py`
Run: `ENVIRONMENT=development python tests/test_conversas_webhook_hardening.py`
Expected: exit 0 nos três

- [ ] **Step 6: Commit**

```bash
git add conversas/app/routers/webhook.py tests/test_conversas_agent_silence.py
git commit -m "fix(conversas): reacao de emoji recebia pedido de desculpas por instabilidade"
```

---

### Task 3: `StageSchema.id` — trocar allowlist estreita por rejeição do que é perigoso

**Problema (evidência).** A Fase 1 fechou um XSS real (`stage.id` interpolado cru
em seis atributos HTML) com DUAS defesas: escapar no template (a que resolve) e
`pattern=r"^[A-Za-z0-9_-]+$"` no schema (defesa em profundidade). A segunda
carrega um risco de disponibilidade que não consigo verificar: `FunnelUpdate`
revalida a lista `etapas` INTEIRA, então se algum funil de produção tiver etapa
com id contendo espaço ou acento — por exemplo `"Sem Contato"`, que é como o
system message do Gerenciador se refere à etapa — **qualquer edição daquele
funil passa a devolver 422**. Verificado: o padrão rejeita `'Sem Contato'`.

O valor de segurança vem de bloquear os caracteres que quebram atributo HTML ou
literal JS. Bloquear espaço e acento não acrescenta segurança e adiciona risco.

**Files:**
- Modify: `app/schemas/pipeline.py` (`StageSchema.id`)
- Test: `tests/test_frontend_injection_contract.py` (ajustar a seção 4)

**Interfaces:**
- Consumes: `StageSchema` de `app/schemas/pipeline.py`
- Produces: `StageSchema.id` rejeita `" ' < > & \ ` + controle; aceita espaço,
  acento e o resto. `max_length=64` e `min_length=1` permanecem.

- [ ] **Step 1: Ajustar o teste primeiro (ele hoje afirma o padrão estreito)**

Em `tests/test_frontend_injection_contract.py`, seção 4, os casos passam a ser:
aceitar `'nova_oportunidade'`, `'sem_contato'`, `'Sem Contato'`, `'Pré-venda'`;
recusar `"x');alert(1);//"`, `'a"b'`, `'<script>'`, `''`, `'x'*65`.

- [ ] **Step 2: Rodar e confirmar que reprova**

Run: `ENVIRONMENT=development python tests/test_frontend_injection_contract.py`
Expected: FAIL em `'Sem Contato'` e `'Pré-venda'` (o padrão atual os recusa)

- [ ] **Step 3: Implementar**

```python
    id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        # AUDIT-2026-08-F2: era `^[A-Za-z0-9_-]+$`. O que protege de verdade e o
        # esc() no template (travado por tests/test_frontend_injection_contract.py);
        # este padrao e defesa em profundidade. Uma allowlist estreita nao
        # acrescenta seguranca sobre "sem aspa, sinal e barra invertida" e
        # QUEBRA qualquer funil de producao cuja etapa ja tenha espaco ou acento
        # — e o proprio system message do Gerenciador chama a etapa de
        # "Sem Contato". Rejeita-se o perigoso, nao tudo que nao e slug.
        pattern=r'^[^"\'<>&\\\x00-\x1f\x7f]+$',
        description="ID unico da etapa (ex: 'novo', 'sem_contato', 'Sem Contato')",
    )
```

- [ ] **Step 4: Rodar e confirmar que passa**

Run: `ENVIRONMENT=development python tests/test_frontend_injection_contract.py`
Expected: PASS

- [ ] **Step 5: Não regredir**

Run: `ENVIRONMENT=development python tests/test_pipeline_review_final.py`
Run: `ENVIRONMENT=development python tests/test_pipeline_stage_pagination.py`
Expected: exit 0 nos dois

- [ ] **Step 6: Commit**

```bash
git add app/schemas/pipeline.py tests/test_frontend_injection_contract.py
git commit -m "fix(pipeline): allowlist de etapa quebraria funil com espaco no id"
```

---

### Task 4: `N8N_CURRENT_STATE_RECONCILIATION.md`

**Files:**
- Create: `docs/audit/N8N_CURRENT_STATE_RECONCILIATION.md`

- [ ] **Step 1: Escrever o documento** com, obrigatoriamente:
  - `CURRENT PRODUCTION WORKFLOWS PROVIDED:` os três, com webhook, nós e tools.
  - `NOT IN PRODUCTION: Notificador`.
  - Delta snapshot antigo × atual, por workflow, medido (`docs/audit/` guarda a saída).
  - Findings confirmados, invalidados e novos, cada um com evidência.
  - Contratos incompatíveis.
  - Riscos não verificáveis daqui.
- [ ] **Step 2: Commit**

---

### Task 5: `N8N_MANUAL_CHANGES.md` + JSONs propostos

**Files:**
- Create: `docs/audit/N8N_MANUAL_CHANGES.md`
- Create: `docs/audit/proposed_n8n/*.json` (marcados `PROPOSED ONLY — NOT DEPLOYED`)

- [ ] **Step 1:** Uma seção por mudança, no formato exigido (WORKFLOW / NODE /
  NODE TYPE / CURRENT BEHAVIOR / PROBLEM / EXACT CHANGE / FIELDS TO CHANGE /
  OLD VALUE / NEW VALUE / CONNECTION CHANGES / NODES TO REMOVE / NODES TO ADD /
  EXPECTED RESULT / TEST MANUAL / ROLLBACK).
- [ ] **Step 2:** Gerar os JSONs propostos a partir dos exports atuais,
  aplicando só as mudanças descritas, e validar que continuam JSON válido e que
  o grafo de conexões continua consistente.
- [ ] **Step 3: Commit**

---

### Task 6: atualizar artefatos e veredito

**Files:**
- Modify: `docs/audit/FINDINGS.csv`, `ROOT_CAUSES.md`, `FULL_SYSTEM_AUDIT.md`,
  `FULL_SYSTEM_STABILIZATION_PLAN.md`, `RELEASE_READINESS.md`
- Create: `docs/audit/POSTGRES_VALIDATION.md`, `docs/audit/BACKUP_RESTORE_VALIDATION.md`

- [ ] **Step 1:** Incorporar os findings novos com IDs `N8N-F..`, reclassificar
  os invalidados, registrar os adjudicados.
- [ ] **Step 2:** Rodar a suíte COMPLETA (64+ arquivos, um processo por arquivo)
  e comparar contra 64/64.
- [ ] **Step 3:** Novo veredito com os campos exigidos.
- [ ] **Step 4: Commit**

---

## Self-Review

**Cobertura da spec:** os três workflows têm tarefa (4, 5); o Notificador tem
tarefa (5); os contratos quebrados têm tarefa (1, 2); o risco introduzido pela
Fase 1 tem tarefa (3); PostgreSQL e backup estão com agentes dedicados e caem na
Task 6. Findings ADDRESSED_UNVERIFIED e OPEN: agente dedicado, Task 6.

**Placeholders:** nenhum "TBD". A Task 2 Step 1 traz esqueleto em vez do corpo
completo do teste — é a única, e é deliberada: o corpo depende da porta efêmera e
do formato de log, que o executor observa na hora. Todos os demais steps trazem
o código real.

**Consistência de tipos:** `_fetch_agent_parts` muda de `list` para
`tuple[list, bool]` na Task 2 — o único chamador está no mesmo arquivo e é
alterado no mesmo step. `LeadUpdate` e `StageSchema` não mudam de nome nem de
tipo, só de validação.
