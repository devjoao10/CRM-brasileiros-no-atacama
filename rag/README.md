# rag/ — Serviço RAG da BIA (BIA-RAG-CONTEXT-01)

RAG de produção para a BIA consultar `bna_agent_context/`. Arquitetura em
[../docs/architecture/ADR-001-bia-rag.md](../docs/architecture/ADR-001-bia-rag.md).
Runbooks em [../docs/runbooks/bia_rag_runbooks.md](../docs/runbooks/bia_rag_runbooks.md).

## Estrutura
```
rag/app/        corpus.py chunker.py embeddings.py store.py ingest.py retrieve.py answer.py service.py config.py
rag/scripts/    rag_cli.py         (ingest/search/status/health/backup/restore/manifest)
rag/tests/      test_rag.py + fixtures/corpus/  (suite hermetica, backend deterministico)
rag/manifests/  corpus_manifest.json           (artefato do corpus)
rag/Dockerfile  rag/requirements.txt
```

## Quickstart (offline, sem API key)
```
python -m rag.scripts.rag_cli ingest-full       # backend deterministico (default)
python -m rag.scripts.rag_cli search "como a Bia faz o handoff"
python -m rag.tests.test_rag                     # suite hermetica (35 checagens)
```

## Produção
Serviço `bia_rag` no docker-compose (interno, sem exposicao publica), corpus
montado read-only, embeddings Gemini `text-embedding-004`. n8n chama
`http://bia_rag:8100/search` com header `X-BIA-RAG-Token`.

## Garantias
- Fonte de verdade = Markdown; índice SQLite reconstruível.
- Conteúdo `[PENDENTE_VALIDACAO]` **nunca** entra no contexto factual (filtro SQL).
- Retrieval antes da geração; fontes internas sempre retornadas.
- No-answer em vez de invenção; conflito prioriza validado/canônico.
- Documentos são **dados**, não instruções (guardrail anti-injection em answer.py).
