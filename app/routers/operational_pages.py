from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter(tags=["Páginas Operacionais"])
templates = Jinja2Templates(directory="templates")


def _require_cookie(request: Request):
    """Verifica se o cookie de autenticação existe antes de servir páginas protegidas."""
    if not request.cookies.get("access_token"):
        return None
    return True


@router.get("/operational/boards", response_class=HTMLResponse, include_in_schema=False)
async def operational_boards_page(request: Request):
    """Serve the list of operational boards."""
    if not _require_cookie(request):
        return RedirectResponse(url="/login?next=/operational/boards", status_code=302)
    return templates.TemplateResponse("operational/boards.html", {"request": request})


@router.get("/operational/boards/{board_id}", response_class=HTMLResponse, include_in_schema=False)
async def operational_kanban_page(board_id: int, request: Request):
    """Serve the Kanban view for a specific board."""
    if not _require_cookie(request):
        return RedirectResponse(url=f"/login?next=/operational/boards/{board_id}", status_code=302)
    return templates.TemplateResponse("operational/kanban.html", {"request": request, "board_id": board_id})


@router.get("/operational/my-pending", response_class=HTMLResponse, include_in_schema=False)
async def operational_my_pending_page(request: Request):
    """Serve the personalized list of pending tasks and notifications."""
    if not _require_cookie(request):
        return RedirectResponse(url="/login?next=/operational/my-pending", status_code=302)
    return templates.TemplateResponse("operational/pending.html", {"request": request})
