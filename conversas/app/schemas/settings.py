from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field, model_validator


# ─── AutoReply ────────────────────────────────────
class AutoReplyUpdate(BaseModel):
    message: Optional[str] = None
    is_active: Optional[bool] = None


class AutoReplyResponse(BaseModel):
    # CONV-EXPEDIENTE-01: gatilho do catálogo que ainda não tem linha no banco volta
    # com id/created_at/updated_at nulos (a linha só nasce no primeiro PUT).
    id: Optional[int] = None
    trigger: str
    title: str
    message: str
    is_active: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class AutoReplyListResponse(BaseModel):
    auto_replies: List[AutoReplyResponse]
    total: int


# ─── BusinessHours ────────────────────────────────
class BusinessHoursDay(BaseModel):
    weekday: int = Field(..., ge=0, le=6)
    is_open: bool
    # CONV-EXPEDIENTE-01: "HH:MM" com zero à esquerda. O webhook compara os horários como
    # texto (`open_time <= "HH:MM" <= close_time`), então "9:00" > "13:00" quebraria a conta.
    open_time: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    close_time: Optional[str] = Field(None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    @model_validator(mode="after")
    def _fechamento_depois_da_abertura(self):
        # Só vale para dia aberto com os dois horários; dia fechado guarda qualquer valor
        # (a tela manda os campos desabilitados) e dia aberto sem horário fica aberto o dia todo.
        if self.is_open and self.open_time and self.close_time and self.close_time <= self.open_time:
            raise ValueError(
                "Horario de fechamento deve ser depois da abertura "
                "(expediente que atravessa a meia-noite nao e suportado)"
            )
        return self


class BusinessHoursUpdate(BaseModel):
    days: List[BusinessHoursDay]


class BusinessHoursResponse(BaseModel):
    # CONV-EXPEDIENTE-01: dia sem linha no banco volta com id nulo (aberto, sem horário).
    id: Optional[int] = None
    weekday: int
    is_open: bool
    open_time: Optional[str] = None
    close_time: Optional[str] = None

    class Config:
        from_attributes = True


class BusinessHoursListResponse(BaseModel):
    hours: List[BusinessHoursResponse]
