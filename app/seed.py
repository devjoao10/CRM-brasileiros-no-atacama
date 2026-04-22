"""Seed script to create initial admin user."""
from app.database import SessionLocal, engine, Base
from app.models.user import User, UserRole
from app.auth import hash_password
from app.config import ADMIN_INITIAL_PASSWORD


def seed_database():
    """Create tables and seed initial data."""
    # Create all tables
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        # Check if admin exists
        admin = db.query(User).filter(User.email == "admin@brasileirosnoatacama.com").first()
        if not admin:
            admin = User(
                nome="Administrador",
                email="admin@brasileirosnoatacama.com",
                hashed_password=hash_password(ADMIN_INITIAL_PASSWORD),
                role=UserRole.ADMIN,
                is_active=True,
                email_verified=True,
            )
            db.add(admin)
            db.commit()
            print("✅ Usuário admin criado: admin@brasileirosnoatacama.com (senha definida via ADMIN_INITIAL_PASSWORD)")
        else:
            print("ℹ️  Usuário admin já existe")
    finally:
        db.close()


if __name__ == "__main__":
    seed_database()
