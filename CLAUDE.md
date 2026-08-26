# CRM Brasileiros no Atacama

> Project memory for Claude Code. Keep this file short and high-signal —
> bloated memory gets ignored. Put hard guarantees in hooks, not prose.

## Behavioral guidelines
<!-- aia-harness:behavioral — non-negotiable; do not edit, reorder, or remove during enrichment -->

1. **Think before coding** — state assumptions explicitly; if multiple interpretations exist, present them instead of picking silently; say so when a simpler approach exists; if something is unclear, stop and ask.
2. **Simplicity first** — minimum code that solves the problem. No speculative features, no abstractions for single-use code, no unrequested configurability, no error handling for impossible scenarios. If 200 lines could be 50, rewrite.
3. **Surgical changes** — touch only what the request requires; match existing style; don't refactor, reformat, or "improve" adjacent code. Remove orphans *your* change created; leave pre-existing dead code alone (mention it, don't delete it). Every changed line should trace directly to the user's request.
4. **Goal-driven execution** — turn tasks into verifiable goals ("fix the bug" → "write a test that reproduces it, then make it pass"). For multi-step work, state a brief plan with a verify check per step, then loop until verified.
5. **Main session = orchestrator — it does not implement.** Plan, decide, coordinate; ALL delegable implementation and analysis goes to a specialist subagent via `Agent`, parallel when scopes don't conflict.

## Stack
Python, JavaScript, SQL · FastAPI · pip

Architecture: **layered**.

## Canonical commands
Always use these exact commands (do not guess):

- **Install (CRM):** `pip install -r requirements.txt`
- **Install (Conversas):** `pip install -r conversas/requirements.txt` — em ambiente SEPARADO; os pins conflitam com os do CRM.
- **Test:** `python tests/test_<nome>.py` (um processo por arquivo). Suite inteira: rode arquivo a arquivo, como o CI faz.
- **Run/Dev (CRM):** `uvicorn app.main:app --reload --port 8000`
- **Run/Dev (Conversas):** `uvicorn app.main:app --reload --port 8001` a partir de `conversas/`
- **Stack completa:** `docker compose up -d`
- **Lint / Format / Typecheck / Build:** não configurados neste projeto. `ruff`, `mypy`, `pytest` e `python -m build` NÃO são dependências e não estão no PATH — não os invoque.

## Workflow & Agents

Invoke `superpowers:subagent-driven-development` for **non-trivial** implementation — trigger it when the request meets **≥2** of:

- touches **3+ files** or **2+ domains/layers** (UI + agent, API + DB…)
- is a **new feature / epic / cross-cutting refactor** (not a one-line or single-function change)
- needs a **multi-step plan** or ordered tasks, each with its own verification
- has **unclear scope or root cause** and needs exploration before coding

Skip it — implement inline — for typo/copy fixes, single-function edits, config tweaks, or one-file bugs with an obvious cause.

When dispatching subagents, you MUST use the matching specialist agent from the table below — never the generic agent when a specialist is listed. Cross-reference the task type with the "When to use" column and pass the exact name as `subagent_type`.

Model dispatch: an agent's frontmatter `model` wins; a generic dispatch or a project/user agent with no `model` in frontmatter is force-set to `sonnet` by a PreToolUse hook, so it never silently inherits this session's model — except namespaced plugin agents (`plugin:name`), left unrewritten since their frontmatter isn't reliably hook-resolvable. Pass `model` explicitly yourself for those, or to override for complex work: `haiku` for search/exploration, `sonnet` for implementation, `opus` for architectural judgment — cheapest tier that fits.

