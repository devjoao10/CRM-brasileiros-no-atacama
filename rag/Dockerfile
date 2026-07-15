FROM python:3.11-slim

ENV TZ="America/Sao_Paulo" \
    PYTHONUNBUFFERED=1 \
    BIA_RAG_DB_PATH=/data/bna_bia_context.sqlite3 \
    BIA_RAG_CORPUS_DIR=/corpus \
    BIA_RAG_EMBEDDING_BACKEND=gemini

WORKDIR /app

COPY rag/requirements.txt ./rag/requirements.txt
RUN pip install --no-cache-dir -r rag/requirements.txt

# Código do serviço (o corpus é MONTADO read-only, não copiado — fonte de verdade
# permanece no host/repo; a imagem não embute os documentos).
COPY rag/app ./rag/app
COPY rag/scripts ./rag/scripts
COPY rag/__init__.py ./rag/__init__.py

RUN mkdir -p /data

EXPOSE 8100

# Healthcheck interno (não depende de rede externa).
HEALTHCHECK --interval=30s --timeout=5s --retries=5 --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8100/health').status==200 else 1)"

CMD ["uvicorn", "rag.app.service:app", "--host", "0.0.0.0", "--port", "8100"]
