#!/usr/bin/env python3
"""
check_readonly_db.py — valida a configuração do banco read-only da IA
(PERPETUA-INTERNAL-AUTH-01) SEM imprimir senhas.

O que faz:
  1. Lê DATABASE_READONLY_URL / DATABASE_URL do ambiente (via app.config).
  2. Ecoa a URL com a SENHA MASCARADA.
  3. Avisa se a URL read-only é igual à de escrita (em prod deveria ser o
     usuário dedicado crm_readonly).
  4. Abre uma conexão e roda `SELECT 1`.
  5. Reporta OK / FALHA com mensagem segura (nunca vaza a senha).

Uso:
    python scripts/check_readonly_db.py

Não altera dados. Não roda contra produção por conta própria — rode você mesmo,
apontando o ambiente para a instância que deseja validar.
"""
import sys
from urllib.parse import urlsplit, urlunsplit


def _mask(url: str) -> str:
    """Retorna a URL com a senha substituída por '***'."""
    if not url:
        return "(vazia)"
    try:
        parts = urlsplit(url)
        if parts.password:
            netloc = parts.netloc.replace(f":{parts.password}@", ":***@", 1)
            parts = parts._replace(netloc=netloc)
        return urlunsplit(parts)
    except Exception:
        # Fallback conservador: não arriscar vazar — corta no '@'.
        return url.split("@")[-1] if "@" in url else "(url não parseável)"


def main() -> int:
    try:
        from app.config import DATABASE_URL, DATABASE_READONLY_URL
    except Exception as e:
        print(f"[FALHA] Não foi possível carregar a config: {type(e).__name__}: {e}")
        return 2

    ro_url = DATABASE_READONLY_URL
    print("=== Verificação do banco read-only da IA ===")
    print(f"DATABASE_READONLY_URL : {_mask(ro_url)}")

    if not ro_url:
        print("[FALHA] DATABASE_READONLY_URL não está definida.")
        return 2

    if ro_url == DATABASE_URL:
        if ro_url.startswith("sqlite"):
            print("[INFO] SQLite (dev): read-only usa a mesma URL — esperado localmente.")
        else:
            print(
                "[AVISO] DATABASE_READONLY_URL == DATABASE_URL. Em produção use o "
                "usuário dedicado crm_readonly (somente SELECT), não o owner."
            )

    try:
        from sqlalchemy import create_engine, text
    except Exception as e:
        print(f"[FALHA] SQLAlchemy indisponível: {type(e).__name__}: {e}")
        return 2

    try:
        engine = create_engine(ro_url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("[OK] Conexão read-only estabelecida e SELECT 1 executado com sucesso.")
        return 0
    except Exception as e:
        # Mensagem segura: reporta o TIPO do erro, nunca a senha.
        name = type(e).__name__
        print(f"[FALHA] Não foi possível conectar/consultar no banco read-only ({name}).")
        print("        Verifique host, usuário crm_readonly e a senha CRM_READONLY_PASSWORD.")
        print("        (Detalhe completo do erro foi omitido para não vazar credenciais.)")
        return 1


if __name__ == "__main__":
    sys.exit(main())
