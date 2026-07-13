# PERPETUA-PRODUCTION-DRIFT-01 — Consolidação dos hotfixes de produção

**Tipo:** BUGFIX / consolidação de configuração · **Status:** consolidado localmente, testado localmente, **não deployado**.

## Por que existia drift

Durante a recuperação da Perpétua (2026-07-08), dois hotfixes foram aplicados
**diretamente na VPS** (`/opt/crm`, containers `crm_app` etc.) para restabelecer
o serviço, sem passar pelo fluxo Git → PR → deploy:

1. **`app/routers/leads.py`** — o endpoint `GET /api/leads/segment` quebrava com
   `TypeError: _build_lead_response() takes 1 positional argument but 2 were given`:
   a assinatura de `_build_lead_response(lead)` tem 1 parâmetro, mas a chamada
   dentro de `segment_leads` ainda passava `(l, db)`. Produção foi corrigida para
   `_build_lead_response(l)`.
2. **`docker-compose.yml`** — o serviço `crm` não recebia `INTERNAL_AI_AUTH_SECRET`,
   necessário para a autenticação HMAC interna da Perpétua
   (PERPETUA-INTERNAL-AUTH-01). Produção ganhou a linha
   `- INTERNAL_AI_AUTH_SECRET=${INTERNAL_AI_AUTH_SECRET:-}` no bloco `environment`
   (logo após `GEMINI_API_KEY`), **somente por expansão de variável** — o valor
   real vive apenas no `.env` da VPS.

Este pacote formaliza exatamente essas duas mudanças no repositório, com a mesma
posição/estilo usados em produção — após o merge+deploy futuro, `git diff` na VPS
para esses arquivos zera.

## Auditoria read-only da VPS (2026-07-12, executada pelo operador)

- VPS em `main` @ `f5e9072`; tracked modificados: **somente** os 2 arquivos acima;
  diffs reais idênticos aos consolidados aqui (1 linha cada).
- `INTERNAL_AI_AUTH_SECRET` e `GEMINI_API_KEY` no compose da VPS: ambos
  `env-expansion` — **nenhum secret hardcoded**.
- Containers: `crm_app` healthy, `crm_postgres` healthy, `conversas_app` e
  `n8n_server` up. Nada foi alterado na VPS.
- Artefatos registrados para tratamento posterior (NÃO tocados):
  - `/opt/crm/.env.backup_perpetua_prod_20260708_210154` ⚠️ backup de `.env`
    (contém secrets) untracked dentro do diretório do repo — mover para fora do
    repo/backup seguro num pacote OPS futuro;
  - `/opt/crm/app/routers/leads.py.backup_segment_20260708_214222` e `_215035`;
  - stashes na VPS: `pre-perpetua-deploy-manual-ai-hotfix` (main) e
    `pre-deploy-vps-local-staged-changes-2026-07-07` — não aplicar/remover sem
    pacote dedicado.

## Alterações deste pacote

- [app/routers/leads.py] `segment_leads`: `_build_lead_response(l, db)` →
  `_build_lead_response(l)` (1 linha; nenhuma outra chamada/assinatura/filtro/
  paginação/contrato alterados).
- [docker-compose.yml] serviço `crm`: + `- INTERNAL_AI_AUTH_SECRET=${INTERNAL_AI_AUTH_SECRET:-}`
  (1 linha, mesma posição da produção; nenhum outro serviço tocado; `version`
  obsoleta mantida de propósito — fora do escopo).
- [tests/test_leads_segment_drift.py] (novo) — 6 testes herméticos:
  segment com leads retorna 200 + estrutura `{total, skip, limit, leads}`;
  filtros/paginação preservados (`search`, `skip`, `limit`, `destino`);
  guard estático do código (tripwire contra `(l, db)` voltar);
  compose: variável presente 1x, só no `crm`, só por expansão, YAML parseável;
  `.env.example` documenta o nome. Sem Gemini/rede/n8n/banco real (SQLite em
  `scratch/`, leads semeados direto no banco — sem disparar automações).
  Verificado nos dois estados: **pré-fix 3/6 FAIL** (reproduz o TypeError de
  produção) · **pós-fix 6/6 PASS**.

## Impacto

- **Contratos:** nenhum (mesma rota, mesmo schema de resposta; a mudança elimina
  um 500). **Migrations:** nenhuma. **Banco:** intocado. **Conversas:** intocado.
- **n8n:** nenhum workflow alterado; workflows LIVE não usam `/api/leads/segment`
  (usam by-whatsapp/leads/tags/pipeline/tasks/analytics — auditoria 2026-07-12).
- **Segurança:** segredo só por expansão; valor real nunca entrou no Git.

## Plano de deploy posterior (fora deste pacote)

Após merge deste pacote na main, o bloqueio de drift deixa de existir:
`git pull` na VPS passará a encontrar árvore compatível (diff dos 2 arquivos
zera). O deploy continua **manual e gated** (`deploy.yml` via workflow_dispatch,
gates de backup/migrations/smoke/aprovação). Recomendação: antes do pull na VPS,
tratar os backups untracked e stashes registrados acima (pacote OPS curto).

## Rollback

- **Código/compose (repo):** `git revert` dos commits do pacote (2 linhas + teste
  + docs); sem migração, sem contrato.
- **Produção futura:** se um deploy consolidado se comportar mal, `git checkout`
  do commit anterior + rebuild seguem o runbook manual existente — os hotfixes
  atuais da VPS são idênticos ao conteúdo consolidado, então não há estado
  intermediário novo.

## Pendências que permanecem (pacotes futuros)

- DOCUMENT-FILENAME-SECURITY-01 (sanitização de filename PDF/Excel, autorização
  de download, limites, retenção).
- OPS curto: remover/arquivar com segurança os backups untracked e stashes da VPS.
- `version` obsoleta do compose; migração `google.generativeai` → `google.genai`.
