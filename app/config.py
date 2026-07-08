import os
import secrets

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Environment
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

# Security
# Em produção, SECRET_KEY DEVE ser definida via variável de ambiente.
# Em dev, gera uma chave aleatória por sessão (tokens invalidam ao reiniciar, mas é seguro).
_default_key = secrets.token_urlsafe(64) if ENVIRONMENT == "development" else None
SECRET_KEY = os.getenv("SECRET_KEY", _default_key)
if not SECRET_KEY:
    raise RuntimeError(
        "\n\n🔒 ERRO CRÍTICO: SECRET_KEY não está definida!\n"
        "Defina a variável de ambiente SECRET_KEY antes de rodar em produção.\n"
        "Gere uma com: python -c \"import secrets; print(secrets.token_urlsafe(64))\"\n"
    )
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))  # 8 hours

# Database — SQLite em dev local, PostgreSQL em produção/Docker
_default_db = "sqlite:///./crm_atacama.db" if ENVIRONMENT == "development" else None
DATABASE_URL = os.getenv("DATABASE_URL", _default_db)
if not DATABASE_URL:
    raise RuntimeError(
        "\n\n🔒 ERRO CRÍTICO: DATABASE_URL não está definida!\n"
        "Defina a variável de ambiente DATABASE_URL antes de rodar em produção.\n"
        "Exemplo: postgresql://crm_app:SENHA@postgres:5432/crm_atacama\n"
    )

# Conexão read-only para queries da IA (usa user separado com permissões limitadas)
# Em dev local, usa a mesma URL (SQLite não tem users separados)
DATABASE_READONLY_URL = os.getenv("DATABASE_READONLY_URL", DATABASE_URL)

# ─── Autenticação interna da IA (Perpétua) — PERPETUA-INTERNAL-AUTH-01 ──
# Segredo BACKEND-ONLY usado para assinar/validar (HMAC-SHA256) as chamadas
# internas que a Perpétua faz às rotas /api/ em nome do usuário logado.
# Com isso, qualquer usuário autenticado usa as ferramentas da IA sem precisar
# gerar manualmente uma API Key.
#   • NUNCA exponha este valor no frontend nem em respostas de API.
#   • Sem este valor, as ferramentas internas da IA ficam DESATIVADAS (fail-safe):
#     a Perpétua ainda responde e faz SELECTs, mas `call_internal_api` recusa.
#   • Gere com:  openssl rand -base64 32
INTERNAL_AI_AUTH_SECRET = os.getenv("INTERNAL_AI_AUTH_SECRET", "")
# Janela máxima de defasagem de relógio (segundos) aceita ao validar o timestamp
# assinado. Protege contra replay de requisições internas antigas. Default: 300s.
INTERNAL_AI_AUTH_MAX_SKEW_SECONDS = int(os.getenv("INTERNAL_AI_AUTH_MAX_SKEW_SECONDS", "300"))

# Application
APP_DOMAIN = os.getenv("APP_DOMAIN", "http://127.0.0.1:8000")
CONVERSAS_BASE_URL = os.getenv("CONVERSAS_BASE_URL", "http://127.0.0.1:8001")

# ─── Seed do Admin Inicial ────────────────────────────────────────────
# SEED_INITIAL_ADMIN controla se o admin inicial é criado no startup.
# Em dev: padrão true (conveniência local). Em prod: padrão false (segurança).
SEED_INITIAL_ADMIN = os.getenv(
    "SEED_INITIAL_ADMIN", "true" if ENVIRONMENT == "development" else "false"
).lower() in ("true", "1", "yes")

ADMIN_INITIAL_EMAIL = os.getenv("ADMIN_INITIAL_EMAIL", "admin@brasileirosnoatacama.com")
ADMIN_INITIAL_PASSWORD = os.getenv("ADMIN_INITIAL_PASSWORD", "")

# Se seed está ativado, a senha é obrigatória — nunca usa fallback hardcoded.
if SEED_INITIAL_ADMIN and not ADMIN_INITIAL_PASSWORD:
    raise RuntimeError(
        "\n\n🔒 ERRO: SEED_INITIAL_ADMIN está ativado mas ADMIN_INITIAL_PASSWORD não foi definida!\n"
        "Defina ADMIN_INITIAL_PASSWORD no .env ou desative o seed com SEED_INITIAL_ADMIN=false.\n"
    )

# Upload
MAX_UPLOAD_SIZE_BYTES = int(os.getenv("MAX_UPLOAD_SIZE_BYTES", str(10 * 1024 * 1024)))  # 10MB

# API Key expiry (days, 0 = never expires)
API_KEY_EXPIRY_DAYS = int(os.getenv("API_KEY_EXPIRY_DAYS", "0"))

# E-mail (SMTP)
MAIL_USERNAME = os.getenv("MAIL_USERNAME", "seu_email@empresa.com")
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "sua_senha")
MAIL_FROM = os.getenv("MAIL_FROM", "seu_email@empresa.com")
MAIL_PORT = int(os.getenv("MAIL_PORT", 587))
MAIL_SERVER = os.getenv("MAIL_SERVER", "smtp.hostinger.com")
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "Brasileiros no Atacama")

# API
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
API_PREFIX = "/api"
PROJECT_NAME = "CRM Brasileiros no Atacama"
VERSION = "1.0.0"
DESCRIPTION = """
CRM interno da Brasileiros no Atacama.

## Autenticação
- **Frontend**: JWT via cookie ou header `Authorization: Bearer <token>`
- **Integrações (N8N)**: Header `X-API-Key: <sua-api-key>`

## Recursos disponíveis
- **Auth**: Login, geração de API Key, dados do usuário
- **Users**: CRUD completo de usuários (admin)
"""
