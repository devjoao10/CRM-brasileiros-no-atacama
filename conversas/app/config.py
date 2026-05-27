import os

# ─── App ─────────────────────────────────────────
PROJECT_NAME = "Conversas — Brasileiros no Atacama"
VERSION = "1.0.0"
DESCRIPTION = "Plataforma de conversas WhatsApp integrada ao CRM"
API_PREFIX = "/api"
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

# ─── Database ────────────────────────────────────
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./conversas.db"
)

# ─── Auth ────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-key-change-me")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))

# ─── CRM Integration ────────────────────────────
CRM_BASE_URL = os.getenv("CRM_BASE_URL", "http://127.0.0.1:8000")

# ─── Meta Cloud API (WhatsApp) ───────────────────
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "")
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
META_PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "")
META_API_VERSION = os.getenv("META_API_VERSION", "v21.0")
META_API_BASE = f"https://graph.facebook.com/{META_API_VERSION}"
META_WABA_ID = os.getenv("META_WABA_ID", "")  # WhatsApp Business Account ID
META_APP_SECRET = os.getenv("META_APP_SECRET", "")  # App Secret — valida X-Hub-Signature-256

# ─── N8N Integration ─────────────────────────────
N8N_BASE_URL = os.getenv("N8N_BASE_URL", "http://127.0.0.1:5678")
N8N_AGENT_ENABLED = os.getenv("N8N_AGENT_ENABLED", "false").lower() == "true"
