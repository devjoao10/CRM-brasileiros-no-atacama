from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import page_login_redirect, require_page_session
from app.database import get_db

router = APIRouter(tags=["Páginas Operacionais"])
templates = Jinja2Templates(directory="templates")


@router.get("/operational/boards", response_class=HTMLResponse, include_in_schema=False)
async def operational_boards_page(request: Request, db: Session = Depends(get_db)):
    """Serve the list of operational boards."""
    if not require_page_session(request, db):
        return page_login_redirect(request, "/operational/boards")
    return templates.TemplateResponse("operational/boards.html", {"request": request})


@router.get("/operational/boards/{board_id}", response_class=HTMLResponse, include_in_schema=False)
async def operational_kanban_page(board_id: int, request: Request, db: Session = Depends(get_db)):
    """Serve the Kanban view for a specific board."""
    if not require_page_session(request, db):
        return page_login_redirect(request, f"/operational/boards/{board_id}")
    return templates.TemplateResponse("operational/kanban.html", {"request": request, "board_id": board_id})


@router.get("/operational/my-pending", response_class=HTMLResponse, include_in_schema=False)
async def operational_my_pending_page(request: Request, db: Session = Depends(get_db)):
    """Serve the personalized list of pending tasks and notifications."""
    if not require_page_session(request, db):
        return page_login_redirect(request, "/operational/my-pending")
    return templates.TemplateResponse("operational/pending.html", {"request": request})
