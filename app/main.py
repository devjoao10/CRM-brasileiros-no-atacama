import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.config import PROJECT_NAME, VERSION, DESCRIPTION, API_PREFIX, ENVIRONMENT
from app.database import engine, Base
from app.limiter import limiter
from app.routers import auth, users, leads, tags, pipeline, segments, teams, pages, tasks, analytics, ai
from app.routers import operational_boards, operational_cards, operational_flow, operational_checklists, operational_comments, operational_notifications, operational_pending, operational_pages
from app.routers import internal_tasks  # Gestão Interna (WP-GI)
from app.models.lead import Lead  # noqa: F401
from app.models.tag import Tag, lead_tags  # noqa: F401
from app.models.pipeline import Funnel, FunnelEntry, LeadHistory  # noqa: F401
from app.models.segment import Segment  # noqa: F401
from app.models.team import Team, user_teams  # noqa: F401
from app.models.task import Task  # noqa: F401
from app.models.chat import ChatSession, ChatMessage  # noqa: F401
import app.models.operational.board  # noqa: F401 — Operational Kanban models
import app.models.operational.card  # noqa: F401
import app.models.operational.checklist  # noqa: F401
import app.models.operational.notification  # noqa: F401
from app.models.internal_task import InternalTask  # noqa: F401 — Gestão Interna (WP-GI)
from app.seed import seed_database

logger = logging.getLogger(__name__)

# Diretório de uploads (arquivos gerados pela IA)
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "uploads")


def _cleanup_old_uploads(max_age_hours: int = 24):
    """Remove arquivos de upload mais antigos que max_age_hours."""
    if not os.path.isdir(UPLOAD_DIR):
        return
    now = time.time()
    cutoff = now - (max_age_hours * 3600)
    removed = 0
    for fname in os.listdir(UPLOAD_DIR):
        fpath = os.path.join(UPLOAD_DIR, fname)
        if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
            try:
                os.remove(fpath)
                removed += 1
            except OSError:
                pass
    if removed:
        logger.info(f"🧹 Limpeza de uploads: {removed} arquivos removidos (>{max_age_hours}h)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    # Startup
    Base.metadata.create_all(bind=engine)
    # Schema drift de bancos JA EXISTENTES (ALTER TABLE / indices) foi movido para
    # migrations manuais idempotentes em `migrations/` (DATA-01). NAO rodamos ALTER
    # TABLE no startup. Bancos novos sao criados completos pelo create_all() acima.
    seed_database()  # Guarded internally by SEED_INITIAL_ADMIN config flag
    _cleanup_old_uploads(max_age_hours=24)
    yield
    # Shutdown (nada por enquanto)


app = FastAPI(
    title=PROJECT_NAME,
    version=VERSION,
    description=DESCRIPTION,
    docs_url="/docs" if ENVIRONMENT == "development" else None,
    redoc_url="/redoc" if ENVIRONMENT == "development" else None,
    lifespan=lifespan,
)

# Rate Limiter — instância única em app/limiter.py (WP-SEC-03)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
# SlowAPIMiddleware aplica o default_limits global (200/minute por IP) a todas as rotas.
# Limites por rota (ex.: login 5/minute) continuam via @limiter.limit no router.
app.add_middleware(SlowAPIMiddleware)

# CORS
# AUDIT-2026-08-W1B (F8) tirou o curinga do Conversas e explicou por que:
# `["*"]` com `allow_credentials=True` faz o Starlette ECOAR o Origin do
# chamador em Access-Control-Allow-Origin, entao qualquer site aberto no
# navegador do usuario le respostas autenticadas usando o cookie dele. E nao
# era opt-in: `ENVIRONMENT` nao definido ja vale "development".
#
# AUDIT-2026-08-WF2 (revisao): o CRM tinha ficado de fora daquela correcao — o
# mesmo curinga, com o mesmo default, no servico que guarda os leads e as
# chaves de API. Assimetria entre dois servicos irmaos e onde defeito se
# esconde. Em dev, lista explicita, igual ao Conversas.
_allowed_origins = [
    "https://crm.crmbrasileirosnoatacama.cloud",
] if ENVIRONMENT != "development" else [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:8001",
    "http://127.0.0.1:8001",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Security Headers Middleware ─────────────────────────────────────
# Rotas cujo corpo depende do estado de sessão: nenhuma resposta pode ser
# reaproveitada de cache (AUTH-LOOP-01). Assets em /static continuam cacheáveis.
# AUDIT-2026-08-W1A: a regra foi INVERTIDA. A lista fechada ({"/login","/hub"})
# deixava todo o resto do CRM — /leads, /pipeline, as respostas da API — livre
# para ser cacheado, e o shell HTML dessas páginas é autenticado. Agora só
# /static (assets sem estado de sessão) escapa do no-store.
_CACHEABLE_PREFIX = "/static"

# Content-Security-Policy (AUDIT-2026-08-W1A). Não havia CSP alguma no repo e o
# JWT de sessão também vive no localStorage: qualquer sink de injeção de HTML
# vira roubo de sessão direto. Esta política é o primeiro passo PRAGMÁTICO —
# ela tranca o que não custa nada (frame-ancestors/object-src/base-uri) e
# permite o que o app realmente usa hoje:
#   • https://cdn.jsdelivr.net → marked+dompurify (ai.html), chart.js
#     (dashboard.html, relatorios.html), fullcalendar (tarefas.html);
#   • fonts.googleapis.com / fonts.gstatic.com → Inter e Caveat Brush
#     (base.html, login.html, base.css, login.css);
#   • 'unsafe-inline' em script/style porque os templates usam <script> inline e
#     atributos style= em massa. Remover 'unsafe-inline' exige de-inline dos
#     templates (ou nonce por request) e está FORA DO ESCOPO desta wave — sem
#     ele a CSP quebraria todas as páginas, o que é pior que não ter CSP.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
    "font-src 'self' data: https://fonts.gstatic.com https://cdn.jsdelivr.net; "
    "img-src 'self' data: blob:; "
    "media-src 'self' data: blob:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "base-uri 'self'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = _CSP
        if not request.url.path.startswith(_CACHEABLE_PREFIX):
            response.headers["Cache-Control"] = "no-store"
        if ENVIRONMENT == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

app.add_middleware(SecurityHeadersMiddleware)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Include API routers
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(leads.router)
app.include_router(tags.router)
app.include_router(pipeline.router)
app.include_router(segments.router)
app.include_router(teams.router)
app.include_router(tasks.router)
app.include_router(analytics.router)
app.include_router(ai.router)
# Include operational Kanban routers (OP-06 integration)
app.include_router(operational_boards.router)
app.include_router(operational_cards.router)
app.include_router(operational_flow.router)
app.include_router(operational_checklists.router)
app.include_router(operational_comments.router)
app.include_router(operational_notifications.router)
app.include_router(operational_pending.router)
app.include_router(operational_pages.router)
app.include_router(internal_tasks.router)
# Include page routes (must be last to not conflict with API routes)
app.include_router(pages.router)


# Health check endpoint (useful for N8N)
@app.get("/api/health", tags=["Sistema"])
async def health_check():
    """Verifica se o sistema está online. Útil para monitoramento via N8N."""
    return {"status": "online", "version": VERSION}
