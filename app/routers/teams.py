from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.team import Team
from app.models.user import User
from app.schemas.team import TeamCreate, TeamUpdate, TeamResponse, TeamListResponse
from app.auth import require_admin

router = APIRouter(prefix="/api/teams", tags=["Equipes"])

@router.get("", response_model=TeamListResponse)
async def list_teams(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    """Lista todas as equipes cadastradas."""
    teams = db.query(Team).all()
    return TeamListResponse(total=len(teams), teams=[TeamResponse.model_validate(t) for t in teams])

@router.post("", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
async def create_team(
    data: TeamCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin)
):
    """Cria uma nova equipe."""
    team = Team(nome=data.nome, descricao=data.descricao, cor=data.cor)
    db.add(team)
    db.commit()
    db.refresh(team)
    return TeamResponse.model_validate(team)

@router.get("/{team_id}", response_model=TeamResponse)
async def get_team(
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
async def update_team(
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
    for field, value in update_data.items():
        setattr(team, field, value)

    db.commit()
    db.refresh(team)
    return TeamResponse.model_validate(team)

@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(
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
async def set_team_members(
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
    # Replace all users in the team with the new list
    team.users = users
    db.commit()
    
    return {"message": f"Equipe atualizada com {len(users)} membros"}
