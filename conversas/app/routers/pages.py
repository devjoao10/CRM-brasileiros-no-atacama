from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["Páginas"])
templates = Jinja2Templates(directory="templates")


def _require_cookie(request: Request):
    """Check if auth cookie exists."""
    if not request.cookies.get("access_token"):
        return None
    return True


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root(request: Request):
    """Main conversations page."""
    if not _require_cookie(request):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(name="conversas.html", request=request)


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    """Login page."""
    return templates.TemplateResponse(name="login.html", request=request)


@router.get("/templates", response_class=HTMLResponse, include_in_schema=False)
async def templates_page(request: Request):
    """Templates management page."""
    if not _require_cookie(request):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(name="templates.html", request=request)


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page(request: Request):
    """Settings page (auto-replies, business hours, quick replies)."""
    if not _require_cookie(request):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(name="settings.html", request=request)
