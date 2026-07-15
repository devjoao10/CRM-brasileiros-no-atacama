"""BIA RAG service (BIA-RAG-CONTEXT-01).

RAG de produção para a BIA consultar o contexto canônico em bna_agent_context/.
Fonte de verdade = Markdown; o vector store (SQLite) é um índice derivado,
reconstruível. Nenhum secret ou PII é indexado. Conteúdo marcado
[PENDENTE_VALIDACAO] nunca é apresentado como fato confirmado.
"""
