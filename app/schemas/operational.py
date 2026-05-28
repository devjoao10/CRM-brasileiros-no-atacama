from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


# ==============================================================================
# --- Board & List Schemas ---
# ==============================================================================

class BoardCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=100, description="Nome do quadro")
    description: Optional[str] = Field(None, description="Descrição opcional do quadro")


class BoardUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=100)
    description: Optional[str] = None
    is_archived: Optional[bool] = None


class BoardResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    is_archived: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ListCreate(BaseModel):
    board_id: int = Field(..., description="ID do quadro associado")
    name: str = Field(..., min_length=1, max_length=100, description="Nome da lista/coluna")
    position: Optional[int] = Field(0, description="Posição de ordenação")


class ListUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    position: Optional[int] = None
    is_archived: Optional[bool] = None


class ListResponse(BaseModel):
    id: int
    board_id: int
    name: str
    position: int
    is_archived: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class BoardTemplateResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    lists_schema: Optional[List[str]]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ==============================================================================
# --- Card & Fields Schemas ---
# ==============================================================================

class CardCreate(BaseModel):
    board_id: int = Field(..., description="ID do quadro")
    list_id: int = Field(..., description="ID da lista/coluna inicial")
    title: str = Field(..., min_length=1, max_length=200, description="Título do card")
    description: Optional[str] = Field(None, description="Descrição detalhada")
    due_date: Optional[datetime] = Field(None, description="Data limite/vencimento")


class CardUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = None
    list_id: Optional[int] = None
    due_date: Optional[datetime] = None
    is_archived: Optional[bool] = None


class CardResponse(BaseModel):
    id: int
    board_id: int
    list_id: int
    title: str
    description: Optional[str]
    due_date: Optional[datetime]
    is_archived: bool
    created_by: Optional[int]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CardAssigneeResponse(BaseModel):
    id: int
    card_id: int
    user_id: int
    created_at: datetime

    class Config:
        from_attributes = True


class CardFieldDefinitionCreate(BaseModel):
    board_id: int = Field(..., description="ID do quadro")
    name: str = Field(..., min_length=1, max_length=100, description="Nome do campo")
    field_type: str = Field(..., description="Tipo do campo: short_text, long_text, date, select, boolean")
    select_options: Optional[List[str]] = Field(default=None, description="Opções válidas (apenas para tipo select)")
    is_required: Optional[bool] = Field(False, description="Se preenchimento é obrigatório")


class CardFieldDefinitionResponse(BaseModel):
    id: int
    board_id: int
    name: str
    field_type: str
    select_options: Optional[List[str]]
    is_required: bool
    is_archived: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CardFieldValueCreate(BaseModel):
    value_text: Optional[str] = Field(None, description="Valor textual (para short_text, long_text e select)")
    value_date: Optional[datetime] = Field(None, description="Valor de data (para date)")
    value_boolean: Optional[bool] = Field(None, description="Valor booleano (para boolean)")


class CardFieldValueResponse(BaseModel):
    id: int
    card_id: int
    definition_id: int
    value_text: Optional[str]
    value_date: Optional[datetime]
    value_boolean: Optional[bool]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ==============================================================================
# --- Checklist Schemas ---
# ==============================================================================

class ChecklistCreate(BaseModel):
    card_id: int = Field(..., description="ID do card")
    name: str = Field(..., min_length=1, max_length=100, description="Nome do checklist")
    position: Optional[int] = 0


class ChecklistResponse(BaseModel):
    id: int
    card_id: int
    name: str
    position: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ChecklistItemCreate(BaseModel):
    checklist_id: int = Field(..., description="ID do checklist")
    name: str = Field(..., min_length=1, max_length=200, description="Nome da tarefa")
    position: Optional[int] = 0


class ChecklistItemUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=200)
    is_checked: Optional[bool] = None
    position: Optional[int] = None


class ChecklistItemResponse(BaseModel):
    id: int
    checklist_id: int
    name: str
    is_checked: bool
    checked_by: Optional[int]
    checked_at: Optional[datetime]
    position: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ==============================================================================
# --- Comments, Mentions & Logs Schemas ---
# ==============================================================================

class CommentCreate(BaseModel):
    card_id: int = Field(..., description="ID do card")
    content: str = Field(..., min_length=1, description="Texto do comentário")


class CommentResponse(BaseModel):
    id: int
    card_id: int
    user_id: int
    content: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class NotificationResponse(BaseModel):
    id: int
    user_id: int
    card_id: Optional[int]
    event_type: str
    message: str
    read_at: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True


class CardMoveRequest(BaseModel):
    to_list_id: int = Field(..., description="ID da lista/coluna de destino")


class CardMovementResponse(BaseModel):
    id: int
    card_id: int
    user_id: int
    from_board_id: Optional[int]
    from_list_id: Optional[int]
    to_board_id: int
    to_list_id: int
    created_at: datetime

    class Config:
        from_attributes = True


class ActivityLogResponse(BaseModel):
    id: int
    card_id: int
    user_id: int
    action: str
    details: Optional[dict]
    created_at: datetime

    class Config:
        from_attributes = True
