"""
Settings API — Frases Automáticas + Horário Comercial.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_user, require_admin, User
from app.models.auto_reply import AUTO_REPLY_PADROES, AutoReply, BusinessHours
from app.schemas.settings import (
    AutoReplyUpdate,
    AutoReplyResponse,
    AutoReplyListResponse,
    BusinessHoursUpdate,
    BusinessHoursResponse,
    BusinessHoursListResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings", tags=["Configurações"])


# ─── Auto Replies ────────────────────────────────

@router.get("/auto-replies", response_model=AutoReplyListResponse)
async def list_auto_replies(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Listar as frases automáticas: o catálogo inteiro (com linha ou não) + linhas fora dele."""
    replies = db.query(AutoReply).order_by(AutoReply.id).all()

    # CONV-EXPEDIENTE-01: em produção o seed não roda e a tabela pode estar vazia. Os gatilhos do
    # catálogo vêm SEMPRE na ordem do catálogo — com a linha do banco quando existe, ou como
    # placeholder INATIVO, sem id e com o texto padrão (a linha nasce no 1º PUT). Assim a frase
    # não muda de posição ao ganhar a primeira linha. Linha fora do catálogo vai depois, por id.
    por_gatilho = {r.trigger: r for r in replies}
    itens = [
        AutoReplyResponse.model_validate(por_gatilho[padrao["trigger"]])
        if padrao["trigger"] in por_gatilho
        else AutoReplyResponse(
            trigger=padrao["trigger"],
            title=padrao["title"],
            message=padrao["message"],
            is_active=False,
        )
        for padrao in AUTO_REPLY_PADROES
    ]
    gatilhos_do_catalogo = {padrao["trigger"] for padrao in AUTO_REPLY_PADROES}
    itens += [AutoReplyResponse.model_validate(r) for r in replies if r.trigger not in gatilhos_do_catalogo]
    return AutoReplyListResponse(auto_replies=itens, total=len(itens))


@router.put("/auto-replies/{trigger}", response_model=AutoReplyResponse)
async def update_auto_reply(
    trigger: str,
    data: AutoReplyUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Editar uma frase automática pelo trigger (cria a linha se for um gatilho do catálogo).

    CONV-EXPEDIENTE-01: só administrador salva. Como o PUT agora cria e ativa frases que o
    webhook envia a clientes, deixá-lo aberto a qualquer usuário logado (achado F-320) deixaria
    qualquer vendedora mudar o texto que sai em nome da empresa. O GET segue liberado.
    """
    reply = db.query(AutoReply).filter(AutoReply.trigger == trigger).first()
    criada = reply is None
    if criada:
        # CONV-EXPEDIENTE-01: só gatilho do catálogo vira linha; qualquer outro continua 404.
        # Nasce com título/texto padrão e INATIVA; os campos do payload são aplicados abaixo.
        # Só entra na sessão (`db.add`) depois da validação do estado efetivo, mais abaixo.
        padrao = next((p for p in AUTO_REPLY_PADROES if p["trigger"] == trigger), None)
        if padrao is None:
            raise HTTPException(status_code=404, detail=f"Frase automática '{trigger}' não encontrada")
        reply = AutoReply(
            trigger=trigger,
            title=padrao["title"],
            message=padrao["message"],
            is_active=False,
        )

    # CONV-EXPEDIENTE-01: frase ativa sem texto é frase que o webhook pula em silêncio. Decide ANTES
    # de alterar a linha, pelo estado EFETIVO (payload + linha atual): sem `rollback`, a linha só
    # muda se a combinação for válida. A orientação depende do que o usuário estava fazendo:
    # apagar o texto de uma frase que JÁ estava ativa, ou ativar uma frase sem texto.
    mensagem_final = reply.message if data.message is None else data.message
    ativa_final = reply.is_active if data.is_active is None else data.is_active
    if ativa_final and not (mensagem_final or "").strip():
        if reply.is_active and data.message is not None:
            detail = (
                "A mensagem de uma frase ativa nao pode ficar vazia. "
                "Desative a frase antes de apagar o texto."
            )
        else:
            detail = "Escreva a mensagem antes de ativar a frase automatica."
        raise HTTPException(status_code=422, detail=detail)

    if criada:
        db.add(reply)
    if data.message is not None:
        reply.message = data.message
    if data.is_active is not None:
        reply.is_active = data.is_active

    # CONV-EXPEDIENTE-01: o id de quem alterou é lido ANTES do commit. O `current_user` é um ORM
    # preso a esta mesma sessão: depois do commit ele está expirado, e ler `.id` só para logar
    # custaria um SELECT em `users` (a linha inteira, com o hash da senha) com a alteração já gravada.
    usuario_id = current_user.id
    db.commit()
    db.refresh(reply)
    logger.info(
        f"✅ Frase automática '{trigger}' {'criada' if criada else 'atualizada'} "
        f"(usuário {usuario_id})"
    )
    return AutoReplyResponse.model_validate(reply)


# ─── Business Hours ──────────────────────────────

def _business_hours_da_semana(db: Session) -> BusinessHoursListResponse:
    """Sempre os 7 dias (0=segunda ... 6=domingo), na ordem.

    CONV-EXPEDIENTE-01: dia sem linha no banco = aberto, sem horário — é o que o webhook já
    considera em `_is_within_business_hours` (sem configuração, assume aberto). Assim a tela
    mostra e salva a semana inteira mesmo com a tabela vazia (produção não roda o seed).
    """
    por_dia = {h.weekday: h for h in db.query(BusinessHours).all()}
    return BusinessHoursListResponse(
        hours=[
            BusinessHoursResponse.model_validate(por_dia[weekday])
            if weekday in por_dia
            else BusinessHoursResponse(weekday=weekday, is_open=True)
            for weekday in range(7)
        ],
    )


@router.get("/business-hours", response_model=BusinessHoursListResponse)
async def list_business_hours(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Listar horário de todos os 7 dias da semana."""
    return _business_hours_da_semana(db)


@router.put("/business-hours", response_model=BusinessHoursListResponse)
async def update_business_hours(
    data: BusinessHoursUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Atualizar horário comercial (batch — 7 dias de uma vez).

    CONV-EXPEDIENTE-01: só administrador salva (mesmo motivo do PUT de frases): o horário decide
    quando o aviso "Fora do expediente" sai para o cliente. O GET segue liberado.
    """
    # CONV-EXPEDIENTE-01: lê as linhas existentes UMA vez, num dict por weekday, e a linha criada
    # entra nele também. O schema (`BusinessHoursUpdate`) já recusa weekday repetido com 422, então
    # na prática cada dia aparece uma só vez; guardar a linha nova no dict é defesa em profundidade
    # deliberada: a rota continua segura (atualiza a linha pendente em vez de inserir uma segunda
    # e estourar o UNIQUE de `weekday`) mesmo se for chamada com dias repetidos sem passar pelo schema.
    por_dia = {h.weekday: h for h in db.query(BusinessHours).all()}
    for day in data.days:
        hours = por_dia.get(day.weekday)
        if hours:
            hours.is_open = day.is_open
            hours.open_time = day.open_time
            hours.close_time = day.close_time
        else:
            hours = BusinessHours(
                weekday=day.weekday,
                is_open=day.is_open,
                open_time=day.open_time,
                close_time=day.close_time,
            )
            db.add(hours)
            por_dia[day.weekday] = hours

    # Id de quem alterou lido ANTES do commit (mesmo motivo do PUT de frases).
    usuario_id = current_user.id
    db.commit()
    logger.info(f"✅ Horário comercial atualizado (usuário {usuario_id})")

    return _business_hours_da_semana(db)
