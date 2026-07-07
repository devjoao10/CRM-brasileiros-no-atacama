"""
CRM Integration Service.
Uses DIRECT DATABASE QUERIES (shared PostgreSQL) to:
- Auto-link leads by WhatsApp number
- Sync lead ownership (responsavel)
- Get pipeline info for navigation

Both CRM and Conversas share the same PostgreSQL database in production,
so we query tables directly instead of making HTTP calls. This avoids
authentication overhead and is more reliable.
"""

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.conversation import Conversation

logger = logging.getLogger(__name__)


async def lookup_lead_by_whatsapp(whatsapp: str, db: Session) -> Optional[dict]:
    """
    Look up a lead in the CRM by WhatsApp number.
    Uses direct DB query on the shared 'leads' table.
    Returns the lead data dict or None if not found.
    """
    # Normalize: remove +, spaces, dashes
    normalized = whatsapp.replace("+", "").replace(" ", "").replace("-", "").strip()
    # Use last 10 digits for flexible matching
    suffix = normalized[-10:] if len(normalized) >= 10 else normalized

    try:
        result = db.execute(
            text(
                "SELECT id, nome, whatsapp, email, responsavel_id "
                "FROM leads "
                "WHERE whatsapp LIKE :pattern "
                "ORDER BY created_at DESC "
                "LIMIT 1"
            ),
            {"pattern": f"%{suffix}%"},
        ).fetchone()

        if not result:
            return None

        # Get responsavel name
        responsavel_nome = "Agente IA"
        if result.responsavel_id:
            user_row = db.execute(
                text("SELECT nome FROM users WHERE id = :uid AND is_active = true"),
                {"uid": result.responsavel_id},
            ).fetchone()
            if user_row:
                responsavel_nome = user_row.nome

        return {
            "id": result.id,
            "nome": result.nome,
            "whatsapp": result.whatsapp,
            "email": result.email,
            "responsavel_id": result.responsavel_id,
            "responsavel_nome": responsavel_nome,
        }
    except Exception as e:
        logger.error(f"Erro ao buscar lead no banco: {e}")
        return None


async def get_lead_pipeline_info(lead_id: int, db: Session) -> Optional[dict]:
    """
    Get pipeline/funnel info for a lead.
    Uses direct DB query on the shared 'funnel_entries' and 'funnels' tables.
    Returns dict with funnel_id, etapa_id, funnel_nome, etapa_nome.
    """
    try:
        result = db.execute(
            text(
                "SELECT fe.id AS entry_id, fe.funnel_id, fe.etapa_id, f.nome AS funnel_nome, f.etapas "
                "FROM funnel_entries fe "
                "JOIN funnels f ON f.id = fe.funnel_id "
                "WHERE fe.lead_id = :lead_id "
                "ORDER BY fe.created_at DESC "
                "LIMIT 1"
            ),
            {"lead_id": lead_id},
        ).fetchone()

        if not result:
            return None

        # Try to find the stage name from the funnel's etapas JSON
        etapa_nome = result.etapa_id
        if result.etapas:
            import json
            etapas = result.etapas if isinstance(result.etapas, list) else json.loads(result.etapas)
            for stage in etapas:
                if isinstance(stage, dict) and stage.get("id") == result.etapa_id:
                    etapa_nome = stage.get("nome", result.etapa_id)
                    break

        return {
            "entry_id": result.entry_id,
            "funnel_id": result.funnel_id,
            "funnel_nome": result.funnel_nome,
            "etapa_id": result.etapa_id,
            "etapa_nome": etapa_nome,
        }
    except Exception as e:
        logger.error(f"Erro ao buscar pipeline info: {e}")
        return None


