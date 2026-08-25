FROM python:3.12-slim

# Set timezone
ENV TZ="America/Sao_Paulo"

WORKDIR /app

# System deps (libpq for PostgreSQL)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# AUDIT-2026-08-W1E (F10): mesmo motivo do Dockerfile da raiz — nada aqui
# precisa de root. CUIDADO: /app/data/media e o mountpoint do volume
# conversas_media; o diretorio PRECISA existir e ja pertencer ao appuser no
# build, senao o Docker cria o mount como root e o container morre ao gravar
# midia (OPS-CONV-MEDIA-01 / CONVERSAS_MEDIA_DIR).
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data/media \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8001

# AUDIT-2026-08-W1E (F10): ver Dockerfile da raiz. `expose:` sem `ports:` =
# so o Traefik alcanca este servico, por isso --forwarded-allow-ips=* e seguro.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--proxy-headers", "--forwarded-allow-ips=*"]
