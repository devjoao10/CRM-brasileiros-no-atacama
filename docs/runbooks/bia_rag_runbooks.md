# Runbooks — RAG da BIA (BIA-RAG-CONTEXT-01)

Serviço interno `bia_rag` (http://bia_rag:8100). Fonte de verdade: Markdown em
`bna_agent_context/`. Índice: SQLite em volume `bia_rag_data`.

## A. Ingestão / reindexação

Quando o contexto muda:
1. Editar o Markdown em `bna_agent_context/`.
2. `python scripts/validate_bna_agent_context.py` (estrutura do vault) → OK.
3. Dry-run: `python -m rag.scripts.rag_cli dry-run` (mostra chunks, sem escrever).
4. Incremental: `python -m rag.scripts.rag_cli ingest` (só novos/alterados são
   embeddados; remove órfãos).
5. `python -m rag.scripts.rag_cli status` (confere index_version, contagens).
6. `python -m rag.scripts.rag_cli search "pergunta de teste"` (confere retrieval).

No container/produção, o mesmo via API: `POST /ingest {"mode":"incremental"}`
(header `X-BIA-RAG-Token`), ou o workflow `01_bia_context_ingestion` (cron 05:00),
ou o bundle de deploy. **Full rebuild** só quando necessário: `ingest-full` /
`{"mode":"full"}` (limpa a coleção e reindexa tudo).

## B. Deploy (operator-assisted)

Sem acesso direto à VPS, usar o bundle único (entregue no relatório do pacote).
O bundle: audita estado → backup do índice atual (se houver) → build SOMENTE do
`bia_rag` → sobe o serviço → aguarda `/health` → ingest-full → smokes de
retrieval (validado/pendente/no-answer) → valida persistência → **preserva
CRM/PostgreSQL/n8n/Conversas** (não recria) → rollback automático se algo falhar.
Pré-requisito: `BIA_RAG_INTERNAL_SECRET` e `GEMINI_API_KEY` no `.env` da VPS.

## C. Rollback

Ver `n8n/workflows/bia_rag/rollback/README.md`. Resumo:
- Serviço: `docker compose stop bia_rag && docker compose rm -f bia_rag` (corpus
  intacto — read-only).
- Índice: `restore <backup.sqlite3>` ou `ingest-full` (reconstrói do corpus).
- WF-01: remover o nó `Buscar Contexto BNA` ou restaurar a baseline (re-vincular
  credenciais por nome no UI).
- **O corpus Markdown nunca é apagado.**

## D. Health / observabilidade

- `GET /health` (liveness, sem auth) · `GET /status` (contagens por status).
- `python -m rag.scripts.rag_cli health` → exit 0 se índice populado.
- Métricas da ingestão (retornadas por `/ingest`): total/novos/atualizados/
  órfãos removidos/dedup, por validation_status. Logs sem conteúdo dos documentos.

## E. Backend de embeddings

- Produção: `BIA_RAG_EMBEDDING_BACKEND=gemini` (reusa `GEMINI_API_KEY`).
- Testes/offline: `deterministic` (sem rede). Trocar o backend exige **reindexar**
  (`ingest-full`), pois os vetores mudam de espaço.