async def sync_responsavel_to_crm(
    lead_id: int, responsavel_id: Optional[int], db: Session
) -> bool:
    """
    Sync the responsavel_id change to the CRM leads table.
    Uses direct DB update on the shared 'leads' table.
    Also logs the change in lead_history.
    Returns True on success.
    """
    if lead_id <= 0:
        return False

    try:
        # Get the current responsavel for history logging
        current = db.execute(
            text("SELECT responsavel_id FROM leads WHERE id = :lid"),
            {"lid": lead_id},
        ).fetchone()

        if not current:
            logger.warning(f"Lead {lead_id} não encontrado para sync de responsável")
            return False

        old_resp = current.responsavel_id

        # Update the lead
        db.execute(
            text("UPDATE leads SET responsavel_id = :resp_id WHERE id = :lid"),
            {"resp_id": responsavel_id, "lid": lead_id},
        )

        # Log in lead_history if changed
        if old_resp != responsavel_id:
            old_name = "Agente IA" if old_resp is None else str(old_resp)
            new_name = "Agente IA" if responsavel_id is None else str(responsavel_id)
            import json
            db.execute(
                text(
                    "INSERT INTO lead_history (lead_id, evento, descricao, dados, created_at) "
                    "VALUES (:lid, 'responsavel_changed', :desc, :dados, NOW())"
                ),
                {
                    "lid": lead_id,
                    "desc": f"Responsável alterado de '{old_name}' para '{new_name}' (via Conversas)",
                    "dados": json.dumps({
                        "old_responsavel_id": old_resp,
                        "new_responsavel_id": responsavel_id,
                        "source": "conversas",
                    }),
                },
            )

        db.commit()
        logger.info(f"Responsável sync'd: lead={lead_id}, responsavel={responsavel_id}")
        return True
    except Exception as e:
        logger.error(f"Erro ao sincronizar responsável: {e}")
        db.rollback()
        return False


async def auto_create_lead_in_crm(
    whatsapp: str, nome: str, db: Session
) -> Optional[dict]:
    """
    Create a new lead in the CRM, add to the first active funnel
    (stage 'nova_oportunidade'), and apply the 'WhatsApp' tag.
    Returns the created lead data dict, or None on failure.
    """
    try:
        # 1. Create the lead
        result = db.execute(
            text(
                "INSERT INTO leads (nome, whatsapp, status_venda, is_active, "
                "campos_personalizados, created_at, updated_at) "
                "VALUES (:nome, :whatsapp, 'em_negociacao', true, "
                "'{\"origem\": \"WhatsApp\"}'::jsonb, NOW(), NOW()) "
                "RETURNING id"
            ),
            {"nome": nome, "whatsapp": whatsapp},
        )
        lead_id = result.fetchone()[0]
        logger.info(f"Lead criado automaticamente: #{lead_id} — {nome} ({whatsapp})")

        # 2. Find the first active funnel with 'whatsapp' in the name (case-insensitive)
        #    Falls back to ANY first active funnel if none matches
        funnel_row = db.execute(
            text(
                "SELECT id, etapas FROM funnels "
                "WHERE is_active = true "
                "ORDER BY (LOWER(nome) LIKE '%whatsapp%') DESC, id ASC "
                "LIMIT 1"
            )
        ).fetchone()

        if funnel_row:
            funnel_id = funnel_row.id
            etapas = funnel_row.etapas

            # Determine the first stage ID
            import json
            stages = etapas if isinstance(etapas, list) else json.loads(etapas)
            first_stage_id = "nova_oportunidade"  # default
            if stages and isinstance(stages[0], dict):
                first_stage_id = stages[0].get("id", "nova_oportunidade")

            # Check if lead is already in this funnel (avoid duplicates)
            existing_entry = db.execute(
                text(
                    "SELECT id FROM funnel_entries "
                    "WHERE lead_id = :lid AND funnel_id = :fid "
                    "LIMIT 1"
                ),
                {"lid": lead_id, "fid": funnel_id},
            ).fetchone()

            if not existing_entry:
                db.execute(
                    text(
                        "INSERT INTO funnel_entries "
                        "(lead_id, funnel_id, etapa_id, posicao, created_at, updated_at) "
                        "VALUES (:lid, :fid, :etapa, 0, NOW(), NOW())"
                    ),
                    {"lid": lead_id, "fid": funnel_id, "etapa": first_stage_id},
                )
                logger.info(
                    f"Lead #{lead_id} adicionado ao funil #{funnel_id} "
                    f"(etapa: {first_stage_id})"
                )

            # Log in lead_history
            db.execute(
                text(
                    "INSERT INTO lead_history "
                    "(lead_id, evento, descricao, created_at) "
                    "VALUES (:lid, 'created', :desc, NOW())"
                ),
                {
                    "lid": lead_id,
                    "desc": f"Lead criado automaticamente via WhatsApp. "
                            f"Adicionado ao funil (etapa: {first_stage_id})",
                },
            )

        # 3. Apply 'WhatsApp' tag (create if it doesn't exist)
        tag_row = db.execute(
            text("SELECT id FROM tags WHERE LOWER(nome) = 'whatsapp' LIMIT 1")
        ).fetchone()

        if tag_row:
            tag_id = tag_row.id
        else:
            tag_result = db.execute(
                text(
                    "INSERT INTO tags (nome, cor, created_at) "
                    "VALUES ('WhatsApp', '#25D366', NOW()) "
                    "RETURNING id"
                )
            )
            tag_id = tag_result.fetchone()[0]
            logger.info(f"Tag 'WhatsApp' criada: #{tag_id}")

        # Link tag to lead (avoid duplicate)
        existing_tag_link = db.execute(
            text(
                "SELECT 1 FROM lead_tags "
                "WHERE lead_id = :lid AND tag_id = :tid"
            ),
            {"lid": lead_id, "tid": tag_id},
        ).fetchone()

        if not existing_tag_link:
            db.execute(
                text(
                    "INSERT INTO lead_tags (lead_id, tag_id) "
                    "VALUES (:lid, :tid)"
                ),
                {"lid": lead_id, "tid": tag_id},
            )

        db.commit()

        return {
            "id": lead_id,
            "nome": nome,
            "whatsapp": whatsapp,
            "responsavel_id": None,
            "responsavel_nome": "Agente IA",
        }

    except Exception as e:
        logger.error(f"Erro ao criar lead automático: {e}", exc_info=True)
        db.rollback()
        return None


