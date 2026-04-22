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

# Application
APP_DOMAIN = os.getenv("APP_DOMAIN", "http://127.0.0.1:8000")

# Senha inicial do admin — OBRIGATÓRIA em produção, fallback inseguro apenas em dev
_default_admin_pwd = "admin123" if ENVIRONMENT == "development" else None
ADMIN_INITIAL_PASSWORD = os.getenv("ADMIN_INITIAL_PASSWORD", _default_admin_pwd)
if not ADMIN_INITIAL_PASSWORD:
    raise RuntimeError(
        "\n\n🔒 ERRO CRÍTICO: ADMIN_INITIAL_PASSWORD não está definida!\n"
        "Defina a variável de ambiente ADMIN_INITIAL_PASSWORD antes de rodar em produção.\n"
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
