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

# AUDIT-2026-08-WF2 (D2) — cabecalho de autenticacao do webhook da Bia.
#
# `/webhook/agent-bia` esta aberto na internet, publicado pelo Traefik. O
# webhook irmao (`/webhook/gerenciador-leads`) ja recebeu Header Auth (D1), mas
# este NAO podia receber: o Conversas nao mandava cabecalho nenhum, entao ligar
# a autenticacao no n8n derrubaria a Bia no mesmo instante.
#
# Com estas duas variaveis o Conversas passa a enviar o cabecalho. Ambas vazias
# (default) = nenhum cabecalho, exatamente o comportamento de hoje — nenhum
# ambiente regride por nao configurar. O nome e configuravel porque a credencial
# "Header Auth" do n8n deixa quem cria escolher o nome do cabecalho; tem de
# bater com o que estiver la.
#
# ORDEM OBRIGATORIA no deploy: configurar aqui e subir o Conversas PRIMEIRO;
# so depois ligar a autenticacao no n8n. O inverso corta a Bia.
N8N_WEBHOOK_AUTH_HEADER = os.getenv("N8N_WEBHOOK_AUTH_HEADER", "").strip()
N8N_WEBHOOK_AUTH_VALUE = os.getenv("N8N_WEBHOOK_AUTH_VALUE", "").strip()

# ─── IA Gemini (BIA-V2 Fases 8 e 9) ───────────────
# Mesmo nome de env var do CRM (app/config.py) — a chave e a MESMA credencial
# Gemini; o SDK que a consome diverge do CRM (google-genai aqui, nao
# google.generativeai — ver o docstring de app/v2/interpretador.py para o
# motivo). Vazio por default, sem RuntimeError: mesmo padrao lazy-check do
# CRM — ausencia so falha na hora de chamar a IA, nunca no boot.
#
# UNICA definicao para os DOIS consumidores V2: app/v2/interpretador.py
# (Fase 8, `_chamar_llm`) e app/v2/comunicador.py (Fase 9, `_chamar_gemini`).
# As duas fases originalmente declaravam esta variavel em blocos separados —
# a Fase 9 tambem tinha decidido usar google.generativeai para espelhar
# app/routers/ai.py (CRM); uma reconciliacao pos-implementacao padronizou os
# dois consumidores V2 em google-genai (o SDK ja verificado pela Fase 8) e
# uniu a declaracao aqui. Cada consumidor cria seu proprio
# `genai.Client(api_key=GEMINI_API_KEY)` — sem estado compartilhado alem da
# leitura desta constante.
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# ─── Media storage (CONV-02) ─────────────────────
# Espelho local dos binarios de midia (media_assets.local_path e RELATIVO a este dir).
# Default fica sob conversas/uploads/ (ja coberto pelo .gitignore). Em producao:
# apontar CONVERSAS_MEDIA_DIR para um volume Docker persistente.
MEDIA_STORAGE_DIR = os.getenv(
    "CONVERSAS_MEDIA_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "uploads", "media"),
)
