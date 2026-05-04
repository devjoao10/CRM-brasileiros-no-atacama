"""
Schemas for API Configuration (WhatsApp Business API credentials).
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class ApiConfigUpdate(BaseModel):
    """Payload for updating API credentials."""
    meta_access_token: Optional[str] = Field(None, description="Meta Graph API Access Token (permanent)")
    meta_phone_number_id: Optional[str] = Field(None, description="Phone Number ID from Meta Business")
    meta_waba_id: Optional[str] = Field(None, description="WhatsApp Business Account ID")
    meta_verify_token: Optional[str] = Field(None, description="Token de verificação do webhook (definido por você)")
    meta_api_version: Optional[str] = Field(None, description="Versão da Graph API (ex: v21.0)")
    webhook_url: Optional[str] = Field(None, description="URL do webhook configurada no Meta")


class ApiConfigResponse(BaseModel):
    """Response with API config (token is masked for security)."""
    id: int
    has_access_token: bool
    meta_phone_number_id: Optional[str] = None
    meta_waba_id: Optional[str] = None
    meta_verify_token: Optional[str] = None
    meta_api_version: str
    webhook_url: Optional[str] = None
    is_connected: bool
    updated_at: Optional[datetime] = None

    @classmethod
    def from_model(cls, config):
        """Convert model to response, masking the access token."""
        return cls(
            id=config.id,
            has_access_token=bool(config.meta_access_token),
            meta_phone_number_id=config.meta_phone_number_id,
            meta_waba_id=config.meta_waba_id,
            meta_verify_token=config.meta_verify_token,
            meta_api_version=config.meta_api_version or "v21.0",
            webhook_url=config.webhook_url,
            is_connected=config.is_connected,
            updated_at=config.updated_at,
        )


class ApiConfigTestResponse(BaseModel):
    """Response from testing the API connection."""
    success: bool
    error: Optional[str] = None
    waba_name: Optional[str] = None
    waba_id: Optional[str] = None
    currency: Optional[str] = None
    phone_display: Optional[str] = None
    phone_name: Optional[str] = None
    phone_quality: Optional[str] = None
