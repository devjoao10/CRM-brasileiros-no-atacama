# -*- coding: utf-8 -*-
"""
AUDIT-2026-08-WB (F-341) — caminho UNICO de criacao de lead do CRM.

Antes desta correcao, `POST /api/leads` (app/routers/leads.py) criava SO a
linha em `leads`: nenhuma FunnelEntry, nenhum LeadHistory, nenhuma tag. Todo
lead criado pelo agente n8n "Gerenciador"/"Bia" ficava FORA do Kanban —
`app/routers/pipeline.py` so renderiza lead que tem FunnelEntry, e
`GET /api/pipeline/locate/{lead_id}` devolvia 404, entao "Ver no Funil" nao
fazia nada.

O outro criador de lead do sistema, `conversas/app/services/crm.py`
(`auto_create_lead_in_crm`, SQL cru — modulo separado, nao importa este
arquivo), sempre fez os tres passos. Os dois caminhos haviam divergido. Este
modulo passa a ser o UNICO lugar do CRM (app/) que decide o que acontece
quando um lead nasce: funil default, entrada no funil, evento no historico e
tag de origem. `conversas/app/services/crm.py` continua em SQL cru (bases
compartilhadas, mas modulos e processos diferentes — nao da para importar
este arquivo de la) e foi corrigido em paralelo para usar a MESMA precedencia
de escolha de funil (ver comentario la).
"""
import logging
from typing import Optional

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import DEFAULT_FUNNEL_ID
from app.models.lead import Lead
from app.models.pipeline import Funnel, FunnelEntry, LeadHistory
from app.models.tag import Tag

logger = logging.getLogger(__name__)

# Mesmo fallback usado em conversas/app/services/crm.py:auto_create_lead_in_crm
# quando o funil nao tem etapas (ou elas vem mal formadas).
_ETAPA_FALLBACK = "nova_oportunidade"


def resolver_funil_padrao(db: Session, funnel_id: Optional[int] = None) -> Optional[Funnel]:
    """
    Resolve o funil onde um lead novo deve entrar. Precedencia:

      1. `funnel_id` explicito — vence e NAO cai para os passos seguintes.
         Se nao existir, devolve None: quem decide o que fazer (404, criar sem
         funil, etc.) e o CALLER — este service nunca levanta HTTPException
         (app/services/CLAUDE.md).
      2. `DEFAULT_FUNNEL_ID` (app/config.py), se apontar para um funil ATIVO.
      3. o funil ATIVO de MENOR id — deterministico, nao depende de nome.

    NUNCA prefere um funil so porque o nome contem "whatsapp". Essa heuristica
    existia em conversas/app/services/crm.py (`ORDER BY (LOWER(nome) LIKE
    '%whatsapp%') DESC`) e mandava lead de qualquer origem para "Vendas
    WhatsApp" em vez do funil principal (W2-10) — corrigida aqui e la, com a
    MESMA regra dos tres itens acima.
    """
    if funnel_id is not None:
        return db.query(Funnel).filter(Funnel.id == funnel_id).first()

    if DEFAULT_FUNNEL_ID is not None:
        funnel = db.query(Funnel).filter(
            Funnel.id == DEFAULT_FUNNEL_ID, Funnel.is_active == True,  # noqa: E712
        ).first()
        if funnel is not None:
            return funnel

    return (
        db.query(Funnel)
        .filter(Funnel.is_active == True)  # noqa: E712
        .order_by(Funnel.id.asc())
        .first()
    )


def garantir_entrada_no_funil(
    db: Session, lead_id: int, funnel: Funnel, etapa_id: Optional[str] = None,
) -> FunnelEntry:
    """
    Garante que o lead tem uma FunnelEntry neste funil. IDEMPOTENTE POR
    CONSTRAINT DO BANCO, nao por check-then-act: existe SELECT-entao-INSERT
    identico em `add_lead_to_funnel` (app/routers/pipeline.py), e entre um
    SELECT e um INSERT cabe outra requisicao concorrente — exatamente o que
    `uq_funnel_entries_lead_funnel` (indice unico em (lead_id, funnel_id),
    app/models/pipeline.py:49-51, aplicado em producao pela migration m011)
    existe para impedir.

    Por isso o INSERT roda dentro de um SAVEPOINT (`db.begin_nested()`), nunca
    de um `db.rollback()` direto: um `IntegrityError` aqui significa que OUTRA
    transacao venceu a corrida (este mesmo service chamado duas vezes, ou um
    POST direto em /api/pipeline/funnels/{id}/leads chegando primeiro) — nao
    que o lead inteiro deva ser descartado. `db.rollback()` desfaria a
    transacao INTEIRA, incluindo o `Lead` que `criar_lead` acabou de inserir e
    ainda nao commitou. O SAVEPOINT desfaz SO o INSERT que colidiu; a
    transacao externa continua valida e `criar_lead` segue para o commit final.
    """
    etapas = funnel.etapas or []
    stage_ids = [s.get("id") for s in etapas if isinstance(s, dict)]

    if etapa_id and etapa_id in stage_ids:
        etapa_resolvida = etapa_id
    elif etapas and isinstance(etapas[0], dict) and etapas[0].get("id"):
        etapa_resolvida = etapas[0]["id"]
    else:
        etapa_resolvida = _ETAPA_FALLBACK

    posicao = db.query(FunnelEntry).filter(
        FunnelEntry.funnel_id == funnel.id,
        FunnelEntry.etapa_id == etapa_resolvida,
    ).count()

    entry = FunnelEntry(
        lead_id=lead_id, funnel_id=funnel.id, etapa_id=etapa_resolvida, posicao=posicao,
    )
    try:
        with db.begin_nested():
            db.add(entry)
            db.flush()
    except IntegrityError:
        # A UNICA violacao esperada aqui e `uq_funnel_entries_lead_funnel`: o
        # outro inserinte ganhou a corrida. No PostgreSQL o indice unico BLOQUEIA
        # o segundo ate o primeiro commitar, entao o re-SELECT enxerga a linha.
        entry = db.query(FunnelEntry).filter(
            FunnelEntry.lead_id == lead_id,
            FunnelEntry.funnel_id == funnel.id,
        ).first()
        if entry is None:
            # AUDIT-2026-08-WB (revisao) — nao era essa a violacao. Engolir aqui
            # devolveria None e o chamador quebraria com AttributeError em
            # `entry.etapa_id`, escondendo o erro real do banco atras de um
            # crash confuso. Levanta o original.
            raise
    return entry