| Agent | When to use |
|---|---|
| `orchestrator` | Coordinates multi-agent or cross-domain tasks by subdelegating to specialized agents. Use proactively when a task spans multiple domains or requires parallel subagent execution. MUST BE USED instead of dispatching generic agents directly for complex workflows. |
| `code-reviewer` | Reviews any code change for bugs, security, error handling, and test coverage. Use proactively after editing any source file. MUST BE USED before merging a pull request. |
| `security-reviewer` | Reviews code for OWASP Top 10 vulnerabilities, hardcoded secrets, broken auth, and dependency CVEs. Use proactively before any merge that touches auth, input handling, or secrets. MUST BE USED before shipping security-sensitive changes. |
| `python-reviewer` | Reviews Python code for injection risks, bare excepts, type annotation gaps, and Pythonic idioms. Use proactively after editing .py files. MUST BE USED before merging Python changes. |
| `fastapi-reviewer` | Reviews FastAPI code for route correctness, Pydantic model validation, dependency injection patterns, async database usage, auth/CORS config, and OpenAPI metadata. Use proactively after editing FastAPI routes, schemas, or middleware. MUST BE USED before merging FastAPI changes. |
| `qa-automation-engineer` | Writes and maintains E2E tests (Playwright/Cypress) and CI/CD quality gates. Use proactively after new user flows are implemented or when E2E coverage is missing for a critical path. |
| `test-engineer` | Writes unit and integration tests with TDD discipline, coverage analysis, and edge-case discovery. Use proactively after implementing new logic or when test coverage gaps are identified. |
| `database-architect` | Designs schemas, migrations, indexes, and query strategies for correctness, integrity, and scalability. Use proactively when adding tables, modifying schemas, planning migrations, or diagnosing slow queries. |
| `devops-engineer` | Owns deployment, CI/CD pipelines, infrastructure configuration, and production operations. Use proactively when deploying, configuring servers, setting up CI, or troubleshooting production incidents. |
| `backend-specialist` | Implements and reviews API endpoints, server-side business logic, authentication, and database integration. Use proactively when building or modifying backend services, REST/GraphQL routes, or persistence layers. |
| `performance-optimizer` | Profiles and fixes performance bottlenecks — slow endpoints, high memory usage, poor Core Web Vitals, and database query inefficiency. Use proactively after profiling reveals a bottleneck or when response times degrade. |
| `product-manager` | Clarifies ambiguous requirements and prioritizes roadmap decisions when requirements are undefined before a story exists. Use when discovery and prioritization need structured analysis. |
| `product-owner` | Translates business objectives into actionable technical specs and defines acceptance criteria for existing stories before implementation begins. Use when a story needs clear acceptance criteria before development starts. |
| `project-planner` | Breaks features and epics into ordered, executable tasks with clear acceptance criteria. Use proactively when starting a new feature, sprint, or significant refactor that needs a structured plan before implementation begins. |
| `code-archaeologist` | Reverse-engineers undocumented or legacy code to uncover intent, trace logic, and map hidden dependencies. Use proactively before refactoring unfamiliar legacy code or when you need to understand why existing behavior exists. |
| `debugger` | Finds the root cause of bugs, crashes, and flaky behavior through systematic, evidence-based investigation. Use proactively when a test fails or a defect is reported, before attempting a fix. |
| `explorer-agent` | Maps an unfamiliar or complex codebase — architecture, patterns, dependencies, and risk areas — to inform planning and integration decisions. Use proactively when onboarding to a new codebase or before planning a cross-cutting change. |
| `documentation-writer` | Produces clear, example-rich technical documentation — READMEs, API docs, runbooks, and guides. Use when documentation is explicitly requested or after a feature ships and needs user-facing docs. |
| `penetration-tester` | Simulates attacker techniques to find exploitable vulnerabilities using PTES and OWASP methodologies. Use proactively before a security release, after adding new auth flows, or when a pentest is required. |
| `security-auditor` | Performs defensive SAST reviews, threat modeling, and hardening recommendations using defense-in-depth principles. Use proactively before a major release or after architectural changes that touch auth, data handling, or trust boundaries. |

### Superpowers → Project Specialists (mandatory bridging)
<!-- aia-harness:agent-routing — superpowers→specialist bridge; do not remove -->

Superpowers skills (`superpowers:dispatching-parallel-agents`, `superpowers:subagent-driven-development`,
`superpowers:executing-plans`, `superpowers:systematic-debugging`) show `general-purpose` as the default
`subagent_type` in their examples. **Never dispatch `general-purpose` (or a generic
implementer) when a specialist below covers the domain** — pass the specialist's exact
name as `subagent_type` instead.

