from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["Páginas"])
templates = Jinja2Templates(directory="templates")


def _require_cookie(request: Request):
    """Verifica se o cookie de autenticação existe antes de servir páginas protegidas."""
    if not request.cookies.get("access_token"):
        return None
    return True


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
    """Serve the dashboard page."""
    if not _require_cookie(request):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("dashboard.html", {"request": request})


@router.get("/leads", response_class=HTMLResponse, include_in_schema=False)
async def leads_page(request: Request):
    """Serve the leads management page."""
    if not _require_cookie(request):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("leads.html", {"request": request})


@router.get("/tags", response_class=HTMLResponse, include_in_schema=False)
async def tags_page(request: Request):
    """Serve the tags management page."""
    if not _require_cookie(request):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("tags.html", {"request": request})


@router.get("/pipeline", response_class=HTMLResponse, include_in_schema=False)
async def pipeline_page(request: Request):
    """Serve the pipeline/kanban page."""
    if not _require_cookie(request):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("pipeline.html", {"request": request})


@router.get("/segmentacao", response_class=HTMLResponse, include_in_schema=False)
async def segmentacao_page(request: Request):
    """Serve the lead segmentation page."""
    if not _require_cookie(request):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("segmentacao.html", {"request": request})


@router.get("/equipe", response_class=HTMLResponse, include_in_schema=False)
async def equipe_page(request: Request):
    """Serve the users and teams management page."""
    if not _require_cookie(request):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("equipes.html", {"request": request})

@router.get("/tarefas", response_class=HTMLResponse, include_in_schema=False)
async def tarefas_page(request: Request):
    """Serve the tasks management page."""
    if not _require_cookie(request):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("tarefas.html", {"request": request})
@router.get("/relatorios", response_class=HTMLResponse, include_in_schema=False)
async def relatorios_page(request: Request):
    """Serve the advanced reports page."""
    if not _require_cookie(request):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("relatorios.html", {"request": request})

@router.get("/ai", response_class=HTMLResponse, include_in_schema=False)
async def ai_page(request: Request):
    """Serve the AI Assistant integration page."""
    if not _require_cookie(request):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("ai.html", {"request": request})
