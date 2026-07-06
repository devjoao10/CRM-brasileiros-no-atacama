import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.config import PROJECT_NAME, VERSION, DESCRIPTION, ENVIRONMENT
from app.database import engine, Base
from app.auth import User  # noqa: F401 — ensure table is known
from app.models.conversation import Conversation, Message  # noqa: F401
from app.models.quick_reply import QuickReply  # noqa: F401
from app.models.template import MessageTemplate  # noqa: F401
from app.models.auto_reply import AutoReply, BusinessHours  # noqa: F401
from app.models.api_config import ApiConfig  # noqa: F401
from app.routers import webhook, conversations, pages, auth, quick_replies, templates, settings, api_config, media
from app.seed import seed_dev_user, seed_quick_replies, seed_templates, seed_auto_replies, seed_business_hours, CONVERSAS_SEED_DEV_DATA

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create tables if they don't exist."""
    Base.metadata.create_all(bind=engine)
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
_allowed_origins = ["*"] if ENVIRONMENT == "development" else [
    "https://conversas.crmbrasileirosnoatacama.cloud",
    "https://crm.crmbrasileirosnoatacama.cloud",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
app.include_router(pages.router)  # Pages always last (catch-all routes)


@app.get("/api/health", tags=["Sistema"])
async def health_check():
    """Health check endpoint."""
    return {"status": "online", "service": "conversas", "version": VERSION}