> Basis: superpowers itself states "User's explicit instructions (CLAUDE.md) — highest
> priority." This section applies that priority over the agent types its examples suggest.
> The normal flow is unchanged (`superpowers:brainstorming` → `superpowers:writing-plans` → `superpowers:subagent-driven-development`);
> only the dispatched `subagent_type` changes.

| When superpowers would use `general-purpose` for… | Dispatch instead |
|---|---|
| Multi-domain feature — subdelegates to specialists | `orchestrator` |
| Review / audit changed code | `code-reviewer` / `security-reviewer` / `python-reviewer` / `fastapi-reviewer` |
| E2E / QA automation | `qa-automation-engineer` |
| Unit / integration tests | `test-engineer` |
| Schema / migration / query / data modeling | `database-architect` |
| Deploy / CI/CD / infra | `devops-engineer` |
| Backend / API / server-side / domain logic | `backend-specialist` |
| Performance profiling / optimization | `performance-optimizer` |
| Understand legacy code before changing it | `code-archaeologist` |
| Bug / crash / root-cause analysis | `debugger` |
| Explore / map an unfamiliar codebase | `explorer-agent` |
| Documentation (only when explicitly requested) | `documentation-writer` |
| Offensive security / pentest | `penetration-tester` |
| Security audit / defensive review | `security-auditor` |

### Parallel wave execution (subagent-driven-development)
<!-- aia-harness:parallel-sdd — parallel wave execution override; do not remove -->

Override `superpowers:subagent-driven-development`'s serial one-implementer-at-a-time default with
parallel waves of independent tasks. Its "never dispatch implementers in parallel" red flag is
superseded here because its two premises are removed: disjoint file ownership per wave, and
controller-serialized commits instead of implementer self-commits. During planning, tag each task
`Files:` / `Depends-on:`; batch tasks with disjoint `Files` and no mutual dependency into one wave,
and dispatch their implementers in a single message using the specialist types from the table above.
Keep the skill's implementer/reviewer prompt contracts intact — the only change is implementers do
NOT self-commit. Untagged or uncertain tasks run serial (no regression). Full protocol:
`.claude/rules/08-parallel-subagent-driven-development.md`.

## Architecture map

Dois apps FastAPI independentes no mesmo repo, cada um com seu `requirements.txt`, Dockerfile e versao de Python.

- `app/` — CRM principal (porta 8000, Python 3.11). Leads, pipeline, tarefas, kanban operacional, IA.
- `app/main.py` — monta o FastAPI, registra ~20 routers e roda o `lifespan`: `create_all()` + `seed_database()` + limpeza de `uploads/`. Nao roda migration.
- `app/config.py` — toda config vem de env; em producao falha na subida se `SECRET_KEY`/`DATABASE_URL` faltarem. Guarda o segredo HMAC da IA interna (`INTERNAL_AI_AUTH_SECRET`).
- `app/database.py` — engine dual: SQLite em dev (com `PRAGMA foreign_keys=ON`), PostgreSQL com pool em prod. Exporta `IS_SQLITE` e `get_db`.
- `app/auth.py` — JWT (python-jose) + bcrypt + API keys hasheadas em SHA-256. Fornece `get_current_user`, `require_admin`, `require_page_session`/`page_login_redirect` (paginas HTML).
- `app/routers/` — endpoints `/api/*` e, em `pages.py`, as paginas Jinja. Modulos legados (leads, pipeline, tags, teams, users...) acessam a sessao e commitam direto aqui.
- `app/services/` — regra de negocio do modulo `operational_*` e da IA; e quem chama `db.commit()` nessa camada. Levanta `ValueError`, o router traduz para HTTP.
- `app/repositories/` — apenas acesso SQLAlchemy do modulo `operational_*`. Nunca commitam; consumidos so por services.
- `app/models/` — SQLAlchemy declarative sobre `Base`; `app/models/operational/` agrupa o kanban. `app/schemas/` — Pydantic v2 com `Field(...)` descritivo.
- `conversas/` — segundo app FastAPI (porta 8001, Python 3.12): WhatsApp/Meta, templates, midia, webhook. Tem `app/` proprio (mesmo nome de pacote do CRM — nao importe os dois no mesmo processo). Consome o CRM via `services/crm.py`.
- `templates/` + `static/` — Jinja2 herdando `base.html` (sidebar/topbar parciais) e JS vanilla, sem build step.
- `migrations/` — scripts idempotentes `mNNN_*.py` rodados a mao, fora do startup. Nao e Alembic.
- `tests/` — arquivos executaveis standalone (`if __name__ == "__main__"`), sem rede; ver convencoes abaixo.
- `n8n/` + `bna_agent_context/` — workflows n8n exportados e a base de conhecimento markdown da agente Bia (persona, tours, precos, guardrails), validada por `scripts/validate_bna_agent_context.py`.
- `docker/`, `docker-compose.yml` — Postgres hardened (sem porta no host, user read-only separado para a IA) atras de Traefik. `docs/` — auditorias e registros de WP.

