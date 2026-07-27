from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean
from sqlalchemy.sql import func

from app.database import Base


class MessageVariable(Base):
    """
    CONV-VAR-01 — Variavel dinamica usada em mensagens (@TOKEN).

    Sintaxe oficial do sistema: `@NOMEDAVARIAVEL` (decisao de produto — os
    vendedores ja usam `@`; NAO migrar para {{}}/${}/%%).

    Dois tipos de atribuicao:
      - kind='fixed'   -> o valor e o texto em `fixed_value`.
      - kind='dynamic' -> o valor vem de `source_key`, que DEVE ser uma chave
        do catalogo controlado em `app/services/variables.py`. O catalogo e
        um dicionario de funcoes Python fixas: NAO existe expressao, script
        ou codigo cadastrado pelo usuario (sem eval/exec — ver o modulo).
    """
    __tablename__ = "message_variables"

    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String(100), nullable=False)                 # "Primeiro Nome do Cliente"
    token       = Column(String(61), unique=True, nullable=False, index=True)  # "@PRIMEIRONOMECLIENTE"
    kind        = Column(String(10), nullable=False)                  # 'fixed' | 'dynamic'
    fixed_value = Column(Text, nullable=True)                         # so quando kind='fixed'
    source_key  = Column(String(60), nullable=True)                   # so quando kind='dynamic'
    is_active   = Column(Boolean, default=True, nullable=False)
    created_by  = Column(Integer, nullable=True)                      # users.id (sem FK: users e do CRM)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
    updated_at  = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self):
        return f"<MessageVariable(token='{self.token}', kind='{self.kind}')>"
