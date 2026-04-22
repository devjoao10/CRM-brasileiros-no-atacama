import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.config import PROJECT_NAME, VERSION, DESCRIPTION, API_PREFIX, ENVIRONMENT
from app.database import engine, Base
from app.routers import auth, users, leads, tags, pipeline, segments, teams, pages, tasks, analytics, ai
from app.models.lead import Lead  # noqa: F401
from app.models.tag import Tag, lead_tags  # noqa: F401
from app.models.pipeline import Funnel, FunnelEntry, LeadHistory  # noqa: F401
from app.models.segment import Segment  # noqa: F401
from app.models.team import Team, user_teams  # noqa: F401
from app.models.task import Task  # noqa: F401
from app.models.chat import ChatSession, ChatMessage  # noqa: F401
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
    seed_database()
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

# Rate Limiter
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS — Restrito em produção, aberto em dev
_allowed_origins = ["*"] if ENVIRONMENT == "development" else [
    "https://crm.crmbrasileirosnoatacama.cloud",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Security Headers Middleware ─────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
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
# Include page routes (must be last to not conflict with API routes)
app.include_router(pages.router)


# Health check endpoint (useful for N8N)
@app.get("/api/health", tags=["Sistema"])
async def health_check():
    """Verifica se o sistema está online. Útil para monitoramento via N8N."""
    return {"status": "online", "version": VERSION}
