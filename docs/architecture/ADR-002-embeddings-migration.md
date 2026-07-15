# ADR-002 — Migração de embeddings para google-genai (BIA-RAG-EMBEDDINGS-01)

**Status:** aceito · **Data:** 2026-07-15 · **Substitui parte do ADR-001**

## Contexto
O primeiro deploy real do RAG (BIA-RAG-CONTEXT-01) falhou **com segurança** no
`ingest-full`: o SDK `google-generativeai` + modelo `text-embedding-004`
retornam `404 ... is not found for API version v1beta` e o pacote está
descontinuado. O bundle fez rollback e removeu só o `bia_rag`; CRM/PostgreSQL/
Conversas/n8n intactos; `BIA_RAG_INTERNAL_SECRET` já criado; o volume
`bia_rag_data` pode ter ficado vazio/parcial.

## Decisão
- **SDK:** `google-generativeai` → **`google-genai`** (`from google import genai`).
  Removido `google-generativeai` das dependências do serviço RAG.
- **Modelo:** `text-embedding-004` → **`gemini-embedding-001`**.
- **Contratos separados** documento/pergunta:
  - `embed_documents` → task_type `RETRIEVAL_DOCUMENT`
  - `embed_query` → task_type `QUESTION_ANSWERING` (fallback explícito e testado
    `RETRIEVAL_QUERY` se a conta rejeitar QA). Nunca `SEMANTIC_SIMILARITY`.
- **Dimensionalidade:** 768, igual em ingestão e consulta. Vetores
  **normalizados** (Python puro) antes de armazenar e comparar (dim reduzida não
  garante normalização automática). Sem numpy.
- **Batching:** lotes de 32 (config), ordem preservada, valida
  `n_textos == n_vetores` e `dim == 768` **antes** do upsert; falha antes de
  gravar índice inconsistente.
- **Cliente:** criado com `GEMINI_API_KEY` (nunca impressa), fechado quando
  suportado. Retry só em 429/500/502/503/504 (backoff pequeno); **nunca** em
  400/401/403/404/config inválida. Erros não incluem o texto dos chunks.
- **Compatibilidade de índice:** metadados persistidos
  (`embedding_provider/model/dimensions/document_task_type/query_task_type/
  index_version/corpus_hash/indexed_at`). Índice criado com
  `text-embedding-004`, outro backend, outra dimensão, ou sem metadados é
  **incompatível** → força **full rebuild atômico**. Nunca mistura vetores de
  espaços diferentes.
- **Atomicidade:** rebuild constrói banco **temporário**, valida + smoke, e só
  então troca pelo ativo (`os.replace`), preservando backup. Em falha, o ativo
  anterior permanece intacto. Mesmo com índice vazio/parcial da 1ª tentativa,
  cria backup antes de reconstruir. Corpus Markdown nunca é apagado.

## Testes
- 35 testes de pipeline (mantidos) + 20 testes de embeddings/atomicidade
  (fake client, sem rede): task types, 768 dims, normalização, ordem, batching,
  404-não-retenta, 429-retenta-limitado, segredo-fora-do-erro, índice antigo
  incompatível → rebuild, temp-não-substitui-em-falha, troca atômica em sucesso,
  recuperação de índice parcial.
- Smoke **real** opcional (`rag/scripts/embedding_smoke.py`) só com
  `GEMINI_API_KEY`: embeda doc+pergunta, confirma 768 dims, sem imprimir vetores/
  chave. O bundle o executa **antes** da ingestão.

## Consequência
O modelo só é declarado validado em produção após a saída real do smoke do
bundle (o ambiente de desenvolvimento não tem a chave). Índice reconstruído do
zero no espaço `gemini-embedding-001/768`.
