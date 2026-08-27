from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.team import Team
from app.models.user import User
from app.schemas.team import TeamCreate, TeamUpdate, TeamResponse, TeamListResponse
from app.auth import require_admin

router = APIRouter(prefix="/api/teams", tags=["Equipes"])

# AUDIT-2026-08-W2G (F12): handlers deste router são `def` puros, não
# `async def` — fazem I/O SÍNCRONO do SQLAlchemy, que como `async` rodava no
# event loop e travava as demais requisições do worker.


def _assert_nome_livre(db: Session, nome: str, ignorar_id=None) -> None:
    """AUDIT-2026-08-W2G (F8): equipe não tinha NENHUMA checagem de nome único.

    Tags, funis, segmentos e usuários têm; equipes não, nem no app nem no banco
    (`app/models/team.py` não declara `unique=True` — pertence a outro agente
    nesta wave, ver NOTES). POSTs repetidos criavam equipes indistinguíveis na
    UI. A checagem vale também no PUT: sem isso o rename refaz a duplicata que o
    create passou a barrar.
    """
    q = db.query(Team.id).filter(Team.nome == nome)
    if ignorar_id is not None:
        q = q.filter(Team.id != ignorar_id)
    if q.first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Já existe uma equipe com este nome"
        )

@router.get("", response_model=TeamListResponse)
def list_teams(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    """Lista todas as equipes cadastradas."""
    teams = db.query(Team).all()
    return TeamListResponse(total=len(teams), teams=[TeamResponse.model_validate(t) for t in teams])

@router.post("", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
def create_team(
    data: TeamCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    """Cria uma nova equipe."""
    _assert_nome_livre(db, data.nome)

    team = Team(nome=data.nome, descricao=data.descricao, cor=data.cor)
    db.add(team)
    # AUDIT-2026-08-W2G (F7): o check acima é fast path e vira 409 se o índice
    # único for adicionado ao modelo (ver NOTES) — sem isso a corrida entre dois
    # creates devolveria um 500 opaco com a transação abortada.
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Já existe uma equipe com este nome"
        )
    db.refresh(team)
    return TeamResponse.model_validate(team)

@router.get("/{team_id}", response_model=TeamResponse)
def get_team(
    team_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    """Retorna os detalhes de uma equipe específica."""
    team = db.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Equipe não encontrada")
    return TeamResponse.model_validate(team)

@router.put("/{team_id}", response_model=TeamResponse)
def update_team(
    team_id: int,
    data: TeamUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    """Atualiza uma equipe."""
    team = db.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Equipe não encontrada")

    update_data = data.model_dump(exclude_unset=True)
    if "nome" in update_data and update_data["nome"] != team.nome:
        _assert_nome_livre(db, update_data["nome"], ignorar_id=team.id)

    for field, value in update_data.items():
        setattr(team, field, value)

    db.commit()
    db.refresh(team)
    return TeamResponse.model_validate(team)

@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_team(
    team_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    """Exclui uma equipe (sem excluir os usuários, apenas as remove da equipe)."""
    team = db.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Equipe não encontrada")
    
    db.delete(team)
    db.commit()
    return

@router.post("/{team_id}/members", summary="Redefine membros da equipe")
def set_team_members(
    team_id: int,
    user_ids: List[int],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    """Assinala uma lista de usuários para uma equipe, removendo os que não estão na lista."""
    team = db.query(Team).filter(Team.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Equipe não encontrada")
    
    users = db.query(User).filter(User.id.in_(user_ids)).all()
    # AUDIT-2026-08-W2G (F9): ids inexistentes eram DESCARTADOS em silêncio — o
    # admin mandava 10 ids, 3 obsoletos, e só via "7 membros" se reparasse no
    # número. Mesmo tratamento que `app/routers/tags.py::set_lead_tags` já dá.
    if len(users) != len(set(user_ids)):
        encontrados = {u.id for u in users}
        faltando = [uid for uid in user_ids if uid not in encontrados]
        raise HTTPException(status_code=404, detail=f"Usuários não encontrados: {faltando}")

    # Replace all users in the team with the new list
    team.users = users
    db.commit()
    
    return {"message": f"Equipe atualizada com {len(users)} membros"}
