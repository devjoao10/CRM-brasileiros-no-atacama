"""
CONV-02 — Endpoints autenticados de midia do Conversas.

Rotas finas (validam, checam auth, delegam ao service):
  POST /api/media/{asset_id}/fetch  -> baixa da Meta p/ o espelho local (sob demanda)
  GET  /api/media/{asset_id}        -> serve o binario local (preview/download)

Seguranca:
  - get_current_user obrigatorio (JWT header/cookie ou API key — mesmo modelo
    do restante do app; o frontend consome via fetch autenticado + blob).
  - Leitura confinada ao MEDIA_STORAGE_DIR (media_storage.resolve_local_file
    bloqueia qualquer traversal, mesmo com local_path corrompido).
  - Erros do provider viram resumos seguros (persistidos em last_error).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_user, User
from app.models.media_asset import MediaAsset
from app.schemas.conversation import MediaAssetResponse
from app.services import media_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/media", tags=["media"])


def _get_asset(asset_id: int, db: Session) -> MediaAsset:
    asset = db.query(MediaAsset).filter(MediaAsset.id == asset_id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="Midia nao encontrada")
    return asset


@router.post("/{asset_id}/fetch", response_model=MediaAssetResponse)
async def fetch_media(
    asset_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Baixa a midia da Meta para o espelho local (idempotente se ja baixada)."""
    asset = _get_asset(asset_id, db)
    msg_type = asset.message.msg_type if asset.message else None
    asset = await media_storage.download_media_asset(asset, db, msg_type=msg_type)

    if asset.status == "downloaded":
        return MediaAssetResponse.model_validate(asset)
    if asset.status == "expired":
        raise HTTPException(status_code=410, detail="Midia expirou na Meta (mais de 30 dias)")
    # failed — detail seguro (last_error ja e resumo sem segredo)
    raise HTTPException(
        status_code=502,
        detail=f"Nao foi possivel baixar a midia: {asset.last_error or 'falha desconhecida'}",
    )


@router.get("/{asset_id}")
async def serve_media(
    asset_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Serve o binario local de um asset baixado (preview/download autenticado)."""
    asset = _get_asset(asset_id, db)
    if asset.status != "downloaded":
        raise HTTPException(
            status_code=409,
            detail="Midia ainda nao baixada — use POST /api/media/{id}/fetch",
        )
    path = media_storage.resolve_local_file(asset)
    if path is None:
        # local_path ausente/corrompido/fora do storage (traversal) ou arquivo sumiu
        raise HTTPException(status_code=404, detail="Arquivo de midia indisponivel")

    return FileResponse(
        path,
        media_type=asset.meta_mime_type or "application/octet-stream",
        filename=asset.filename or path.name,
        content_disposition_type="inline",
    )