async def auto_link_conversation(conversation: Conversation, db: Session) -> bool:
    """
    Automatically link a conversation to a CRM lead by WhatsApp number.
    If no lead exists, creates one automatically in the CRM.
    Updates conversation.lead_id, responsavel_id, and responsavel_nome.
    Returns True if linked.
    """
    if not conversation.whatsapp:
        return False

    lead_data = await lookup_lead_by_whatsapp(conversation.whatsapp, db)

    # Lead not found — create automatically
    if not lead_data:
        nome = conversation.nome or conversation.whatsapp
        lead_data = await auto_create_lead_in_crm(conversation.whatsapp, nome, db)
        if not lead_data:
            logger.warning(
                f"Falha ao criar lead automático para {conversation.whatsapp}"
            )
            return False

    conversation.lead_id = lead_data.get("id", 0)
    conversation.responsavel_id = lead_data.get("responsavel_id")
    conversation.responsavel_nome = lead_data.get("responsavel_nome", "Agente IA")

    # Update name from CRM if not set
    if not conversation.nome or conversation.nome == conversation.whatsapp:
        conversation.nome = lead_data.get("nome", conversation.nome)

    db.commit()
    logger.info(
        f"Conversa {conversation.id} vinculada ao lead CRM #{lead_data['id']} "
        f"({lead_data.get('nome', '?')})"
    )
    return True


async def get_users_list(db: Session) -> list:
    """
    Get list of active users from the shared users table (for responsavel selection).
    Since both systems share the same DB, we can query directly.
    """
    from app.auth import User
    users = db.query(User).filter(User.is_active == True).order_by(User.nome).all()
    return [
        {
            "id": u.id,
            "nome": u.nome,
            "email": u.email,
            "role": u.role,
        }
        for u in users
    ]


# ─── CONV-TAGS-SYNC-01: tags do lead (CRM) <-> Conversas ────────────────────
# Mesmo padrao SQL-direto do restante deste modulo (base compartilhada).
# Todas as funcoes toleram a AUSENCIA das tabelas do CRM (dev isolado):
# try/except + rollback -> retornam None/False e o Conversas segue 100% local.

import re as _re

_HEX6 = _re.compile(r"^#[0-9A-Fa-f]{6}$")
_DEFAULT_TAG_COLOR = "#3B82F6"


