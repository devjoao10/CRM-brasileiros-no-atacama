from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import page_login_redirect, require_page_session
from app.database import get_db

router = APIRouter(tags=["Páginas"])
templates = Jinja2Templates(directory="templates")


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root():
    """Redirect to login page."""
    return RedirectResponse(url="/login", status_code=302)


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    """Serve the login page."""
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/hub", response_class=HTMLResponse, include_in_schema=False)
async def hub_page(request: Request, db: Session = Depends(get_db)):
    """Serve the sector hub (post-login landing) — WP-UX-02."""
    if not require_page_session(request, db):
        return page_login_redirect(request)
    return templates.TemplateResponse("hub.html", {"request": request})


@router.get("/gestao/pendencias", response_class=HTMLResponse, include_in_schema=False)
async def gestao_pendencias_page(request: Request, db: Session = Depends(get_db)):
    """Serve the internal tasks hub (Gestão Interna) — WP-GI-03."""
    if not require_page_session(request, db):
        return page_login_redirect(request, "/gestao/pendencias")
    return templates.TemplateResponse("gestao/pendencias.html", {"request": request})


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_page(request: Request, db: Session = Depends(get_db)):
    """Serve the dashboard page."""
    if not require_page_session(request, db):
        return page_login_redirect(request)
    return templates.TemplateResponse("dashboard.html", {"request": request})


@router.get("/leads", response_class=HTMLResponse, include_in_schema=False)
async def leads_page(request: Request, db: Session = Depends(get_db)):
    """Serve the leads management page."""
    if not require_page_session(request, db):
        return page_login_redirect(request)
    return templates.TemplateResponse("leads.html", {"request": request})


@router.get("/tags", response_class=HTMLResponse, include_in_schema=False)
async def tags_page(request: Request, db: Session = Depends(get_db)):
    """Serve the tags management page."""
    if not require_page_session(request, db):
        return page_login_redirect(request)
    return templates.TemplateResponse("tags.html", {"request": request})


@router.get("/pipeline", response_class=HTMLResponse, include_in_schema=False)
async def pipeline_page(request: Request, db: Session = Depends(get_db)):
    """Serve the pipeline/kanban page."""
    if not require_page_session(request, db):
        return page_login_redirect(request)
    return templates.TemplateResponse("pipeline.html", {"request": request})


@router.get("/segmentacao", response_class=HTMLResponse, include_in_schema=False)
async def segmentacao_page(request: Request, db: Session = Depends(get_db)):
    """Serve the lead segmentation page."""
    if not require_page_session(request, db):
        return page_login_redirect(request)
    return templates.TemplateResponse("segmentacao.html", {"request": request})


@router.get("/equipe", response_class=HTMLResponse, include_in_schema=False)
async def equipe_page(request: Request, db: Session = Depends(get_db)):
    """Serve the users and teams management page."""
    if not require_page_session(request, db):
        return page_login_redirect(request)
    return templates.TemplateResponse("equipes.html", {"request": request})

@router.get("/tarefas", response_class=HTMLResponse, include_in_schema=False)
async def tarefas_page(request: Request, db: Session = Depends(get_db)):
    """Serve the tasks management page."""
    if not require_page_session(request, db):
        return page_login_redirect(request)
    return templates.TemplateResponse("tarefas.html", {"request": request})
@router.get("/relatorios", response_class=HTMLResponse, include_in_schema=False)
async def relatorios_page(request: Request, db: Session = Depends(get_db)):
    """Serve the advanced reports page."""
    if not require_page_session(request, db):
        return page_login_redirect(request)
    return templates.TemplateResponse("relatorios.html", {"request": request})

@router.get("/ai", response_class=HTMLResponse, include_in_schema=False)
async def ai_page(request: Request, db: Session = Depends(get_db)):
    """Serve the AI Assistant integration page."""
    if not require_page_session(request, db):
        return page_login_redirect(request)
    return templates.TemplateResponse("ai.html", {"request": request})
