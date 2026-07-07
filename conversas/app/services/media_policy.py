"""
CONV-01 — Politica de MIME/tipo/tamanho de midia do Conversas.

Funcoes PURAS (sem I/O, sem DB): a fonte unica de verdade sobre o que o sistema
aceita como midia, alinhada aos tipos e limites suportados pela Meta Cloud API
(WhatsApp Business). Consumidores: CONV-02 (validacao de upload/download) e o
webhook (classificacao de inbound). Manter esta tabela aqui — e nao espalhada
em rotas — e o que impede divergencia de regras entre envio e recebimento.

Limites conforme documentacao da Meta Cloud API (v21):
  image 5 MB · video 16 MB · audio 16 MB · document 100 MB · sticker 500 KB
"""

from typing import Optional

MB = 1024 * 1024

# kind -> conjunto de MIMEs aceitos pela Meta Cloud API
ALLOWED_MIME = {
    "image": {"image/jpeg", "image/png", "image/webp"},
    "video": {"video/mp4", "video/3gpp"},
    "audio": {
        "audio/aac", "audio/mp4", "audio/mpeg", "audio/amr",
        "audio/ogg", "audio/opus",
    },
    "document": {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "text/plain",
    },
    "sticker": {"image/webp"},
}

# kind -> tamanho maximo em bytes
MAX_SIZE_BYTES = {
    "image": 5 * MB,
    "video": 16 * MB,
    "audio": 16 * MB,
    "document": 100 * MB,
    "sticker": 500 * 1024,
}

MEDIA_KINDS = frozenset(ALLOWED_MIME.keys())


def classify_mime(mime_type: Optional[str]) -> Optional[str]:
    """Retorna o kind ('image'|'video'|'audio'|'document'|'sticker') de um MIME, ou None."""
    if not mime_type:
        return None
    # Meta pode anexar parametros (ex.: 'audio/ogg; codecs=opus')
    base = mime_type.split(";")[0].strip().lower()
    for kind, mimes in ALLOWED_MIME.items():
        if base in mimes:
            # 'image/webp' e ambiguo (image e sticker compartilham); prioriza image
            if kind == "sticker" and base in ALLOWED_MIME["image"]:
                continue
            return kind
    return None


def is_allowed(kind: str, mime_type: Optional[str]) -> bool:
    """True se o MIME e aceito para o kind informado."""
    if kind not in ALLOWED_MIME or not mime_type:
        return False
    base = mime_type.split(";")[0].strip().lower()
    return base in ALLOWED_MIME[kind]


def max_size_for(kind: str) -> Optional[int]:
    """Tamanho maximo em bytes para o kind, ou None se kind desconhecido."""
    return MAX_SIZE_BYTES.get(kind)


def validate(kind: str, mime_type: Optional[str], size_bytes: Optional[int]) -> tuple[bool, Optional[str]]:
    """
    Valida (kind, mime, tamanho) contra a politica.
    Retorna (ok, motivo_seguro). `size_bytes=None` pula a checagem de tamanho
    (o webhook inbound nao informa tamanho; o CONV-02 informa apos download).
    O motivo e SEGURO para log/UI — nunca inclui conteudo do arquivo.
    """
    if kind not in MEDIA_KINDS:
        return False, f"tipo de midia nao suportado: {kind}"
    if not is_allowed(kind, mime_type):
        return False, f"MIME nao aceito para {kind}: {mime_type or '(vazio)'}"
    if size_bytes is not None:
        limit = MAX_SIZE_BYTES[kind]
        if size_bytes > limit:
            return False, f"tamanho excede o limite de {kind} ({size_bytes} > {limit} bytes)"
        if size_bytes <= 0:
            return False, "tamanho invalido (<= 0)"
    return True, None
