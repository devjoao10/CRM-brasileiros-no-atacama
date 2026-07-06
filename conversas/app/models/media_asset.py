from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class MediaAsset(Base):
    """
    CONV-01 — Fundacao de midia do Conversas.

    Uma linha por mensagem de midia (WhatsApp carrega exatamente 1 midia por
    mensagem -> message_id UNIQUE). Estrategia de storage HIBRIDA:

      - Referencia Meta (preenchida JA no webhook inbound): meta_media_id,
        meta_mime_type, meta_sha256, filename. O media_id da Meta vale ~30 dias;
        a URL de download expira em ~5 min (por isso o espelho local).
      - Espelho local (preenchido pelo CONV-02, download via Graph API):
        local_path, local_size_bytes, downloaded_at.

    `Message.media_url` continua existindo e recebendo o media_id inbound
    (compatibilidade com o frontend atual) ate CONV-02/03/04 migrarem o consumo.
    """
    __tablename__ = "media_assets"

    id = Column(Integer, primary_key=True, index=True)
    message_id = Column(
        Integer,
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # Referencia Meta (metadados publicos do webhook — sem token/URL assinada)
    meta_media_id = Column(String(100), nullable=True, index=True)
    meta_mime_type = Column(String(100), nullable=True)
    meta_sha256 = Column(String(100), nullable=True)
    filename = Column(String(255), nullable=True)  # documentos trazem filename

    # Ciclo de vida do asset:
    #   referenced -> so a referencia Meta e conhecida (estado do CONV-01)
    #   downloaded -> espelho local disponivel (CONV-02)
    #   failed     -> download falhou (CONV-02; resumo seguro em last_error)
    #   expired    -> media_id expirou antes do download (CONV-02)
    status = Column(String(20), default="referenced", nullable=False)

    # Espelho local (futuro CONV-02 — campos ja aditivos para evitar m005 so disso)
    local_path = Column(Text, nullable=True)
    local_size_bytes = Column(Integer, nullable=True)
    downloaded_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)  # resumo SEGURO (padrao CONV-08b)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    message = relationship("Message", back_populates="media_asset")

    def __repr__(self):
        return f"<MediaAsset(id={self.id}, message_id={self.message_id}, status='{self.status}')>"