def _safe_color(cor) -> str:
    """Cor vinda do CRM vai para atributo style no frontend — sanitizar SEMPRE."""
    return cor if isinstance(cor, str) and _HEX6.match(cor) else _DEFAULT_TAG_COLOR


def get_lead_tags(lead_id: int, db: Session):
    """Le as tags do lead no CRM. Retorna [{'nome','cor'}] ou None se CRM inacessivel."""
    try:
        rows = db.execute(
            text(
                "SELECT t.nome AS nome, t.cor AS cor "
                "FROM tags t JOIN lead_tags lt ON lt.tag_id = t.id "
                "WHERE lt.lead_id = :lid"
            ),
            {"lid": lead_id},
        ).fetchall()
        return [{"nome": r.nome, "cor": _safe_color(r.cor)} for r in rows]
    except Exception:
        db.rollback()  # limpa a transacao abortada (dev sem tabelas CRM)
        return None


def add_tag_to_lead(lead_id: int, nome: str, cor: str, db: Session) -> bool:
    """Aplica a tag ao lead no CRM (cria a tag por NOME se nao existir). Idempotente."""
    try:
        row = db.execute(text("SELECT id FROM tags WHERE nome = :n"), {"n": nome}).fetchone()
        if row:
            tag_id = row.id
        else:
            db.execute(
                text("INSERT INTO tags (nome, cor, created_at) VALUES (:n, :c, CURRENT_TIMESTAMP)"),
                {"n": nome, "c": _safe_color(cor)},
            )
            tag_id = db.execute(text("SELECT id FROM tags WHERE nome = :n"), {"n": nome}).fetchone().id
        link = db.execute(
            text("SELECT 1 FROM lead_tags WHERE lead_id = :lid AND tag_id = :tid"),
            {"lid": lead_id, "tid": tag_id},
        ).fetchone()
        if not link:
            db.execute(
                text("INSERT INTO lead_tags (lead_id, tag_id) VALUES (:lid, :tid)"),
                {"lid": lead_id, "tid": tag_id},
            )
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.warning(f"Tag sync Conversas->CRM indisponivel (lead {lead_id}): {type(e).__name__}")
        return False


def remove_tag_from_lead(lead_id: int, nome: str, db: Session) -> bool:
    """Remove o vinculo lead<->tag no CRM (por NOME da tag). Idempotente."""
    try:
        db.execute(
            text(
                "DELETE FROM lead_tags WHERE lead_id = :lid "
                "AND tag_id IN (SELECT id FROM tags WHERE nome = :n)"
            ),
            {"lid": lead_id, "n": nome},
        )
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.warning(f"Tag unsync Conversas->CRM indisponivel (lead {lead_id}): {type(e).__name__}")
        return False


def sync_lead_tags_to_conversation(conversation: Conversation, db: Session) -> bool:
    """
    Espelho CRM -> Conversas (read-repair, chamado ao ABRIR a conversa).

    Para conversa VINCULADA (lead_id > 0) o CRM e a fonte de verdade: o
    conjunto de tags da conversa vira EXATAMENTE o conjunto de tags do lead
    (tag removida no CRM some aqui; aplicada la aparece aqui). Tags locais
    sao criadas por NOME (cor copiada e sanitizada). Se o CRM estiver
    inacessivel (dev isolado), NAO toca nas tags locais.
    """
    if not conversation.lead_id or conversation.lead_id <= 0:
        return False
    crm_tags = get_lead_tags(conversation.lead_id, db)
    if crm_tags is None:
        return False  # CRM inacessivel: preserva estado local

    from app.models.tag import ConversationTag

    desired = []
    for item in crm_tags:
        tag = db.query(ConversationTag).filter(ConversationTag.nome == item["nome"]).first()
        if not tag:
            tag = ConversationTag(nome=item["nome"], cor=item["cor"])
            db.add(tag)
            db.flush()
        desired.append(tag)

    if {t.id for t in conversation.tags} != {t.id for t in desired}:
        conversation.tags = desired
        db.commit()
        logger.info(f"Tags da conversa {conversation.id} espelhadas do lead {conversation.lead_id}")
    return True
