FROM python:3.11-slim

# Set timezone
ENV TZ="America/Sao_Paulo"

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# AUDIT-2026-08-W1E (F10): sem USER, o uvicorn E toda ferramenta invocada pela
# IA rodavam como UID 0 dentro do container — qualquer RCE virava root direto.
# /app/uploads e criado e chowneado AQUI porque o app faz makedirs em runtime e
# um usuario nao-root nao consegue criar diretorio em /app depois do build.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/uploads \
    && chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE 8000

# AUDIT-2026-08-W1E (F10): sem --proxy-headers o rate limiter enxergava apenas o
# IP do container do Traefik, entao o limite de 5 logins/min valia para a
# INTERNET INTEIRA somada. --forwarded-allow-ips=* so e aceitavel porque este
# servico nao tem `ports:` (apenas `expose:`) — nao existe caminho ate ele que
# nao passe pelo Traefik, entao o X-Forwarded-For nao pode ser forjado de fora.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
