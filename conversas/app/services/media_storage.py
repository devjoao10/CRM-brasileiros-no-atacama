"""
CONV-02 — Storage de midia do Conversas (espelho local dos binarios da Meta).

Orquestra o ciclo de vida do MediaAsset:
  referenced --download ok--> downloaded
  referenced --falha-------> failed (last_error com resumo SEGURO)

Regras de seguranca deste modulo:
  - O nome do arquivo local e gerado 100% server-side (asset_<id>.<ext do MIME>)
    — nenhum dado do cliente/provider entra no path (anti path-traversal na escrita).
  - `resolve_local_file` so devolve paths CONFINADOS ao MEDIA_STORAGE_DIR
    (anti path-traversal na leitura, mesmo se local_path for corrompido no banco).
  - Erros persistidos em last_error sao resumos seguros (padrao CONV-08b) —
    nunca token/headers/payload.
"""

import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from app.config import MEDIA_STORAGE_DIR
from app.models.media_asset import MediaAsset
from app.services import whatsapp
from app.services import media_policy

logger = logging.getLogger(__name__)

# Extensoes explicitas p/ MIMEs comuns do WhatsApp (mimetypes do Windows/Linux
# divergem; tabela fixa = nomes de arquivo deterministicos)
_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/3gpp": ".3gp",
    "audio/aac": ".aac",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/amr": ".amr",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
}


def storage_dir() -> Path:
    d = Path(MEDIA_STORAGE_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ext_for_mime(mime_type: Optional[str]) -> str:
    if not mime_type:
        return ".bin"
    base = mime_type.split(";")[0].strip().lower()
    if base in _EXT_BY_MIME:
        return _EXT_BY_MIME[base]
    guessed = mimetypes.guess_extension(base)
    return guessed or ".bin"


def _safe_filename(asset: MediaAsset, mime_type: Optional[str]) -> str:
    # 100% server-side: id numerico + extensao derivada do MIME validado
    return f"asset_{asset.id}{_ext_for_mime(mime_type)}"


def resolve_local_file(asset: MediaAsset) -> Optional[Path]:
    """
    Resolve o arquivo local de um asset, CONFINADO ao storage dir.
    Retorna None se nao ha arquivo, se o path escapa do diretorio (traversal)
    ou se o arquivo nao existe no disco.
    """
    if not asset.local_path:
        return None
    base = storage_dir().resolve()
    try:
        candidate = (base / asset.local_path).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(base):
        logger.warning(f"Path traversal bloqueado no asset {asset.id}")
        return None
    if not candidate.is_file():
        return None
    return candidate


def _mark_failed(asset: MediaAsset, db: Session, summary: str) -> MediaAsset:
    asset.status = "failed"
    asset.last_error = (summary or "falha no download da midia")[:300]
    db.commit()
    db.refresh(asset)
    logger.warning(f"Download de midia FALHOU (asset {asset.id}): {asset.last_error}")
    return asset


def store_bytes(
    asset: MediaAsset, content: bytes, mime_type: Optional[str], db: Session
) -> MediaAsset:
    """Grava o binario no storage e transiciona o asset para 'downloaded'."""
    filename = _safe_filename(asset, mime_type or asset.meta_mime_type)
    path = storage_dir() / filename
    path.write_bytes(content)

    asset.local_path = filename  # relativo ao storage dir
    asset.local_size_bytes = len(content)
    asset.downloaded_at = datetime.now(timezone.utc)
    asset.status = "downloaded"
    asset.last_error = None
    if mime_type and not asset.meta_mime_type:
        asset.meta_mime_type = mime_type
    db.commit()
    db.refresh(asset)
    logger.info(f"Midia do asset {asset.id} espelhada localmente ({len(content)} bytes)")
    return asset


async def download_media_asset(asset: MediaAsset, db: Session, msg_type: Optional[str] = None) -> MediaAsset:
    """
    Baixa a midia de um asset 'referenced' (ou re-tenta um 'failed') da Meta.
    Nunca levanta excecao por falha do provider — transiciona o asset e retorna.
    """
    if asset.status == "downloaded" and resolve_local_file(asset):
        return asset  # ja espelhado
    if not asset.meta_media_id:
        return _mark_failed(asset, db, "asset sem meta_media_id")

    # 1) resolve media_id -> URL temporaria (+ metadados de tamanho)
    info = await whatsapp.get_media_url(asset.meta_media_id, db)
    if not isinstance(info, dict) or info.get("error"):
        summary = info.get("summary") if isinstance(info, dict) else None
        # media_id expirado (Meta responde 400/404 apos ~30 dias)
        if isinstance(info, dict) and info.get("status_code") in (400, 404):
            asset.status = "expired"
            asset.last_error = (summary or "media_id expirado na Meta")[:300]
            db.commit()
            db.refresh(asset)
            return asset
        return _mark_failed(asset, db, summary or "falha ao resolver media_id")
    if info.get("simulated"):
        return _mark_failed(asset, db, "Meta nao configurada (modo dev) — download indisponivel")

    mime_type = info.get("mime_type") or asset.meta_mime_type
    file_size = info.get("file_size")

    # 2) politica: valida MIME/tamanho ANTES de baixar
    kind = msg_type or media_policy.classify_mime(mime_type) or "document"
    ok, reason = media_policy.validate(kind, mime_type, file_size)
    if not ok:
        return _mark_failed(asset, db, f"politica de midia: {reason}")

    # 3) baixa o binario
    result = await whatsapp.download_media_content(info.get("url", ""), db)
    if not isinstance(result, dict) or result.get("error") or result.get("simulated"):
        summary = result.get("summary") if isinstance(result, dict) else None
        return _mark_failed(asset, db, summary or "falha ao baixar o binario")

    content = result.get("content") or b""
    if not content:
        return _mark_failed(asset, db, "download vazio")
    # defesa em profundidade: tamanho real tambem respeita a politica
    ok, reason = media_policy.validate(kind, mime_type, len(content))
    if not ok:
        return _mark_failed(asset, db, f"politica de midia: {reason}")

    return store_bytes(asset, content, mime_type, db)