def _obter_ou_criar_tag(db: Session, nome: str) -> Tag:
    """
    Busca a tag por nome CASE-INSENSITIVE; cria se nao existir. Mesma corrida
    de `garantir_entrada_no_funil`, so que aqui quem colide e `tags.nome`
    (unique=True em app/models/tag.py) — duas criacoes de lead concorrentes
    pedindo a MESMA tag nova pela primeira vez. SAVEPOINT pelo mesmo motivo:
    nao pode derrubar o Lead ja inserido nesta transacao.
    """
    nome = nome.strip()
    tag = db.query(Tag).filter(func.lower(Tag.nome) == nome.lower()).first()
    if tag is not None:
        return tag
    try:
        with db.begin_nested():
            tag = Tag(nome=nome)
            db.add(tag)
            db.flush()
    except IntegrityError:
        # Mesma logica de `garantir_entrada_no_funil`: a violacao esperada e
        # `tags.nome`, e o re-SELECT tem de encontrar a tag que o outro criou.
        tag = db.query(Tag).filter(func.lower(Tag.nome) == nome.lower()).first()
        if tag is None:
            # AUDIT-2026-08-WB (revisao) — outra violacao. Sem isto o chamador
            # faria `lead.tags.append(None)` e quebraria o relacionamento com um
            # erro que nao aponta para a causa.
            raise
    return tag


def criar_lead(
    db: Session,
    *,
    dados: dict,
    funnel_id: Optional[int] = None,
    etapa_id: Optional[str] = None,
    tag_nome: Optional[str] = None,
    origem: str,
) -> Lead:
    """
    Cria o lead e, na MESMA transacao: entrada no funil default (ou no
    `funnel_id` explicito), evento 'created' no historico e a tag de origem
    (se houver). UM `db.commit()` no final — nada fica meio-aplicado.

    `dados` e um dict ja validado na fronteira (schema Pydantic em
    app/routers/leads.py, ou a linha normalizada do import); este service nao
    valida FORMATO de payload (app/schemas/), so a regra de negocio "todo lead
    novo entra no Kanban".
    """
    lead = Lead(
        nome=dados.get("nome"),
        email=dados.get("email"),
        whatsapp=dados.get("whatsapp"),
        destinos=dados.get("destinos") or [],
        data_chegada=dados.get("data_chegada"),
        data_partida=dados.get("data_partida"),
        total_dias=dados.get("total_dias"),
        datas_destinos=dados.get("datas_destinos") or {},
        dias_por_destino=dados.get("dias_por_destino") or {},
        num_viajantes=dados.get("num_viajantes"),
        num_criancas=dados.get("num_criancas") or 0,
        idades_criancas=dados.get("idades_criancas"),
        campos_personalizados=dados.get("campos_personalizados") or {},
        status_venda=dados.get("status_venda") or "em_negociacao",
        responsavel_id=dados.get("responsavel_id"),
    )
    db.add(lead)
    # autoflush=False neste projeto (app/database.py) — flush explicito para
    # ganhar lead.id antes de criar a FunnelEntry/LeadHistory que referenciam.
    db.flush()

    funnel = resolver_funil_padrao(db, funnel_id)
    historico_dados = {"origem": origem}

    if funnel is not None:
        entry = garantir_entrada_no_funil(db, lead.id, funnel, etapa_id)
        historico_dados["funnel_id"] = funnel.id
        historico_dados["etapa_id"] = entry.etapa_id
        descricao = f"Lead criado ({origem}) e adicionado ao funil '{funnel.nome}'"
    else:
        # Nunca pular em silencio — a causa raiz original do F-341 tambem
        # cobria isto ("se funnel_row e None, o bloco e pulado e o lead ainda
        # commita"). Sem funil ativo o lead E criado (nao existe motivo de
        # dominio para recusar o cadastro), mas o problema fica LOGADO e
        # registrado no proprio historico do lead — nunca escondido.
        logger.warning(
            "Lead #%s criado sem funil: nenhum funil ativo encontrado (origem=%s).",
            lead.id, origem,
        )
        historico_dados["aviso"] = "nenhum funil ativo encontrado no momento da criacao"
        descricao = f"Lead criado ({origem}) — sem funil (nenhum funil ativo)"

    # `dados` NUNCA None: GET /api/pipeline/history/{lead_id} valida cada linha
    # contra `HistoryResponse.dados: dict = {}` — mesma causa-raiz documentada
    # em conversas/app/services/crm.py:296-305 (AUDIT-2026-08-W2F F9), que
    # devolvia 500 justamente para os leads criados automaticamente.
    db.add(LeadHistory(
        lead_id=lead.id, evento="created", descricao=descricao, dados=historico_dados,
    ))

    if tag_nome:
        tag = _obter_ou_criar_tag(db, tag_nome)
        lead.tags.append(tag)

    db.commit()
    db.refresh(lead)
    return lead
