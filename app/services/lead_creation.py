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

from app.config import DEFAULT_ETAPA_NOME, DEFAULT_FUNNEL_ID, DEFAULT_FUNNEL_NOME
from app.models.lead import Lead
from app.models.pipeline import Funnel, FunnelEntry, LeadHistory
from app.models.tag import Tag

logger = logging.getLogger(__name__)

# Mesmo fallback usado em conversas/app/services/crm.py:auto_create_lead_in_crm
# quando o funil nao tem etapas (ou elas vem mal formadas).
_ETAPA_FALLBACK = "nova_oportunidade"


def _normalizar(valor) -> str:
    """
    Chave de comparacao para nome de funil e de etapa.

    Minusculas, sem espaco nas bordas, e `_` tratado como espaco. Esse ultimo
    detalhe existe por um motivo concreto: o `etapa_id` real gravado em
    producao NAO e conhecivel a partir deste repositorio — nada aqui cria
    funil, e app/schemas/pipeline.py documenta `sem_contato` e `Sem Contato`
    como igualmente validos. Normalizar `_` faz as duas grafias resolverem para
    a mesma etapa, sem que ninguem precise adivinhar qual e a real nem mexer em
    dado de producao para descobrir.

    NAO e busca por substring: e igualdade sobre a forma normalizada.
    "Vendas WhatsApp" nunca casa com "Vendas: Principal".
    """
    return " ".join(str(valor or "").replace("_", " ").split()).lower()


def resolver_funil_padrao(db: Session, funnel_id: Optional[int] = None) -> Optional[Funnel]:
    """
    Resolve o funil onde um lead novo deve entrar.

    AUDIT-2026-08-WF2 — NAO existe mais fallback por ordem de id.

    A versao anterior caia, na falta de `DEFAULT_FUNNEL_ID`, no "funil ATIVO de
    MENOR id". Isso amarrava uma regra de negocio a um acidente de historico: o
    funil certo so vencia porque tinha sido criado primeiro. Criar um funil novo
    com id menor, ou desativar e recriar o principal, mandava silenciosamente
    todo lead novo para o lugar errado — sem erro, sem log, sem sintoma ate
    alguem reparar que o Kanban do time esvaziou.

    Precedencia agora:

      1. `funnel_id` explicito — vence e NAO cai para os passos seguintes.
         Inexistente devolve None: quem decide o que fazer e o CALLER (este
         service nunca levanta HTTPException — app/services/CLAUDE.md).
      2. `DEFAULT_FUNNEL_ID`, se configurado. Configurado e apontando para funil
         INEXISTENTE ou INATIVO devolve None e loga ERROR — NAO cai para o passo
         3. Configuracao errada tem de doer, nao ser contornada em silencio por
         um funil arbitrario.
      3. `DEFAULT_FUNNEL_NOME` (default "Vendas: Principal"), por igualdade
         EXATA sobre o nome normalizado, entre os funis ATIVOS. `funnels.nome` e
         UNIQUE (app/models/pipeline.py), entao esse nome e um identificador
         estavel do dominio — e e o mesmo contrato que o system message do
         Gerenciador ja declara (gerenciador_leads.json: "o funil
         'Vendas: Principal', sempre na etapa 'Sem Contato'").

    Nao encontrado em nenhum passo: None + ERROR. O lead ainda e criado (nao ha
    motivo de dominio para recusar o cadastro), mas o problema fica LOGADO e
    registrado no historico do proprio lead — nunca escondido.

    NUNCA por substring, NUNCA por ordem de id, NUNCA "o primeiro ativo".
    """
    if funnel_id is not None:
        return db.query(Funnel).filter(Funnel.id == funnel_id).first()

    if DEFAULT_FUNNEL_ID is not None:
        funnel = db.query(Funnel).filter(
            Funnel.id == DEFAULT_FUNNEL_ID, Funnel.is_active == True,  # noqa: E712
        ).first()
        if funnel is None:
            logger.error(
                "DEFAULT_FUNNEL_ID=%s nao aponta para nenhum funil ATIVO. O lead "
                "sera criado SEM funil em vez de entrar num funil arbitrario — "
                "corrija a configuracao.", DEFAULT_FUNNEL_ID,
            )
        return funnel

    alvo = _normalizar(DEFAULT_FUNNEL_NOME)
    candidatos = [
        f for f in db.query(Funnel).filter(Funnel.is_active == True).all()  # noqa: E712
        if _normalizar(f.nome) == alvo
    ]
    if len(candidatos) == 1:
        return candidatos[0]

    if not candidatos:
        logger.error(
            "Nenhum funil ATIVO chamado %r. O lead sera criado SEM funil — "
            "configure DEFAULT_FUNNEL_ID ou DEFAULT_FUNNEL_NOME, ou crie/reative "
            "o funil.", DEFAULT_FUNNEL_NOME,
        )
    else:
        # `funnels.nome` e UNIQUE, entao isto so acontece se dois nomes diferirem
        # apenas por caixa/espaco/underscore. Escolher um deles seria exatamente
        # o tipo de decisao silenciosa que esta correcao remove.
        logger.error(
            "AMBIGUIDADE: %s funis ATIVOS normalizam para %r (ids %s). O lead "
            "sera criado SEM funil — renomeie para desambiguar.",
            len(candidatos), DEFAULT_FUNNEL_NOME, sorted(f.id for f in candidatos),
        )
    return None


