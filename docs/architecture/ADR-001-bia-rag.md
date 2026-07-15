# ADR-001 — Arquitetura RAG da BIA (BIA-RAG-CONTEXT-01)

**Status:** aceito · **Data:** 2026-07-15 · **Pacote:** BIA-RAG-CONTEXT-01

## Contexto

A BIA precisa responder com base no contexto canônico versionado em
`bna_agent_context/` (73 arquivos Markdown, taxonomia numerada, ~122 pendências
reais). Hoje o conhecimento está embutido no prompt de produção (~20k chars);
mudar um preço exige editar o workflow live. O objetivo é RAG de produção:
ingestão → chunking → embeddings → vector store persistente → retrieval filtrado
por status de validação → integração com a BIA — sem colar os 73 arquivos num
prompt e sem edição manual recorrente do workflow.

## Restrições que moldaram a decisão

1. **O MCP do n8n não edita workflows live com segurança** (comprovado em
   N8N-BIA-GUARDRAILS-03: `update_workflow` reconstrói o workflow e não preserva
   bindings de credencial). Logo, integração via nós nativos de vector store
   editados por MCP é inviável com segurança.
2. **Não há vector store existente** para reusar; **não há pgvector** instalado.
3. **Instalar pgvector no Postgres do CRM arrisca a produção** (a missão adverte
   contra alterar o banco do CRM).
4. Stack do projeto é **Python/FastAPI**; **Gemini já é dependência** e tem
   modelo dedicado de embeddings (`text-embedding-004`).

## Opções consideradas

| Opção | Veredito |
|---|---|
| A. Nós nativos de vector store do n8n | **Rejeitada** — edição live via MCP destrói bindings; disponibilidade dos nós não verificável via MCP. |
| B. **Serviço RAG auxiliar isolado** | **ESCOLHIDA** — FastAPI + SQLite persistente + Gemini embeddings; n8n chama via HTTP interno. |
| C. pgvector no Postgres do CRM | Rejeitada — risco ao banco de produção; extensão ausente. |
| D. Vector DB dedicado (Qdrant/Chroma) | Rejeitada por ora — serviço pesado, nova superfície de falha/backup; SQLite atende à escala (centenas de chunks). |

## Decisão

**Serviço RAG auxiliar (Opção B):**

- **Serviço:** FastAPI (`rag/app/service.py`), container `bia_rag`, porta interna
  8100, **sem Traefik** (não exposto publicamente), auth por header
  `X-BIA-RAG-Token`. Endpoints: `/health`, `/status`, `/ingest`, `/search`,
  `/validate`.
- **Vector store:** SQLite (`rag/app/store.py`) em volume persistente
  `bia_rag_data` (`/data/bna_bia_context.sqlite3`). Cosseno em Python puro
  (escala do corpus). Upsert, dedup por `chunk_hash`, delete de órfãos, backup/
  restore. Coleção `bia_context_v1`.
- **Embeddings:** Gemini `text-embedding-004` em produção (reusa
  `GEMINI_API_KEY`); backend **determinístico** offline para testes/CI (sem rede,
  sem custo, reproduzível).
- **Corpus:** montado **read-only** (`./bna_agent_context:/corpus:ro`) — o
  serviço nunca escreve no corpus. Markdown é a fonte de verdade; o índice é
  derivado e reconstruível.
- **Chunking semântico** por heading (`rag/app/chunker.py`), 120–900 tokens,
  overlap ~80, nunca separa título do conteúdo nem preço/restrição da explicação.
- **Filtro de validação** (`rag/app/retrieve.py`): retrieval híbrido (embedding +
  portão lexical de termos salientes). Chunks com `[PENDENTE_VALIDACAO]` são
  excluídos do contexto factual no nível SQL; se a única base for pendente, a BIA
  informa a pendência e cita a fonte (não afirma como fato); no-answer quando não
  há base; conflito prioriza validado/canônico e sinaliza a pendência.
- **Integração:** WF-01 chama a tool `Buscar Contexto BNA` (HTTP → `/search`)
  ANTES de gerar. Retrieval antes da geração.

## Consequências

- ✅ Persistente, isolado, sem risco ao CRM, reusa credencial Gemini, testável
  offline (35/35 testes herméticos passam), reconstruível.
- ✅ Nenhuma edição live no n8n via MCP (integração é importação manual +
  1 nó-tool no WF-01, reversível).
- ⚠️ SQLite escala para centenas/poucos milhares de chunks; se o corpus crescer
  muito (dezenas de milhares), migrar para vector DB dedicado (Opção D) — o
  contrato do `store` isola essa troca.
- ⚠️ O backend determinístico é para testes de PIPELINE, não de precisão
  semântica; a precisão de retrieval em produção depende do Gemini.