Domain-specific guidance lives in nested CLAUDE.md files (loaded on demand):

- `app/models/` — models layer
- `app/repositories/` — repositories layer
- `app/schemas/` — schemas layer
- `app/services/` — services layer

## Conventions

- **Testes rodam por arquivo, nao por `pytest` generico**: `python tests/test_x.py`. O CI abre dois jobs (CRM 3.11 / Conversas 3.12) e separa os arquivos pela string `CONVERSAS_DIR` — todo teste novo de `conversas/` precisa conter esse marcador ou cai no job errado.
- **Nunca instale `requirements.txt` e `conversas/requirements.txt` juntos** — os pins de fastapi/sqlalchemy/jinja2/python-jose conflitam e o pip aborta.
- **Query com funcao especifica de banco exige o ramo `IS_SQLITE`** — dev e SQLite, prod e PostgreSQL, e os dois precisam passar (exemplos: `app/query_filters.py`, `app/routers/leads.py`, `app/routers/segments.py`).
- **Fronteira de commit por camada**: no modulo `operational_*` so o service commita (repository nunca); nos modulos legados o commit vive no router. Siga o padrao do arquivo que voce esta editando — nao migre camada sem pedido explicito.
- **Nenhum `ALTER TABLE` no startup**: mudanca de schema em banco existente vira script idempotente em `migrations/mNNN_*.py`, e aplicar em producao e acao humana com backup + aprovacao (ver `migrations/README.md`).
- **Auth explicita em toda rota**: mutacao/admin usa `require_admin`, pagina HTML usa `require_page_session` + `page_login_redirect`. `tests/test_security_greps.py` quebra se a contagem de `require_admin` cair.
- **Idioma**: comentarios, docstrings e mensagens de erro em PT-BR; identificadores de dominio em PT (`nome`, `destinos`, `responsavel_id`). Trabalho e rastreado por codigo de WP (`WP-SEC-03`, `DATA-01`, `CONV-VAR-01`) em comentarios e em `docs/wps/`.

## Engineering rules
<!-- aia-harness:fixed — non-negotiable; do not edit, reorder, or remove during enrichment -->

- Match the style of surrounding code; do not introduce new patterns unprompted.
- Test what can break — business rules, branching logic, money/security/auth, bug regressions; skip trivial getters, wrappers, config, presentational UI (rubric: `.claude/rules/05-testing.md`).
- Run the test files touched by your change (`python tests/test_<nome>.py`) before claiming work is complete. There is no lint/typecheck step in this project.
- Never commit secrets; keep them in gitignored env files (`.env`/`.env.local`) — `.claude/settings.local.json` is only for MCP-server credentials referenced by `.mcp.json`.
- Fix every compilation/syntax/lint error found during a session — regardless of whether you edited the file. Never leave the build broken or label errors "pre-existing, not related".
- When performing a code review (user requests it or a workflow triggers it), always use `code-reviewer` and `security-reviewer` and `fastapi-reviewer` and `python-reviewer`, applying the `uncle-bob-craft` skill's criteria (Dependency Rule, SOLID in context, code smells) alongside their findings.

@.claude/memory/INSTRUCTIONS.md
@.claude/memory/MEMORY.md
<!-- Generated by aia-harness. Edit freely; re-run /aia-harness:doctor to audit. -->

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
