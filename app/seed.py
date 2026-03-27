"""Seed script to create initial admin user."""
from app.database import SessionLocal, engine, Base
from app.models.user import User, UserRole
from app.auth import hash_password


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
                hashed_password=hash_password("admin123"),
                role=UserRole.ADMIN,
                is_active=True,
            )
            db.add(admin)
            db.commit()
            print("✅ Usuário admin criado: admin@brasileirosnoatacama.com / admin123")
        else:
            print("ℹ️  Usuário admin já existe")
    finally:
        db.close()


if __name__ == "__main__":
    seed_database()
