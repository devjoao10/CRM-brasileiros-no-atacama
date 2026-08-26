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
import os
from typing import Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.models.conversation import Conversation

logger = logging.getLogger(__name__)


def _only_digits(value: Optional[str]) -> str:
    """Normalizacao unica de telefone: so digitos (+, espacos, (), - e . somem)."""
    return "".join(ch for ch in (value or "") if ch.isdigit())


async def lookup_lead_by_whatsapp(whatsapp: str, db: Session) -> Optional[dict]:
    """
    Look up a lead in the CRM by WhatsApp number.
    Uses direct DB query on the shared 'leads' table.
    Returns the lead data dict or None if not found.

    AUDIT-2026-08-W2F (F10) — IDENTIDADE, nao "busca flexivel".
    O casamento antigo era `whatsapp LIKE '%<10 ultimos digitos>%'` e o primeiro
    resultado vencia. Em numeros brasileiros os 10 ultimos digitos NAO
    identificam ninguem: 5511987654321 e 5521987654321 terminam ambos em
    1987654321 — DDDs diferentes, clientes diferentes. Como
    `auto_link_conversation` grava o lead_id PERMANENTEMENTE na conversa e
    `variables.py` resolve @...CLIENTE a partir dele, um casamento errado manda
    nome/e-mail do cliente B para o cliente A dentro de um template. Nao e
    ruido de busca: e vazamento entre clientes. (`variables.py:226-229` ja
    documentava esta funcao como insegura para identidade e a evitava.)

    Agora: o LIKE sobrou apenas como PRE-FILTRO barato (aproveita o indice/scan
    e nao depende de regexp_replace, que existe no PostgreSQL mas nao no
    SQLite); a decisao e feita em Python por igualdade EXATA dos digitos. E,
    se mais de um lead casar exatamente, devolve NENHUM — ambiguidade e
    recusada, nunca resolvida por "ORDER BY created_at DESC LIMIT 1", que
    escolhia um cliente arbitrario.
    """
    normalized = _only_digits(whatsapp)
    if not normalized:
        return None
    # Use last 10 digits only to NARROW the candidate set (nunca para decidir)
    suffix = normalized[-10:] if len(normalized) >= 10 else normalized

    try:
        candidates = db.execute(
            text(
                "SELECT id, nome, whatsapp, email, responsavel_id "
                "FROM leads "
                "WHERE whatsapp LIKE :pattern "
                "ORDER BY created_at DESC "
                "LIMIT 50"
            ),
            {"pattern": f"%{suffix}%"},
        ).fetchall()

        exact = [r for r in candidates if _only_digits(r.whatsapp) == normalized]
        if not exact:
            return None
        if len({r.id for r in exact}) > 1:
            logger.warning(
                "Lookup de lead AMBIGUO para %s: %s leads com o mesmo numero "
                "(%s) — nenhum vinculo automatico sera feito.",
                normalized, len(exact), sorted({r.id for r in exact}),
            )
            return None
        result = exact[0]

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

    AUDIT-2026-08-W2F (F8) — NAO COMMITA MAIS.
    Antes: o router commitava a conversa e SO DEPOIS chamava esta funcao, que
    commitava (ou fazia rollback e devolvia False) por conta propria — com o
    resultado descartado. Como `_repair_responsavel_cache` reescreve o cache da
    conversa A PARTIR DO CRM a cada listagem (5s) e a cada abertura, uma
    atribuicao cujo UPDATE no lead falhasse aparecia como sucesso e REVERTIA
    sozinha no refresh seguinte.

    Conversas e CRM compartilham a MESMA base e a MESMA session factory, entao
    a correcao certa nao e "avisar depois": e uma transacao unica. Esta funcao
    passa a apenas EMITIR os comandos e devolver True/False; quem commita (ou
    aborta tudo, conversa inclusive) e o caller. O `rollback()` do except
    continua, porque uma transacao abortada precisa ser limpa antes de qualquer
    outra query na mesma sessao.
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

        # Sem commit: o caller fecha a transacao (ver docstring, F8).
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

        # 2. Funil default — MESMA precedencia de
        #    app/services/lead_creation.py:resolver_funil_padrao (CRM, modulo
        #    irmao que nao da para importar daqui: processo e app FastAPI
        #    separados). AUDIT-2026-08-WB (W2-10): a query antiga preferia
        #    QUALQUER funil ativo com "whatsapp" no nome
        #    (`ORDER BY (LOWER(nome) LIKE '%whatsapp%') DESC, id ASC`) e
        #    mandava o lead para "Vendas WhatsApp" em vez do funil principal.
        #    Precedencia agora: DEFAULT_FUNNEL_ID (env), se apontar para um
        #    funil ativo; senao o funil ATIVO de MENOR id — nunca por nome.
        #
        #    Le a env DIRETO com os.getenv, nao via conversas/app/config.py:
        #    este modulo e do Conversas, que tem o proprio config.py, e a
        #    tarefa que gerou esta correcao nao e dona dele para acrescentar a
        #    variavel la. os.getenv aqui e o mesmo valor, sem precisar editar
        #    um arquivo fora do escopo.
        funnel_row = None
        default_funnel_id_raw = os.getenv("DEFAULT_FUNNEL_ID", "").strip()
        if default_funnel_id_raw.isdigit():
            funnel_row = db.execute(
                text(
                    "SELECT id, etapas FROM funnels "
                    "WHERE id = :fid AND is_active = true "
                    "LIMIT 1"
                ),
                {"fid": int(default_funnel_id_raw)},
            ).fetchone()

        if not funnel_row:
            funnel_row = db.execute(
                text(
                    "SELECT id, etapas FROM funnels "
                    "WHERE is_active = true "
                    "ORDER BY id ASC "
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
            #
            # AUDIT-2026-08-W2F (F9): `dados` E OBRIGATORIO. Sem a coluna aqui a
            # linha nascia com SQL NULL, mas o schema de resposta do CRM declara
            # `dados: dict = {}` (app/schemas/pipeline.py:129) e
            # app/routers/pipeline.py:765 valida CADA linha —
            # GET /api/pipeline/history/{lead_id} entao estourava ValidationError
            # e devolvia 500 para TODO lead criado automaticamente pelo WhatsApp,
            # justamente os que mais precisam da timeline. O outro INSERT deste
            # modulo (sync_responsavel_to_crm) e o writer do proprio CRM sempre
            # mandaram `dados`; so este nao mandava. '{}' = mesmo default do schema.
            db.execute(
                text(
                    "INSERT INTO lead_history "
                    "(lead_id, evento, descricao, dados, created_at) "
                    "VALUES (:lid, 'created', :desc, :dados, NOW())"
                ),
                {
                    "lid": lead_id,
                    "desc": f"Lead criado automaticamente via WhatsApp. "
                            f"Adicionado ao funil (etapa: {first_stage_id})",
                    "dados": "{}",
                },
            )
        else:
            # AUDIT-2026-08-WB: antes este bloco era pulado em SILENCIO — o
            # lead commitava sem FunnelEntry e sem ninguem saber por que
            # (outra rota para "zero FunnelEntry" documentada no F-341).
            logger.warning(
                "Lead #%s criado sem funil: nenhum funil ativo encontrado.",
                lead_id,
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


def get_leads_responsaveis(lead_ids: list, db: Session) -> Optional[dict]:
    """
    CONV-HOTFIX-POSTDEPLOY-01: busca em LOTE o responsavel dos leads no CRM
    (fonte de verdade para conversas vinculadas). UMA query parametrizada
    leads LEFT JOIN users (so usuarios ativos ganham nome).

    Retorna {lead_id: {"responsavel_id": int|None, "responsavel_nome": str|None}}
    ou None se o CRM estiver inacessivel (dev isolado sem tabelas CRM) —
    o caller segue com o cache local, como o tag sync faz.
    """
    if not lead_ids:
        return {}
    try:
        stmt = text(
            "SELECT l.id AS lead_id, l.responsavel_id AS responsavel_id, u.nome AS nome "
            "FROM leads l "
            "LEFT JOIN users u ON u.id = l.responsavel_id AND u.is_active "
            "WHERE l.id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        rows = db.execute(stmt, {"ids": list(lead_ids)}).fetchall()
        return {
            r.lead_id: {"responsavel_id": r.responsavel_id, "responsavel_nome": r.nome}
            for r in rows
        }
    except Exception as e:
        db.rollback()  # limpa a transacao abortada (dev sem tabelas CRM)
        # AUDIT-2026-08-W2F-orq: este except era MUDO. Quando dispara, a lista de
        # conversas perde o responsavel de TODAS elas de uma vez, sem erro na tela
        # e sem uma linha de log — indistinguivel de "ninguem foi atribuido".
        logger.warning(
            "Lookup em lote de responsavel no CRM falhou (%s: %s) — %s conversa(s) "
            "ficam sem responsavel nesta chamada.",
            type(e).__name__, e, len(lead_ids) if lead_ids else 0,
        )
        return None


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
    except Exception as e:
        db.rollback()  # limpa a transacao abortada (dev sem tabelas CRM)
        # AUDIT-2026-08-W2F-orq: este except era MUDO. Quando dispara, a conversa
        # deixa de espelhar as tags do lead — sem erro e sem log. Um espelho que
        # para de espelhar em silencio e indistinguivel de um lead sem tags.
        logger.warning(
            "Leitura das tags do lead %s no CRM falhou (%s: %s) — a conversa NAO "
            "sera espelhada nesta chamada.", lead_id, type(e).__name__, e,
        )
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
        logger.warning(
            "Tag sync Conversas->CRM falhou (lead %s, tag %r) — %s: %s. A tag ficou "
            "so no Conversas e o CRM segue sem ela.",
            lead_id, nome, type(e).__name__, e,
        )
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
        logger.warning(
            "Tag unsync Conversas->CRM falhou (lead %s, tag %r) — %s: %s. A tag "
            "continua no CRM depois de removida no Conversas.",
            lead_id, nome, type(e).__name__, e,
        )
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
