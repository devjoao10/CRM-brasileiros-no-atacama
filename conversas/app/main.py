import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

from app.config import PROJECT_NAME, VERSION, DESCRIPTION, ENVIRONMENT
from app.database import engine, Base
from app.auth import User  # noqa: F401 — ensure table is known
from app.models.conversation import Conversation, Message  # noqa: F401
from app.models.quick_reply import QuickReply  # noqa: F401
from app.models.template import MessageTemplate, ServiceTemplate  # noqa: F401 — CONV-CURATION-01
from app.models.auto_reply import AutoReply, BusinessHours  # noqa: F401
from app.models.api_config import ApiConfig  # noqa: F401
from app.models.message_variable import MessageVariable  # noqa: F401 — CONV-VAR-01
from app.routers import webhook, conversations, pages, auth, quick_replies, templates, settings, api_config, media, tags, notes, variables
from app.seed import seed_dev_user, seed_quick_replies, seed_templates, seed_auto_replies, seed_business_hours, CONVERSAS_SEED_DEV_DATA
from app.logging_config import configurar_logging

# BIA-V2 Fase 0 — sem isto o root logger fica em WARNING sem handler, e toda a
# trilha `.info` que este servico ja escreve morre dentro do processo.
configurar_logging()

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: cria as tabelas DESTE servico; `users` so em development.

    AUDIT-2026-08-orq — `users` e a unica tabela compartilhada, e o DONO dela e
    o CRM (app/models/user.py). O `Base.metadata` daqui contem um ESPELHO dela
    (conversas/app/auth.py), entao um `create_all()` sem filtro fazia deste
    servico um criador legitimo da tabela — bastava ele subir primeiro num banco
    novo. O espelho e necessariamente aproximado: o CRM declara `role` como
    `SAEnum(UserRole)`, que no PostgreSQL vira um TIPO ENUM NATIVO, e o espelho
    declara `VARCHAR(20)`. Nao ha como o espelho criar a coluna certa sem
    importar o enum do outro servico.

    Entao ele deixa de criar. Em development a tabela continua sendo criada,
    porque o Conversas roda isolado no proprio SQLite e precisa dela para o
    login local. Fora de development, `users` ausente e erro de implantacao — o
    CRM sobe antes e cria — e o certo e falhar cedo, nao improvisar um schema
    que o dono nao reconhece.
    """
    tabelas = list(Base.metadata.sorted_tables)
    if ENVIRONMENT != "development":
        tabelas = [t for t in tabelas if t.name != "users"]
    Base.metadata.create_all(bind=engine, tables=tabelas)
    seed_dev_user()  # Guarded internally by CONVERSAS_SEED_DEV_DATA
    if CONVERSAS_SEED_DEV_DATA:
        seed_quick_replies()
        seed_templates()
        seed_auto_replies()
        seed_business_hours()
    logger.info("Conversas app iniciado!")
    yield


app = FastAPI(
    title=PROJECT_NAME,
    version=VERSION,
    description=DESCRIPTION,
    docs_url="/docs" if ENVIRONMENT == "development" else None,
    redoc_url="/redoc" if ENVIRONMENT == "development" else None,
    lifespan=lifespan,
)

# CORS
# AUDIT-2026-08-W1B — F8: `["*"]` com `allow_credentials=True` era o default (e nao
# um opt-in): ENVIRONMENT nao definido ja valia "development". Nessa combinacao o
# Starlette ECOA o Origin do chamador em Access-Control-Allow-Origin, entao qualquer
# site aberto no navegador do atendente lia respostas autenticadas do inbox usando o
# cookie dele. Curinga nunca mais anda junto de credenciais — em dev, lista explicita.
_allowed_origins = [
    "https://conversas.crmbrasileirosnoatacama.cloud",
    "https://crm.crmbrasileirosnoatacama.cloud",
] if ENVIRONMENT != "development" else [
    "http://localhost:8001",
    "http://127.0.0.1:8001",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Security Headers Middleware (AUDIT-2026-08-W1B — F8) ─────────────
# O app nao mandava NENHUM header de seguranca. O mais grave: sem X-Frame-Options /
# frame-ancestors, o inbox era enquadravel em iframe — clickjacking numa UI cujos
# botoes ENVIAM mensagens de WhatsApp em nome da empresa. Espelha app/main.py:109-128
# e acrescenta a CSP que o CRM ainda nao tem.
#
# Rotas cujo corpo depende do estado de sessao nao podem ser reaproveitadas de cache
# (mesma razao do AUTH-LOOP-01 no CRM). /static continua cacheavel.
_NO_STORE_PATHS = {"/login", "/"}

# CSP montada a partir do que os templates REALMENTE carregam (conferido):
#   • 'unsafe-inline' em script-src: login.html tem um <script> inline (linha ~164);
#   • 'unsafe-inline' em style-src: login.html tem <style> inline e conversas.html /
#     settings.html / templates.html usam dezenas de atributos style=" ";
#   • fonts.googleapis.com em style-src e fonts.gstatic.com em font-src: a fonte Inter
#     vem de la (login.html <link> e @import no topo de conversas.css);
#   • blob: em img-src/media-src: conversas.js faz URL.createObjectURL nos downloads
#     de midia autenticada; data: em img-src cobre icones embutidos;
#   • connect-src 'self': o front so fala com o proprio dominio (os links para o CRM
#     sao navegacao via href, nao fetch).
# Tirar os 'unsafe-inline' exige extrair os inline handlers/estilos dos 4 templates —
# fora do escopo deste incidente, anotado no relatorio.
_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: blob:",
    "media-src 'self' blob:",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
])


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = _CSP
        path = request.url.path
        if (
            path in _NO_STORE_PATHS
            or path.startswith("/api/auth/")
            # cobre o 302 de QUALQUER pagina protegida para /login
            or response.headers.get("location", "").startswith("/login")
        ):
            response.headers["Cache-Control"] = "no-store"
        if ENVIRONMENT == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


app.add_middleware(SecurityHeadersMiddleware)

# Static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Routers
app.include_router(webhook.router)
app.include_router(conversations.router)
app.include_router(auth.router)  # Login endpoint (local em dev, PostgreSQL em prod)
app.include_router(quick_replies.router)
app.include_router(templates.router)
app.include_router(settings.router)
app.include_router(api_config.router)
app.include_router(media.router)  # CONV-02: preview/download autenticado de midia
app.include_router(tags.router)   # CONV-05: tags de conversa
app.include_router(notes.router)  # CONV-07: notas internas (nunca vao ao WhatsApp)
app.include_router(variables.router)  # CONV-VAR-01: variaveis dinamicas (@TOKEN)
app.include_router(pages.router)  # Pages always last (catch-all routes)


@app.get("/api/health", tags=["Sistema"])
async def health_check():
    """Health check endpoint."""
    return {"status": "online", "service": "conversas", "version": VERSION}
