from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import page_login_redirect, require_page_session
from app.config import VERSION
from app.database import get_db

router = APIRouter(tags=["Páginas"])
templates = Jinja2Templates(directory="templates")
# AUDIT-2026-08-WG — cache-busting de /static/**, mesmo mecanismo que o CRM.
# Os tokens eram literais escritos a mao (`conv-var-hard-01`, `conv-mobile-resp-02`)
# e ja tinham divergido: o MESMO conversas.css era servido sob dois tokens
# diferentes conforme a pagina. Agora um unico valor (app/config.py::VERSION)
# alimenta todo `?v=`, e trocar a versao invalida o cache de todos de uma vez.
templates.env.globals["asset_version"] = VERSION


# AUDIT-2026-08-W1B — F2: `_require_cookie` foi REMOVIDO. Ele devolvia True para
# QUALQUER valor de cookie (nunca decodificava o JWT, nunca checava `exp`, nunca
# carregava o usuario nem `is_active`), o que renderizava o shell para sessoes
# mortas e produzia o loop /login <-> /. Agora as paginas usam o mesmo resolver
# da API (`require_page_session` -> `_get_user_from_jwt`) e o 302 de falha APAGA
# o cookie invalido, para que o navegador se recupere sozinho.


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root(request: Request, db: Session = Depends(get_db)):
    """Main conversations page."""
    if not require_page_session(request, db):
        return page_login_redirect(request)
    return templates.TemplateResponse(name="conversas.html", request=request)


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request):
    """Login page."""
    return templates.TemplateResponse(name="login.html", request=request)


@router.get("/templates", response_class=HTMLResponse, include_in_schema=False)
async def templates_page(request: Request, db: Session = Depends(get_db)):
    """Templates management page."""
    if not require_page_session(request, db):
        return page_login_redirect(request)
    return templates.TemplateResponse(name="templates.html", request=request)


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    """Settings page (auto-replies, business hours, quick replies)."""
    if not require_page_session(request, db):
        return page_login_redirect(request)
    return templates.TemplateResponse(name="settings.html", request=request)
