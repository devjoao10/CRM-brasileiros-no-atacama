"""Seed script to create initial admin user.

Controlled by SEED_INITIAL_ADMIN (default: true in dev, false in prod).
Credentials come from ADMIN_INITIAL_EMAIL and ADMIN_INITIAL_PASSWORD — no hardcoded values.
"""
import logging

from app.database import SessionLocal
from app.models.user import User, UserRole
from app.auth import hash_password
from app.config import SEED_INITIAL_ADMIN, ADMIN_INITIAL_EMAIL, ADMIN_INITIAL_PASSWORD

logger = logging.getLogger(__name__)


def seed_database():
    """Create initial admin user if SEED_INITIAL_ADMIN is enabled and user doesn't exist."""

    if not SEED_INITIAL_ADMIN:
        logger.info("[SEED] Admin seed desativado (SEED_INITIAL_ADMIN=false)")
        return

    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.email == ADMIN_INITIAL_EMAIL).first()
        if not admin:
            admin = User(
                nome="Administrador",
                email=ADMIN_INITIAL_EMAIL,
                hashed_password=hash_password(ADMIN_INITIAL_PASSWORD),
                role=UserRole.ADMIN,
                is_active=True,
                email_verified=True,
            )
            db.add(admin)
            db.commit()
            logger.info(f"[SEED] Admin criado: {ADMIN_INITIAL_EMAIL}")
        else:
            logger.info(f"[SEED] Admin ja existe: {ADMIN_INITIAL_EMAIL}")
    finally:
        db.close()


if __name__ == "__main__":
    seed_database()
