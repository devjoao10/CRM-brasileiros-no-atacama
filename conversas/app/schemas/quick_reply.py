from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


class QuickReplyCreate(BaseModel):
    shortcut: str = Field(..., min_length=2, max_length=50)
    title: str = Field(..., min_length=1, max_length=100)
    content: str = Field(..., min_length=1, max_length=5000)
    category: Optional[str] = None


class QuickReplyUpdate(BaseModel):
    shortcut: Optional[str] = None
    title: Optional[str] = None
    content: Optional[str] = None
    category: Optional[str] = None
    is_active: Optional[bool] = None


class QuickReplyResponse(BaseModel):
    id: int
    shortcut: str
    title: str
    content: str
    category: Optional[str] = None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class QuickReplyListResponse(BaseModel):
    quick_replies: List[QuickReplyResponse]
    total: int
