#!/usr/bin/env python3
"""Smoke REAL do SDK/modelo de embeddings (BIA-RAG-EMBEDDINGS-01).

Usa a API real SOMENTE quando GEMINI_API_KEY está configurada. Confirma que
`google-genai` + `gemini-embedding-001` respondem com 768 dimensões para um
documento curto e uma pergunta curta. NÃO imprime os vetores nem a API key.
NÃO gera conteúdo externo. Executado pelo bundle de deploy ANTES da ingestão.

Saída: 0 = OK · 1 = falha · 2 = pulado (sem GEMINI_API_KEY).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rag.app import config  # noqa: E402
from rag.app.embeddings import GeminiBackend, EmbeddingError  # noqa: E402


def main() -> int:
    if not os.getenv(config.GEMINI_API_KEY_ENV):
        print("SKIP: GEMINI_API_KEY nao configurada — smoke real pulado")
        return 2
    be = GeminiBackend()
    try:
        doc = be.embed_documents(["documento curto de teste do RAG da BIA"])
        q = be.embed_query("pergunta curta de teste")
    except EmbeddingError as exc:
        print(f"FALHA: embedding real falhou — {exc}")  # exc já é mensagem segura
        return 1
    finally:
        be.close()
    d_ok = len(doc) == 1 and len(doc[0]) == config.EMBEDDING_DIMENSIONS
    q_ok = len(q) == config.EMBEDDING_DIMENSIONS
    print(f"model={config.EMBEDDING_MODEL} doc_dims={len(doc[0])} query_dims={len(q)} "
          f"doc_task={config.DOCUMENT_TASK_TYPE} query_task={config.QUERY_TASK_TYPE}")
    if d_ok and q_ok:
        print("OK: embeddings reais com 768 dimensoes (documento e pergunta)")
        return 0
    print("FALHA: dimensionalidade inesperada")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
