from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator, model_validator


# ─── AutoReply ────────────────────────────────────
class AutoReplyUpdate(BaseModel):
    # CONV-EXPEDIENTE-01: 4096 é o teto do texto livre no WhatsApp (acima disso a Meta recusa o
    # envio). O caractere nulo é barrado porque o PostgreSQL não o aceita em coluna de texto —
    # em produção seria um 500; aqui vira 422 com mensagem.
    message: Optional[str] = Field(None, max_length=4096)
    is_active: Optional[bool] = None

    @field_validator("message")
    @classmethod
    def _sem_caractere_nulo(cls, message):
        if message is not None and "\x00" in message:
            raise ValueError("A mensagem nao pode conter o caractere nulo.")
        return message


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
    # Dígitos ASCII (`[0-9]`, não `\d`): o `\d` aceitaria dígitos de outros alfabetos (por exemplo
    # U+0669, o nove arábico-índico), que passariam pela validação e quebrariam a mesma comparação
    # de texto.
    open_time: Optional[str] = Field(None, pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    close_time: Optional[str] = Field(None, pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")

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
    # CONV-EXPEDIENTE-01: a semana tem 7 dias e `business_hours.weekday` é UNIQUE — um dia repetido
    # no envio estourava IntegrityError (500). Aqui vira 422 antes de tocar no banco.
    days: List[BusinessHoursDay] = Field(..., max_length=7)

    @field_validator("days")
    @classmethod
    def _sem_dia_repetido(cls, days):
        weekdays = [day.weekday for day in days]
        if len(weekdays) != len(set(weekdays)):
            raise ValueError("Dia da semana repetido no envio.")
        return days


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
