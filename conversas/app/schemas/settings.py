from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


# ─── AutoReply ────────────────────────────────────
class AutoReplyUpdate(BaseModel):
    message: Optional[str] = None
    is_active: Optional[bool] = None


class AutoReplyResponse(BaseModel):
    id: int
    trigger: str
    title: str
    message: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AutoReplyListResponse(BaseModel):
    auto_replies: List[AutoReplyResponse]
    total: int


# ─── BusinessHours ────────────────────────────────
class BusinessHoursDay(BaseModel):
    weekday: int = Field(..., ge=0, le=6)
    is_open: bool
    open_time: Optional[str] = None
    close_time: Optional[str] = None


class BusinessHoursUpdate(BaseModel):
    days: List[BusinessHoursDay]


class BusinessHoursResponse(BaseModel):
    id: int
    weekday: int
    is_open: bool
    open_time: Optional[str] = None
    close_time: Optional[str] = None

    class Config:
        from_attributes = True


class BusinessHoursListResponse(BaseModel):
    hours: List[BusinessHoursResponse]
