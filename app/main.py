from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from app.config import PROJECT_NAME, VERSION, DESCRIPTION, API_PREFIX
from app.database import engine, Base
from app.routers import auth, users, leads, tags, pipeline, pages
from app.models.lead import Lead  # noqa: F401
from app.models.tag import Tag, lead_tags  # noqa: F401
from app.models.pipeline import Funnel, FunnelEntry, LeadHistory  # noqa: F401
from app.seed import seed_database

# Create tables & seed on startup
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title=PROJECT_NAME,
    version=VERSION,
    description=DESCRIPTION,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS — Allow all for dev (restrict in production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Include API routers
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(leads.router)
app.include_router(tags.router)
app.include_router(pipeline.router)

# Include page routes (must be last to not conflict with API routes)
app.include_router(pages.router)


@app.on_event("startup")
async def startup_event():
    """Seed database on first run."""
    seed_database()


# Health check endpoint (useful for N8N)
@app.get("/api/health", tags=["Sistema"])
async def health_check():
    """Verifica se o sistema está online. Útil para monitoramento via N8N."""
    return {"status": "online", "version": VERSION}
