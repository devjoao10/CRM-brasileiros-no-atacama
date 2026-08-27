from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SAEnum
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
import enum

from app.database import Base


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    USER = "user"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String(100), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    # AUDIT-2026-08-W2E (F7) — NAO ALTERAR SEM MIGRATION DE DADOS.
    # `SAEnum(UserRole)` persiste o NOME do membro ("ADMIN"/"USER"); todo leitor
    # Python enxerga o VALOR ("admin"/"user"). A divergencia e real e ja mordeu:
    # conversas/app/seed.py:54 grava a string minuscula "admin" na MESMA tabela
    # compartilhada, e essa linha faz a ORM do CRM levantar LookupError em toda
    # query que a retorne.
    #
    # A correcao "limpa" seria `values_callable=lambda e: [m.value for m in e]`,
    # e ela foi DELIBERADAMENTE NAO FEITA nesta auditoria porque muda o que fica
    # gravado e portanto exige reescrever linhas existentes:
    #   • PostgreSQL: a coluna e um enum NATIVO (`userrole`) cujos rotulos sao
    #     ADMIN/USER. Trocar exige ALTER TYPE ... RENAME VALUE (ou criar tipo
    #     novo + ALTER COLUMN ... USING), operacao multi-passo que reescreve a
    #     identidade do tipo sob a tabela viva.
    #   • Em QUALQUER dialeto, as linhas ja gravadas continuariam dizendo
    #     "ADMIN" e passariam a ser ilegiveis — o mesmo LookupError, so que
    #     invertido e agora atingindo TODOS os usuarios em vez de um.
    #   • Ou seja: os dois caminhos terminam num UPDATE de coluna de negocio,
    #     proibido pelas regras de migration deste repositorio.
    # Enquanto isso, tests/test_data_integrity_constraints.py trava a forma atual
    # ("ADMIN") para que ninguem a mude por acidente. Ver NOT_DONE do W2E.
    role = Column(SAEnum(UserRole), default=UserRole.USER, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    email_verified = Column(Boolean, default=False, nullable=False)
    api_key = Column(String(255), unique=True, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relacionamento M:N com Team
    teams = relationship("Team", secondary="user_teams", back_populates="users")

    def __repr__(self):
        return f"<User(id={self.id}, email='{self.email}', role='{self.role}')>"
