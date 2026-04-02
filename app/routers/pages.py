from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

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


@router.get("/dashboard", response_class=HTMLResponse, include_in_schema=False)
async def dashboard_page(request: Request):
    """Serve the dashboard page (auth is checked client-side via JS)."""
    return templates.TemplateResponse("dashboard.html", {"request": request})


@router.get("/leads", response_class=HTMLResponse, include_in_schema=False)
async def leads_page(request: Request):
    """Serve the leads management page."""
    return templates.TemplateResponse("leads.html", {"request": request})


@router.get("/tags", response_class=HTMLResponse, include_in_schema=False)
async def tags_page(request: Request):
    """Serve the tags management page."""
    return templates.TemplateResponse("tags.html", {"request": request})


@router.get("/pipeline", response_class=HTMLResponse, include_in_schema=False)
async def pipeline_page(request: Request):
    """Serve the pipeline/kanban page."""
    return templates.TemplateResponse("pipeline.html", {"request": request})


@router.get("/segmentacao", response_class=HTMLResponse, include_in_schema=False)
async def segmentacao_page(request: Request):
    """Serve the lead segmentation page."""
    return templates.TemplateResponse("segmentacao.html", {"request": request})


@router.get("/equipe", response_class=HTMLResponse, include_in_schema=False)
async def equipe_page(request: Request):
    """Serve the users and teams management page."""
    return templates.TemplateResponse("equipes.html", {"request": request})

@router.get("/tarefas", response_class=HTMLResponse, include_in_schema=False)
async def tarefas_page(request: Request):
    """Serve the tasks management page."""
    return templates.TemplateResponse("tarefas.html", {"request": request})
@router.get("/relatorios", response_class=HTMLResponse, include_in_schema=False)
async def relatorios_page(request: Request):
    """Serve the advanced reports page."""
    return templates.TemplateResponse("relatorios.html", {"request": request})
