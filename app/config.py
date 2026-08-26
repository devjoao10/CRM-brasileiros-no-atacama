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
# AUDIT-2026-08-WA — credencial da ponte CRM -> Conversas.
# Vazia (default) = ponte desligada, no-op silencioso: e exatamente o
# comportamento de hoje, entao nenhum ambiente regride por nao a configurar.
# Deve ser a API key de um usuario do CRM (o Conversas le a MESMA tabela
# `users`), nunca um segredo novo — ver app/services/conversas_bridge.py.
CONVERSAS_API_KEY = os.getenv("CONVERSAS_API_KEY", "")

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

# ─── Criacao de lead (AUDIT-2026-08-WB, F-341) — app/services/lead_creation.py
#
# AUDIT-2026-08-WF2 — o destino comercial padrao passou a ser resolvido por
# NOME, nao por ordem de id.
#
# A versao anterior caia, na falta de DEFAULT_FUNNEL_ID, no "funil ATIVO de
# MENOR id". Isso amarrava uma regra de negocio a um acidente de historico: o
# funil certo so vencia porque tinha sido criado primeiro. Criar um funil novo
# com id menor, ou desativar e recriar o principal, mandava silenciosamente
# todo lead novo para o lugar errado — e nada no sistema acusaria.
#
# `funnels.nome` e UNIQUE (app/models/pipeline.py:15). E, portanto, um
# identificador ESTAVEL do dominio, e e exatamente o que o system message do
# Gerenciador ja usa como contrato:
#
#   "Todos os leads NOVOS devem ser adicionados ao funil "Vendas: Principal",
#    sempre na etapa "Sem Contato"."
#   (n8n/workflows/live_exports/20260826_wa/gerenciador_leads.json:610)
#
# DEFAULT_FUNNEL_ID continua existindo e continua tendo prioridade — e a
# configuracao canonica de quem quer fixar por id. Mas ele agora FALHA ALTO se
# apontar para funil inexistente ou inativo, em vez de cair em outro qualquer.
_default_funnel_id_raw = os.getenv("DEFAULT_FUNNEL_ID", "").strip()
DEFAULT_FUNNEL_ID = int(_default_funnel_id_raw) if _default_funnel_id_raw.isdigit() else None

# Nome EXATO do funil comercial padrao. Usado quando DEFAULT_FUNNEL_ID nao esta
# configurado. A comparacao e case-insensitive e ignora espaco nas bordas, mas
# NAO e busca por substring: "Vendas WhatsApp" nunca casa com isto.
DEFAULT_FUNNEL_NOME = os.getenv("DEFAULT_FUNNEL_NOME", "Vendas: Principal")

# Nome (ou id) da etapa inicial dentro do funil comercial padrao. O `etapa_id`
# real gravado em producao NAO e conhecivel a partir deste repositorio — nada
# aqui cria funil, e o schema aceita tanto `sem_contato` quanto `Sem Contato`
# (app/schemas/pipeline.py:44). Por isso a resolucao compara contra o `id` E
# contra o `nome` da etapa, normalizando `_` e espaco: as duas grafias possiveis
# resolvem para a mesma etapa sem que ninguem precise adivinhar qual e a real.
DEFAULT_ETAPA_NOME = os.getenv("DEFAULT_ETAPA_NOME", "Sem Contato")

# Tag aplicada a todo lead criado via POST /api/leads. Esse endpoint recebe
# tanto o formulario do site quanto o agente n8n e nao tem como distinguir a
# origem real — uma tag configuravel e unica e honesta; inventar uma tag por
# caller nao seria (o endpoint nao sabe qual caller e). Default "" = nenhuma tag.
LEAD_TAG_ORIGEM_API = os.getenv("LEAD_TAG_ORIGEM_API", "")

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
