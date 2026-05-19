"""
Cria um usuário admin de teste no banco local (SQLite).
Rodar: python scripts/create_test_user.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, engine, Base
# Importar todos os models para resolver relacionamentos
import app.models.user
import app.models.team
import app.models.lead
import app.models.tag
import app.models.pipeline
import app.models.task
import app.models.segment
import app.models.chat
from app.models.user import User
from app.auth import hash_password

# Garante que as tabelas existem
Base.metadata.create_all(bind=engine)

db = SessionLocal()

EMAIL = "admin@teste.com"
SENHA = "admin123"
NOME  = "Admin Teste"

existing = db.query(User).filter(User.email == EMAIL).first()
if existing:
    print(f"Usuário já existe: {EMAIL} / {SENHA}")
else:
    user = User(
        email=EMAIL,
        nome=NOME,
        hashed_password=hash_password(SENHA),
        role="admin",
        is_active=True,
    )
    db.add(user)
    db.commit()
    print(f"Usuário criado com sucesso!")

print(f"\n  Email: {EMAIL}")
print(f"  Senha: {SENHA}")
print(f"  URL:   http://127.0.0.1:8001/login")

db.close()
