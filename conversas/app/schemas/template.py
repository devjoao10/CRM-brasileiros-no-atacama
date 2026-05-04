import json
from datetime import datetime
from typing import Optional, List, Any
from pydantic import BaseModel, Field


# ─── Create / Update ────────────────────────────
class ButtonSchema(BaseModel):
    type: str = Field(..., description="QUICK_REPLY, URL, PHONE_NUMBER")
    text: str = Field(..., max_length=25)
    url: Optional[str] = None
    phone_number: Optional[str] = None


class TemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=512, description="snake_case, sem espaços")
    category: str = Field(..., description="MARKETING, UTILITY ou AUTHENTICATION")
    language: str = Field(default="pt_BR")
    header_type: Optional[str] = Field(None, description="TEXT, IMAGE, VIDEO, DOCUMENT")
    header_text: Optional[str] = Field(None, max_length=60)
    body_text: str = Field(..., min_length=1, max_length=1024)
    footer_text: Optional[str] = Field(None, max_length=60)
    buttons: Optional[List[ButtonSchema]] = None
    sample_values: Optional[dict] = Field(None, description='{"header": ["João"], "body": ["12345"]}')


class TemplateUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    language: Optional[str] = None
    header_type: Optional[str] = None
    header_text: Optional[str] = None
    body_text: Optional[str] = None
    footer_text: Optional[str] = None
    buttons: Optional[List[ButtonSchema]] = None
    sample_values: Optional[dict] = None


# ─── Response ────────────────────────────────────
class TemplateResponse(BaseModel):
    id: int
    name: str
    category: str
    language: str
    status: str
    header_type: Optional[str] = None
    header_text: Optional[str] = None
    body_text: str
    footer_text: Optional[str] = None
    buttons: Optional[List[Any]] = None
    sample_values: Optional[dict] = None
    meta_template_id: Optional[str] = None
    rejection_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @classmethod
    def from_orm_model(cls, obj):
        """Convert ORM model to response, parsing JSON fields."""
        buttons = None
        if obj.buttons_json:
            try:
                buttons = json.loads(obj.buttons_json)
            except (json.JSONDecodeError, TypeError):
                buttons = None

        sample_values = None
        if obj.sample_values_json:
            try:
                sample_values = json.loads(obj.sample_values_json)
            except (json.JSONDecodeError, TypeError):
                sample_values = None

        return cls(
            id=obj.id,
            name=obj.name,
            category=obj.category,
            language=obj.language,
            status=obj.status,
            header_type=obj.header_type,
            header_text=obj.header_text,
            body_text=obj.body_text,
            footer_text=obj.footer_text,
            buttons=buttons,
            sample_values=sample_values,
            meta_template_id=obj.meta_template_id,
            rejection_reason=obj.rejection_reason,
            created_at=obj.created_at,
            updated_at=obj.updated_at,
        )


class TemplateListResponse(BaseModel):
    templates: List[TemplateResponse]
    total: int


class TemplateSendRequest(BaseModel):
    to: str = Field(..., description="Número do WhatsApp no formato internacional")
    variables: Optional[dict] = Field(None, description='{"header": ["João"], "body": ["12345"]}')
