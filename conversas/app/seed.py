"""
Seed script for local development.
Creates dev users, quick replies, templates, auto-replies, and business hours.

Controlled by CONVERSAS_SEED_DEV_DATA (default: true in dev, false in prod).
Dev user credentials come from CONVERSAS_DEV_EMAIL and CONVERSAS_DEV_PASSWORD — no hardcoded values.
"""

import hashlib
import logging
import os

from app.database import SessionLocal
from app.auth import User
from app.config import ENVIRONMENT

logger = logging.getLogger(__name__)

# ─── Seed control ─────────────────────────────────
CONVERSAS_SEED_DEV_DATA = os.getenv(
    "CONVERSAS_SEED_DEV_DATA", "true" if ENVIRONMENT == "development" else "false"
).lower() in ("true", "1", "yes")

CONVERSAS_DEV_EMAIL = os.getenv("CONVERSAS_DEV_EMAIL", "")
CONVERSAS_DEV_PASSWORD = os.getenv("CONVERSAS_DEV_PASSWORD", "")


def _hash_password(password: str) -> str:
    """Simple SHA-256 hash for dev."""
    return hashlib.sha256(password.encode()).hexdigest()


def seed_dev_user():
    """Create a dev user if CONVERSAS_SEED_DEV_DATA is enabled and user doesn't exist."""
    if not CONVERSAS_SEED_DEV_DATA:
        logger.info("[SEED] Conversas dev seed desativado (CONVERSAS_SEED_DEV_DATA=false)")
        return

    if not CONVERSAS_DEV_EMAIL or not CONVERSAS_DEV_PASSWORD:
        raise RuntimeError(
            "\n\n🔒 ERRO: CONVERSAS_SEED_DEV_DATA está ativado mas CONVERSAS_DEV_EMAIL "
            "e/ou CONVERSAS_DEV_PASSWORD não foram definidas!\n"
            "Defina ambas no .env ou desative com CONVERSAS_SEED_DEV_DATA=false.\n"
        )

    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == CONVERSAS_DEV_EMAIL).first()
        if not existing:
            user = User(
                nome="Admin Dev",
                email=CONVERSAS_DEV_EMAIL,
                hashed_password=_hash_password(CONVERSAS_DEV_PASSWORD),
                role="admin",
                is_active=True,
            )
            db.add(user)
            db.commit()
            logger.info(f"[SEED] Usuario dev criado: {CONVERSAS_DEV_EMAIL}")
        else:
            logger.info(f"[SEED] Usuario dev ja existe: {CONVERSAS_DEV_EMAIL}")
    finally:
        db.close()


# ─── Quick Replies ───────────────────────────
def seed_quick_replies():
    """Create default quick reply shortcuts."""
    from app.models.quick_reply import QuickReply

    db = SessionLocal()
    try:
        if db.query(QuickReply).count() > 0:
            logger.info("[SEED] Quick replies ja existem, pulando.")
            return

        replies = [
            {
                "shortcut": "/boasvindas",
                "title": "Boas-vindas ao Atacama",
                "content": "Olá! Seja bem-vindo(a) à Brasileiros no Atacama! 🏔️🌵\nComo posso te ajudar hoje?",
                "category": "Saudação",
            },
            {
                "shortcut": "/pagamento",
                "title": "Formas de pagamento",
                "content": "Aceitamos as seguintes formas de pagamento:\n💳 Cartão de crédito (até 6x)\n📱 PIX (desconto de 5%)\n💵 Transferência bancária\n\nQual forma prefere?",
                "category": "Pagamento",
            },
            {
                "shortcut": "/roteiro",
                "title": "Sugestão de roteiro 5 dias",
                "content": "Recomendamos o seguinte roteiro de 5 dias:\n🌙 Dia 1: Vale da Lua e Vale da Morte\n🏜️ Dia 2: Salar de Atacama + Lagunas\n🌋 Dia 3: Geysers del Tatio\n🏊 Dia 4: Termas de Puritama\n⭐ Dia 5: Tour astronômico\n\nPosso te enviar o orçamento?",
                "category": "Roteiro",
            },
            {
                "shortcut": "/horario",
                "title": "Horário de atendimento",
                "content": "Nosso atendimento funciona de segunda a sexta, das 13h às 19h (horário de Brasília).\nFora deste horário, deixe sua mensagem que retornaremos assim que possível! 😊",
                "category": "Informação",
            },
            {
                "shortcut": "/cancelar",
                "title": "Política de cancelamento",
                "content": "Nossa política de cancelamento:\n✅ Até 30 dias antes: reembolso total\n⚠️ 15-30 dias antes: reembolso de 50%\n❌ Menos de 15 dias: sem reembolso\n\nPosso ajudar com algo mais?",
                "category": "Informação",
            },
            {
                "shortcut": "/clima",
                "title": "Informações sobre clima",
                "content": "O clima no Atacama é desértico:\n🌡️ Dia: 20-25°C (verão) / 15-20°C (inverno)\n🌙 Noite: 0-5°C o ano todo\n☀️ Quase não chove\n\nRecomendamos levar:\n🧥 Casaco para a noite\n🧴 Protetor solar FPS 50+\n🕶️ Óculos de sol\n💧 Garrafa d'água",
                "category": "Informação",
            },
        ]

        for r in replies:
            db.add(QuickReply(**r))

        db.commit()
        logger.info(f"[SEED] {len(replies)} quick replies criados")
    finally:
        db.close()


