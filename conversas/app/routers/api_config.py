"""
API Configuration Router — Manage WhatsApp Business API credentials.
Provides CRUD for the ApiConfig singleton + connection testing.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_user, User
from app.models.api_config import ApiConfig
from app.schemas.api_config import (
    ApiConfigUpdate,
    ApiConfigResponse,
    ApiConfigTestResponse,
)
from app.services import meta_templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/config", tags=["Configuração API"])


def _get_or_create_config(db: Session) -> ApiConfig:
    """Get the singleton ApiConfig or create it."""
    config = db.query(ApiConfig).filter(ApiConfig.id == 1).first()
    if not config:
        config = ApiConfig(id=1, meta_api_version="v21.0", is_connected=False)
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


@router.get("", response_model=ApiConfigResponse)
async def get_api_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Obter configuração atual da API WhatsApp."""
    config = _get_or_create_config(db)
    return ApiConfigResponse.from_model(config)


@router.put("", response_model=ApiConfigResponse)
async def update_api_config(
    data: ApiConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Atualizar credenciais da API WhatsApp."""
    config = _get_or_create_config(db)

    if data.meta_access_token is not None:
        config.meta_access_token = data.meta_access_token.strip() if data.meta_access_token else None
    if data.meta_phone_number_id is not None:
        config.meta_phone_number_id = data.meta_phone_number_id.strip() if data.meta_phone_number_id else None
    if data.meta_waba_id is not None:
        config.meta_waba_id = data.meta_waba_id.strip() if data.meta_waba_id else None
    if data.meta_verify_token is not None:
        config.meta_verify_token = data.meta_verify_token.strip() if data.meta_verify_token else None
    if data.meta_api_version is not None:
        config.meta_api_version = data.meta_api_version.strip() if data.meta_api_version else "v21.0"
    if data.webhook_url is not None:
        config.webhook_url = data.webhook_url.strip() if data.webhook_url else None

    db.commit()
    db.refresh(config)

    logger.info(f"Configuração da API atualizada por {current_user.email}")
    return ApiConfigResponse.from_model(config)


@router.post("/test", response_model=ApiConfigTestResponse)
async def test_api_connection(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Testar conexão com a Meta API usando as credenciais salvas."""
    result = await meta_templates.test_connection(db)
    return ApiConfigTestResponse(**result)


@router.post("/connect")
async def connect_api(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Testar e ativar a conexão com a Meta API."""
    config = _get_or_create_config(db)

    if not config.meta_access_token or not config.meta_waba_id:
        raise HTTPException(
            status_code=400,
            detail="Access Token e WABA ID são obrigatórios para conectar."
        )

    # Test the connection
    result = await meta_templates.test_connection(db)

    if result.get("success"):
        config.is_connected = True
        db.commit()
        logger.info(f"API WhatsApp conectada por {current_user.email}")
        return {
            "message": "Conexão estabelecida com sucesso!",
            "connected": True,
            **result,
        }
    else:
        config.is_connected = False
        db.commit()
        raise HTTPException(
            status_code=400,
            detail=result.get("error", "Falha na conexão com a Meta API.")
        )


@router.post("/disconnect")
async def disconnect_api(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Desconectar a API (não apaga credenciais, apenas desativa)."""
    config = _get_or_create_config(db)
    config.is_connected = False
    db.commit()
    logger.info(f"API WhatsApp desconectada por {current_user.email}")
    return {"message": "API desconectada.", "connected": False}
