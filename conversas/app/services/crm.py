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
import unicodedata
import os
from typing import Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.models.conversation import Conversation

logger = logging.getLogger(__name__)


def _only_digits(value: Optional[str]) -> str:
    """Normalizacao unica de telefone: so digitos (+, espacos, (), - e . somem)."""
    return "".join(ch for ch in (value or "") if ch.isdigit())


async def lookup_lead_by_whatsapp(whatsapp: str, db: Session) -> tuple[Optional[dict], bool]:
    """
    Look up a lead in the CRM by WhatsApp number.
    Uses direct DB query on the shared 'leads' table.

    Devolve `(lead, bloquear_criacao)`:
      - `(dict, False)`  exatamente UM lead tem este numero;
      - `(None, False)`  NENHUM lead tem este numero — o caller pode criar;
      - `(None, True)`   nao da para afirmar que o numero e novo (dois ou mais
                         leads com ele, ou a consulta falhou). O caller NAO
                         pode vincular nem criar: criar aqui e fabricar mais um
                         duplicado em cima do problema.

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

    AUDIT-2026-08-WF2 — o PRE-FILTRO passou a normalizar OS DOIS LADOS no SQL.
    A decisao ja era por igualdade exata dos digitos, mas o conjunto de
    candidatos vinha de um LIKE sobre a coluna CRUA: lead gravado com
    formatacao (`+55 11 98765-4322`, que e exatamente o que o formulario do
    site grava — o proprio `_only_digits` existe porque a coluna guarda `+`,
    espaco, `()`, `-` e `.`) NUNCA entrava na lista. Nem o casamento exato nem
    o guard de ambiguidade chegavam a ve-lo: o lookup devolvia None e
    `auto_create_lead_in_crm` criava o lead DE NOVO, com a conversa presa ao
    duplicado e o lead real — com e-mail, destinos e responsavel — orfao.
    Medido em PostgreSQL 16 com 19.004 leads: o LIKE devolvia `[]`.

    `regexp_replace(whatsapp, '[^0-9]', '', 'g')` e a mesma normalizacao de
    `_only_digits`, agora do lado do banco, entao o pre-filtro devolve TODOS os
    leads com aquele numero, em qualquer formato — que e o que o guard de
    ambiguidade precisa enxergar para funcionar. Custo: continua um Seq Scan
    (2,7 ms -> 11,5 ms nos mesmos 19k leads; o LIKE com `%` a esquerda tambem
    nunca usou `ix_leads_whatsapp`). Com um indice de expressao sobre a mesma
    normalizacao vira Index Scan de 0,1 ms — criar esse indice e migration
    (`migrations/mNNN_*.py`), fora do escopo deste arquivo.

    A igualdade em Python continua depois do SQL de proposito: `[^0-9]` do
    PostgreSQL e ASCII e `str.isdigit()` nao e, entao quem decide identidade e
    sempre o Python. Ambiguidade continua RECUSADA, nunca resolvida por
    "ORDER BY created_at DESC LIMIT 1", que escolhia um cliente arbitrario.

    SO-PostgreSQL, como o resto do modulo (`NOW()`, `::jsonb`, `RETURNING`):
    em SQLite `regexp_replace` nao existe e a consulta cai no `except` — que
    agora bloqueia a criacao em vez de deixar passar como "numero novo".
    """
    normalized = _only_digits(whatsapp)
    if not normalized:
        return None, False

    try:
        candidates = db.execute(
            text(
                "SELECT id, nome, whatsapp, email, responsavel_id "
                "FROM leads "
                "WHERE regexp_replace(whatsapp, '[^0-9]', '', 'g') = :digitos "
                "LIMIT 50"
            ),
            {"digitos": normalized},
        ).fetchall()

        exact = [r for r in candidates if _only_digits(r.whatsapp) == normalized]
        if not exact:
            return None, False
        if len({r.id for r in exact}) > 1:
            logger.warning(
                "Lookup de lead AMBIGUO para %s: %s leads com o mesmo numero "
                "(%s) — nenhum vinculo automatico sera feito.",
                normalized, len(exact), sorted({r.id for r in exact}),
            )
            return None, True
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
        }, False
    except Exception as e:
        # AUDIT-2026-08-WF2 — falha AQUI nao e "numero novo". Este except cobre
        # tambem a leitura do responsavel, que roda DEPOIS de o lead ter sido
        # encontrado: devolver "pode criar" transformava um erro de consulta em
        # duplicata do lead que a consulta tinha acabado de achar.
        logger.error(f"Erro ao buscar lead no banco: {e}")
        return None, True


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
        #    separados, e os dois pacotes se chamam `app`).
        #
        #    AUDIT-2026-08-WB (W2-10): a query original preferia QUALQUER funil
        #    ativo com "whatsapp" no nome
        #    (`ORDER BY (LOWER(nome) LIKE '%whatsapp%') DESC, id ASC`) e mandava
        #    o lead para "Vendas WhatsApp" em vez do funil principal.
        #
        #    AUDIT-2026-08-WF2: a correcao daquela vez trocou por "funil ATIVO
        #    de MENOR id", que era so um acidente de historico com outro nome —
        #    o funil certo vencia porque tinha sido criado primeiro. Agora a
        #    resolucao e por NOME, que e UNIQUE em `funnels` e e o mesmo
        #    contrato que o system message do Gerenciador declara.
        #
        #    Precedencia: DEFAULT_FUNNEL_ID (env) se apontar para funil ATIVO —
        #    e, se estiver configurado e NAO apontar, FALHA em vez de cair em
        #    outro; senao DEFAULT_FUNNEL_NOME por igualdade normalizada. Sem
        #    fallback por ordem de id.
        #
        #    A normalizacao roda em Python, nao em SQL: `funnels` tem dezenas de
        #    linhas, e replicar `lower(btrim(replace(...)))` em dois dialetos
        #    seria mais uma copia para divergir.
        #
        #    Le as envs DIRETO com os.getenv, nao via conversas/app/config.py:
        #    este modulo e do Conversas, que tem o proprio config, e as tres
        #    variaveis sao do dominio do CRM (app/config.py). O valor e o mesmo.
        def _norm(valor):
            # NFC como no CRM: mesmo nome visivel, bytes diferentes, sem isto
            # nao casa. Ver app/services/lead_creation.py:_normalizar.
            texto = unicodedata.normalize("NFC", str(valor or ""))
            return " ".join(texto.replace("_", " ").split()).lower()

        funnel_row = None
        default_funnel_id_raw = os.getenv("DEFAULT_FUNNEL_ID", "").strip()
        if default_funnel_id_raw.isdigit():
            funnel_row = db.execute(
                text(
                    "SELECT id, nome, etapas FROM funnels "
                    "WHERE id = :fid AND is_active = true "
                    "LIMIT 1"
                ),
                {"fid": int(default_funnel_id_raw)},
            ).fetchone()
            if not funnel_row:
                logger.error(
                    "DEFAULT_FUNNEL_ID=%s nao aponta para nenhum funil ATIVO. O "
                    "lead %s sera criado SEM funil em vez de entrar num funil "
                    "arbitrario — corrija a configuracao.",
                    default_funnel_id_raw, lead_id,
                )
        else:
            alvo_nome = _norm(os.getenv("DEFAULT_FUNNEL_NOME", "Vendas: Principal"))
            ativos = db.execute(
                text("SELECT id, nome, etapas FROM funnels WHERE is_active = true")
            ).fetchall()
            candidatos = [f for f in ativos if _norm(f.nome) == alvo_nome]
            if len(candidatos) == 1:
                funnel_row = candidatos[0]
            elif not candidatos:
                logger.error(
                    "Nenhum funil ATIVO chamado %r. O lead %s sera criado SEM "
                    "funil — configure DEFAULT_FUNNEL_ID ou DEFAULT_FUNNEL_NOME.",
                    os.getenv("DEFAULT_FUNNEL_NOME", "Vendas: Principal"), lead_id,
                )
            else:
                logger.error(
                    "AMBIGUIDADE: %s funis ATIVOS normalizam para o mesmo nome "
                    "(ids %s). O lead %s sera criado SEM funil — renomeie para "
                    "desambiguar.",
                    len(candidatos), sorted(f.id for f in candidatos), lead_id,
                )

        if funnel_row:
            funnel_id = funnel_row.id
            etapas = funnel_row.etapas

            # AUDIT-2026-08-WF2 — a etapa deixou de ser `stages[0]`.
            #
            # "Primeira etapa" nao e conceito do negocio: e a ordem em que
            # alguem arrastou os cartoes na tela de configuracao do funil.
            # Reordenar mudava, sem aviso, onde todo lead novo nasce. Agora
            # procura a etapa cujo `id` OU `nome` casa com DEFAULT_ETAPA_NOME
            # ("Sem Contato" por default), normalizando `_` e espaco — porque o
            # `etapa_id` real de producao pode ser `sem_contato` ou
            # `Sem Contato` e este repositorio nao tem como saber qual dos dois
            # (nada aqui cria funil). Mesma regra de
            # app/services/lead_creation.py:resolver_etapa_inicial.
            import json
            stages = etapas if isinstance(etapas, list) else json.loads(etapas)
            # AUDIT-2026-08-WF2 (revisao) — `id` tem precedencia sobre `nome`,
            # e empate desempata pelo proprio id, NUNCA pela posicao na lista.
            # Mesma regra de app/services/lead_creation.py:_casar_etapa: sem
            # isto, reordenar as etapas na tela de configuracao do funil mudava
            # onde o lead nasce. Etapa sem `id` e descartada — casava por
            # `nome` e devolvia None como etapa.
            stages = [e for e in stages if isinstance(e, dict) and e.get("id")]
            alvo_etapa = _norm(os.getenv("DEFAULT_ETAPA_NOME", "Sem Contato"))
            casam = [e for e in stages if _norm(e["id"]) == alvo_etapa]
            if not casam:
                casam = [e for e in stages if _norm(e.get("nome")) == alvo_etapa]
            first_stage_id = None
            if len(casam) == 1:
                first_stage_id = casam[0]["id"]
            elif casam:
                first_stage_id = min(e["id"] for e in casam)
                logger.warning(
                    "Funil #%s: %s etapas casam com %r (ids %s). Usando %r, "
                    "escolhida pelo id e nao pela posicao — funil ambiguo.",
                    funnel_id, len(casam), alvo_etapa,
                    sorted(e["id"] for e in casam), first_stage_id,
                )
            if first_stage_id is None:
                if stages:
                    first_stage_id = stages[0]["id"]
                    logger.warning(
                        "Funil #%s nao tem etapa %r — usando a primeira (%r). A "
                        "posicao na lista nao e contrato de negocio.",
                        funnel_id, os.getenv("DEFAULT_ETAPA_NOME", "Sem Contato"),
                        first_stage_id,
                    )
                else:
                    first_stage_id = "nova_oportunidade"
                    logger.warning(
                        "Funil #%s esta sem etapas utilizaveis — usando %r.",
                        funnel_id, first_stage_id,
                    )

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
            #
            # AUDIT-2026-08-WF2 (revisao): o log nao bastava. O INSERT em
            # `lead_history` vivia DENTRO do `if funnel_row`, entao o lead que
            # nascia sem funil ficava tambem sem NENHUMA linha de historico —
            # invisivel no Kanban E com a timeline vazia, sem nada no proprio
            # registro dizendo por que. O CRM (app/services/lead_creation.py)
            # sempre gravou o `created` com um campo `aviso` nesse caso; o
            # espelho daqui nao. Duas origens, mesmo estado de configuracao,
            # rastreabilidade oposta.
            #
            # Antes desta rodada o `else` era quase inalcancavel (a query
            # antiga so falhava com zero funis ativos). O fail-closed do funil
            # abriu tres caminhos ate aqui — id mal configurado, nome nao
            # encontrado, nome ambiguo — e nenhum deles levava o historico.
            logger.warning(
                "Lead #%s criado sem funil: nenhum funil ativo resolvido.",
                lead_id,
            )
            db.execute(
                text(
                    "INSERT INTO lead_history "
                    "(lead_id, evento, descricao, dados, created_at) "
                    "VALUES (:lid, 'created', :desc, :dados, NOW())"
                ),
                {
                    "lid": lead_id,
                    "desc": "Lead criado automaticamente via WhatsApp, SEM funil.",
                    # String JSON literal, como o INSERT irmao logo acima: a
                    # coluna e JSON e o schema de resposta valida CADA linha.
                    "dados": '{"aviso": "nenhum funil ativo resolvido na criacao"}',
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

    lead_data, bloquear_criacao = await lookup_lead_by_whatsapp(conversation.whatsapp, db)

    # Lead not found — create automatically
    if not lead_data:
        # AUDIT-2026-08-WF2 — "nao achei" e "nao sei" nao sao a mesma coisa.
        # Com o pre-filtro corrigido o guard de ambiguidade finalmente enxerga o
        # par formatado/nao-formatado do MESMO cliente; se criassemos assim
        # mesmo, o unico efeito de detectar a duplicata seria produzir uma
        # TERCEIRA. Sem vinculo e sem lead novo: a conversa fica pendente ate
        # alguem unificar os leads no CRM, que e a unica correcao possivel.
        if bloquear_criacao:
            logger.warning(
                "Conversa %s NAO vinculada: o WhatsApp dela nao identifica um "
                "unico lead (ambiguidade ou falha de consulta — o log logo acima "
                "diz qual). Nenhum lead foi criado; desambigue no CRM.",
                conversation.id,
            )
            return False
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