# ─── Message Templates ───────────────────────
def seed_templates():
    """Create example message templates for development."""
    from app.models.template import MessageTemplate

    db = SessionLocal()
    try:
        if db.query(MessageTemplate).count() > 0:
            logger.info("[SEED] Templates ja existem, pulando.")
            return

        templates = [
            {
                "name": "boas_vindas",
                "category": "UTILITY",
                "body_text": "Olá {{1}}! Bem-vindo(a) à Brasileiros no Atacama. Como posso ajudá-lo(a) hoje?",
                "status": "PENDING",
                "sample_values_json": '{"body": ["João"]}',
            },
            {
                "name": "confirmacao_reserva",
                "category": "UTILITY",
                "body_text": "Olá {{1}}! Sua reserva #{{2}} para o dia {{3}} está confirmada.\n\nDetalhes:\nDestino: Atacama\nData: {{3}}\nPessoas: {{4}}\n\nQualquer dúvida, estamos à disposição!",
                "status": "PENDING",
                "sample_values_json": '{"body": ["Maria", "12345", "15/05/2026", "2"]}',
                "footer_text": "Brasileiros no Atacama",
            },
            {
                "name": "lembrete_passeio",
                "category": "UTILITY",
                "body_text": "Oi {{1}}! Lembrando que seu passeio {{2}} é amanhã!\n\nHorário: {{3}}\nPonto de encontro: {{4}}\n\nNão esqueça de levar protetor solar e água!",
                "status": "PENDING",
                "sample_values_json": '{"body": ["Carlos", "Vale da Lua", "14:00", "Hotel Atacama"]}',
            },
            {
                "name": "follow_up",
                "category": "MARKETING",
                "body_text": "Oi {{1}}! Notamos que você se interessou pelo Atacama.\n\nTemos uma promoção especial para o mês de {{2}}! Quer saber mais?",
                "status": "PENDING",
                "sample_values_json": '{"body": ["Ana", "julho"]}',
                "buttons_json": '[{"type": "QUICK_REPLY", "text": "Sim, quero saber!"}, {"type": "QUICK_REPLY", "text": "Não, obrigado"}]',
            },
        ]

        for t in templates:
            db.add(MessageTemplate(**t))

        db.commit()
        logger.info(f"[SEED] {len(templates)} templates criados")
    finally:
        db.close()


# ─── Auto Replies ────────────────────────────
def seed_auto_replies():
    """Create default auto-reply messages."""
    from app.models.auto_reply import AutoReply

    db = SessionLocal()
    try:
        if db.query(AutoReply).count() > 0:
            logger.info("[SEED] Auto replies ja existem, pulando.")
            return

        replies = [
            {"trigger": "greeting", "title": "Frase de apresentação", "message": "", "is_active": False},
            {
                "trigger": "start_service",
                "title": "Início de atendimento",
                "message": "Estamos iniciando o seu atendimento. Como podemos te ajudar hoje?",
            },
            {
                "trigger": "waiting",
                "title": "Aguardando atendimento",
                "message": "Recebemos sua mensagem! Em breve, um atendente estará com você. Obrigado pela paciência.",
            },
            {
                "trigger": "end_service",
                "title": "Término de atendimento",
                "message": "Seu atendimento foi finalizado. Caso precise de mais alguma coisa, estamos à disposição. Até breve!",
            },
            {
                "trigger": "out_of_hours",
                "title": "Fora do expediente",
                "message": "*Seja bem-vind@ à Brasileiros no Atacama!* 🌵\n\nNossa equipe comercial responde em horários específicos para garantir um atendimento ágil e de qualidade. No momento, estamos fora do horário, mas retornaremos sua mensagem o mais breve possível!",
            },
            {"trigger": "break_time", "title": "Intervalo de atendimento", "message": "", "is_active": False},
            {
                "trigger": "paused",
                "title": "Status em pausa",
                "message": "Estamos em uma breve pausa no momento, mas logo retornaremos para continuar seu atendimento. Obrigado pela compreensão!",
            },
        ]

        for r in replies:
            db.add(AutoReply(**r))

        db.commit()
        logger.info(f"[SEED] {len(replies)} auto replies criados")
    finally:
        db.close()


# ─── Business Hours ──────────────────────────
def seed_business_hours():
    """Create default business hours (Mon-Fri 13:00-19:00)."""
    from app.models.auto_reply import BusinessHours

    db = SessionLocal()
    try:
        if db.query(BusinessHours).count() > 0:
            logger.info("[SEED] Business hours ja existem, pulando.")
            return

        days = [
            {"weekday": 0, "is_open": True, "open_time": "13:00", "close_time": "19:00"},   # Segunda
            {"weekday": 1, "is_open": True, "open_time": "13:00", "close_time": "19:00"},   # Terça
            {"weekday": 2, "is_open": True, "open_time": "13:00", "close_time": "19:00"},   # Quarta
            {"weekday": 3, "is_open": True, "open_time": "13:00", "close_time": "19:00"},   # Quinta
            {"weekday": 4, "is_open": True, "open_time": "13:00", "close_time": "19:00"},   # Sexta
            {"weekday": 5, "is_open": False, "open_time": None, "close_time": None},         # Sábado
            {"weekday": 6, "is_open": False, "open_time": None, "close_time": None},         # Domingo
        ]

        for d in days:
            db.add(BusinessHours(**d))

        db.commit()
        logger.info("[SEED] Business hours criados (Seg-Sex 13:00-19:00)")
    finally:
        db.close()
