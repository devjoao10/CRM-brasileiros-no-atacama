import os
import secrets

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
# AUDIT-2026-08-W1B — F1: o fallback anterior era uma CONSTANTE literal de dev,
# versionada neste repositorio (logo, publica). Como o Conversas
# compartilha SECRET_KEY *e* a tabela `users` com o CRM (app/), qualquer pessoa
# com acesso ao repo podia assinar {"sub": "<email de um admin>"} e ser admin nos
# DOIS servicos — o token e aceito por ambos. Espelha exatamente app/config.py:16-23:
#   • dev  -> chave aleatoria POR PROCESSO (tokens morrem no restart, mas nao existe
#             segredo previsivel em lugar nenhum);
#   • prod -> RuntimeError. Subir sem segredo e pior do que nao subir, entao a
#             falha e ruidosa no boot em vez de silenciosa em runtime.
# `os.getenv` devolve "" quando a var existe vazia; o `if not SECRET_KEY` cobre isso.
_default_key = secrets.token_urlsafe(64) if ENVIRONMENT == "development" else None
SECRET_KEY = os.getenv("SECRET_KEY", _default_key)
if not SECRET_KEY:
    raise RuntimeError(
        "\n\n🔒 ERRO CRÍTICO: SECRET_KEY não está definida!\n"
        "O Conversas compartilha a chave de assinatura com o CRM — sem ela\n"
        "qualquer token seria forjável em AMBOS os serviços.\n"
        "Defina SECRET_KEY (a MESMA do CRM) antes de rodar fora de development.\n"
        "Gere uma com: python -c \"import secrets; print(secrets.token_urlsafe(64))\"\n"
    )
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

# ─── Media storage (CONV-02) ─────────────────────
# Espelho local dos binarios de midia (media_assets.local_path e RELATIVO a este dir).
# Default fica sob conversas/uploads/ (ja coberto pelo .gitignore). Em producao:
# apontar CONVERSAS_MEDIA_DIR para um volume Docker persistente.
MEDIA_STORAGE_DIR = os.getenv(
    "CONVERSAS_MEDIA_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads", "media"),
)
