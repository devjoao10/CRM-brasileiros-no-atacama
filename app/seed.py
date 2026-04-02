"""Seed script to create initial admin user and run migrations."""
from sqlalchemy import text, inspect
from app.database import SessionLocal, engine, Base
from app.models.user import User, UserRole
from app.auth import hash_password


def _migrate_destino_to_destinos(db):
    """
    Migrate old single-string 'destino' column to JSON list 'destinos'.
    Handles the transition from String column to JSON list gracefully.
    """
    try:
        inspector = inspect(engine)
        columns = [c["name"] for c in inspector.get_columns("leads")]

        # Check if old 'destino' column exists (before migration)
        if "destino" in columns and "destinos" not in columns:
            print("🔄 Migrando coluna 'destino' → 'destinos' (lista)...")
            # Add new column
            db.execute(text("ALTER TABLE leads ADD COLUMN destinos JSON DEFAULT '[]'"))
            # Copy data: convert single string to JSON list
            rows = db.execute(text("SELECT id, destino FROM leads WHERE destino IS NOT NULL AND destino != ''")).fetchall()
            for row in rows:
                lead_id, old_destino = row
                # Convert single value to a JSON list
                import json
                destinos_list = json.dumps([old_destino.strip()])
                db.execute(text("UPDATE leads SET destinos = :destinos WHERE id = :id"),
                           {"destinos": destinos_list, "id": lead_id})
            db.commit()
            print(f"  ✅ {len(rows)} leads migrados com sucesso")

        elif "destino" in columns and "destinos" in columns:
            # Both columns exist — migrate any remaining data from old to new
            import json
            rows = db.execute(text(
                "SELECT id, destino FROM leads WHERE destino IS NOT NULL AND destino != '' "
                "AND (destinos IS NULL OR destinos = '[]' OR destinos = 'null')"
            )).fetchall()
            if rows:
                print(f"🔄 Migrando {len(rows)} leads restantes de 'destino' → 'destinos'...")
                for row in rows:
                    lead_id, old_destino = row
                    destinos_list = json.dumps([old_destino.strip()])
                    db.execute(text("UPDATE leads SET destinos = :destinos WHERE id = :id"),
                               {"destinos": destinos_list, "id": lead_id})
                db.commit()
                print("  ✅ Migração concluída")

    except Exception as e:
        print(f"⚠️  Aviso na migração de destinos: {e}")
        db.rollback()


def _migrate_email_verified(db):
    try:
        inspector = inspect(engine)
        columns = [c["name"] for c in inspector.get_columns("users")]
        if "email_verified" not in columns:
            print("🔄 Adicionando coluna 'email_verified' na tabela 'users'...")
            from sqlalchemy import text
            db.execute(text("ALTER TABLE users ADD COLUMN email_verified BOOLEAN DEFAULT 0"))
            db.commit()
            print("  ✅ Migração de 'email_verified' concluída")
    except Exception as e:
        print(f"⚠️  Aviso na migração email_verified: {e}")
        db.rollback()


def seed_database():
    """Create tables and seed initial data."""
    # Create all tables
    Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        # Run migrations
        _migrate_destino_to_destinos(db)
        _migrate_email_verified(db)

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
