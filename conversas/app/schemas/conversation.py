from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field


# ─── Message Schemas ─────────────────────────────
class MessageCreate(BaseModel):
    content: str = Field(..., min_length=1, max_length=5000)
    msg_type: str = Field(default="text")
    media_url: Optional[str] = None
    template_name: Optional[str] = None


class MessageResponse(BaseModel):
    id: int
    conversation_id: int
    direction: str
    content: str
    msg_type: str
    media_url: Optional[str] = None
    whatsapp_msg_id: Optional[str] = None
    status: str
    created_at: datetime
    # CONV-08b — metadados de integridade de envio (outbound)
    last_error: Optional[str] = None
    send_attempts: Optional[int] = 0
    last_attempt_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ─── Conversation Schemas ────────────────────────
class ConversationUpdate(BaseModel):
    status: Optional[str] = None
    atendente_id: Optional[int] = None
    is_bot_active: Optional[bool] = None
    responsavel_id: Optional[int] = None


class ConversationResponse(BaseModel):
    id: int
    lead_id: int
    whatsapp: str
    nome: Optional[str] = None
    status: str
    ultimo_msg: Optional[str] = None
    unread_count: int
    atendente_id: Optional[int] = None
    is_bot_active: bool
    responsavel_id: Optional[int] = None
    responsavel_nome: Optional[str] = None
    last_customer_msg_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ConversationDetail(ConversationResponse):
    messages: List[MessageResponse] = []

    class Config:
        from_attributes = True


class ConversationListResponse(BaseModel):
    conversations: List[ConversationResponse]
    total: int


# ─── Notification Schemas (N8N Integration) ──────
class NotificationCreate(BaseModel):
    """Payload sent by N8N workflows to send WhatsApp notifications."""
    to: str = Field(..., min_length=10, max_length=20, description="Número WhatsApp do destinatário (ex: 5511999999999)")
    message: str = Field(..., min_length=1, max_length=5000, description="Texto da mensagem")


class InitiateConversation(BaseModel):
    """Payload para iniciar uma conversa nova com um lead que ainda não tem conversa."""
    whatsapp: str = Field(..., min_length=10, max_length=20, description="Número WhatsApp no formato 55XXXXXXXXXXX")
    nome: Optional[str] = Field(None, description="Nome do lead")
    lead_id: Optional[int] = Field(None, description="ID do lead no CRM para vincular")
    template_name: Optional[str] = Field(None, description="Nome do template WhatsApp a enviar")
    template_language: Optional[str] = Field(default="pt_BR", description="Idioma do template")