def resolver_funil_por_nome(db: Session, nome: str) -> Optional[Funnel]:
    """
    Funil ATIVO cujo nome casa EXATAMENTE (na forma normalizada) com `nome`.

    Existe para quem conhece o funil pelo nome e nao pelo id — o caso do
    formulario do site, cujo id (`3`) so vive dentro do workflow do n8n e nao
    esta versionado em lugar nenhum deste repositorio.

    Devolve None quando nao ha exatamente um: zero (nao existe/inativo) ou mais
    de um (nomes que so diferem por caixa/espaco/underscore). O caller decide o
    que fazer — este service nao levanta HTTPException.
    """
    alvo = _normalizar(nome)
    candidatos = [
        f for f in db.query(Funnel).filter(Funnel.is_active == True).all()  # noqa: E712
        if _normalizar(f.nome) == alvo
    ]
    if len(candidatos) == 1:
        return candidatos[0]
    if len(candidatos) > 1:
        logger.error(
            "AMBIGUIDADE: %s funis ATIVOS normalizam para %r (ids %s).",
            len(candidatos), nome, sorted(f.id for f in candidatos),
        )
    return None


def resolver_etapa_inicial(funnel: Funnel, etapa_id: Optional[str] = None) -> str:
    """
    Decide em qual etapa do funil o lead entra.

    AUDIT-2026-08-WF2 — deixou de ser `etapas[0]`.

    Antes, sem `etapa_id` explicito, a entrada ia para a PRIMEIRA etapa da
    lista. "Primeira etapa" nao e um conceito do negocio: e a ordem em que
    alguem arrastou os cartoes na tela de configuracao do funil. Reordenar as
    etapas mudava, sem aviso, onde todo lead novo nasce.

    Precedencia:
      1. `etapa_id` explicito, se existir NESTE funil.
      2. a etapa cujo `id` OU `nome` casa com `DEFAULT_ETAPA_NOME` (default
         "Sem Contato") na forma normalizada — ver `_normalizar` para por que
         `id` e `nome` sao os dois comparados.
      3. `etapas[0]`, com WARNING. Continua sendo melhor que nao criar a
         entrada, mas agora e ruidoso em vez de ser a regra.
      4. sem etapas utilizaveis: `_ETAPA_FALLBACK`.
    """
    etapas = [e for e in (funnel.etapas or []) if isinstance(e, dict)]
    if etapa_id and any(e.get("id") == etapa_id for e in etapas):
        return etapa_id

    alvo = _normalizar(DEFAULT_ETAPA_NOME)
    for etapa in etapas:
        if _normalizar(etapa.get("id")) == alvo or _normalizar(etapa.get("nome")) == alvo:
            return etapa["id"]

    if etapas and etapas[0].get("id"):
        logger.warning(
            "Funil %r (#%s) nao tem etapa %r — usando a primeira (%r). A posicao "
            "na lista nao e contrato de negocio; renomeie a etapa ou ajuste "
            "DEFAULT_ETAPA_NOME.",
            funnel.nome, funnel.id, DEFAULT_ETAPA_NOME, etapas[0]["id"],
        )
        return etapas[0]["id"]

    logger.warning(
        "Funil %r (#%s) esta sem etapas utilizaveis — usando %r.",
        funnel.nome, funnel.id, _ETAPA_FALLBACK,
    )
    return _ETAPA_FALLBACK


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
    etapa_resolvida = resolver_etapa_inicial(funnel, etapa_id)

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
