# Rollback — workflows RAG da BIA (BIA-RAG-CONTEXT-01)

Nenhuma alteração LIVE foi feita no n8n neste pacote. Os workflows novos são
importados **inativos**. Rollback por cenário:

## 1. Workflows novos importados (ingestion / retrieval / healthcheck)
- **Reverter:** arquivar o workflow no n8n UI (Workflow → Archive) ou excluir a
  importação. Como estão inativos e são novos, não há impacto em produção.

## 2. Integração no WF-01 (nó "Buscar Contexto BNA")
- Se o operador adicionou o nó-tool ao WF-01:
  - **Reverter:** remover o nó "Buscar Contexto BNA" dos subnodes do agente e
    republicar; ou restaurar a definição de referência
    `../baseline/WF-01_Agente_Bia_baseline.json` (sha256
    `1f22eb7be4c6ade51d38d0add35fc7d9acfb61cd8cf8ebfe82828a059d1e373c`),
    **re-vinculando as credenciais por NOME no UI** (Gemini, X-API-Key do CRM),
    pois o export do MCP não preserva bindings.
- O WF-01 permanece `active` como antes; a integração é aditiva e reversível.

## 3. Serviço RAG (container bia_rag)
- **Reverter:** `docker compose stop bia_rag && docker compose rm -f bia_rag`.
  O corpus (Markdown) NÃO é afetado (mount read-only). O volume `bia_rag_data`
  (vetores) pode ser preservado para restore ou removido — o índice é
  reconstruível por `ingest-full`.

## 4. Índice vetorial
- Backup: `python -m rag.scripts.rag_cli backup <dest.sqlite3>` (ou copiar o
  volume). Restore: `restore <src.sqlite3>`. O índice é derivado — sempre
  reconstruível a partir do corpus com `ingest-full`.

**O corpus Markdown (`bna_agent_context/`) nunca é apagado por nenhum rollback.**
