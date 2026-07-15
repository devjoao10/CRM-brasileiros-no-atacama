"""Configuração do serviço RAG da BIA. Sem segredos hardcoded — tudo por env."""
import os
from pathlib import Path

# Raiz do corpus canônico (montado read-only no container). Default resolve
# para <repo>/bna_agent_context relativo a este arquivo (rag/app/config.py).
_DEFAULT_CORPUS = Path(__file__).resolve().parents[2] / "bna_agent_context"
CORPUS_DIR = Path(os.getenv("BIA_RAG_CORPUS_DIR", str(_DEFAULT_CORPUS)))

# Vector store persistente (SQLite). Em container: volume dedicado.
_DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "bna_bia_context.sqlite3"
DB_PATH = Path(os.getenv("BIA_RAG_DB_PATH", str(_DEFAULT_DB)))

COLLECTION = os.getenv("BIA_RAG_COLLECTION", "bia_context_v1")

# Backend de embeddings: "gemini" (produção) ou "deterministic" (testes/offline).
# Aceita BIA_RAG_EMBEDDING_BACKEND (legado) ou BIA_RAG_EMBEDDING_PROVIDER.
EMBEDDING_BACKEND = os.getenv(
    "BIA_RAG_EMBEDDING_BACKEND",
    os.getenv("BIA_RAG_EMBEDDING_PROVIDER", "deterministic"),
)
# GEMINI_API_KEY é lido pelo backend Gemini via ambiente; nunca impresso.
GEMINI_API_KEY_ENV = "GEMINI_API_KEY"

# Modelo/dimensão/task types do Gemini (SDK google-genai). NÃO usar
# text-embedding-004 (descontinuado, 404 na API). Defaults seguros → o operador
# não precisa adicionar essas variáveis manualmente.
EMBEDDING_MODEL = os.getenv("BIA_RAG_EMBEDDING_MODEL", "gemini-embedding-001")
EMBEDDING_DIMENSIONS = int(os.getenv("BIA_RAG_EMBEDDING_DIMENSIONS", "768"))
DOCUMENT_TASK_TYPE = os.getenv("BIA_RAG_DOCUMENT_TASK_TYPE", "RETRIEVAL_DOCUMENT")
QUERY_TASK_TYPE = os.getenv("BIA_RAG_QUERY_TASK_TYPE", "QUESTION_ANSWERING")
QUERY_FALLBACK_TASK_TYPE = os.getenv("BIA_RAG_QUERY_FALLBACK_TASK_TYPE", "RETRIEVAL_QUERY")
EMBEDDING_BATCH_SIZE = int(os.getenv("BIA_RAG_EMBEDDING_BATCH_SIZE", "32"))

# Dimensão do backend determinístico (offline/testes; independente do Gemini).
DETERMINISTIC_DIM = int(os.getenv("BIA_RAG_DETERMINISTIC_DIM", "256"))

# Autenticação interna do serviço (header). Valor só no ambiente do container.
INTERNAL_AUTH_SECRET = os.getenv("BIA_RAG_INTERNAL_SECRET", "")
AUTH_HEADER = "X-BIA-RAG-Token"

# Chunking
CHUNK_MIN_TOKENS = int(os.getenv("BIA_RAG_CHUNK_MIN_TOKENS", "120"))
CHUNK_MAX_TOKENS = int(os.getenv("BIA_RAG_CHUNK_MAX_TOKENS", "900"))
CHUNK_TARGET_TOKENS = int(os.getenv("BIA_RAG_CHUNK_TARGET_TOKENS", "600"))
CHUNK_OVERLAP_TOKENS = int(os.getenv("BIA_RAG_CHUNK_OVERLAP_TOKENS", "80"))

# Retrieval
DEFAULT_TOP_K = int(os.getenv("BIA_RAG_TOP_K", "6"))
SCORE_THRESHOLD = float(os.getenv("BIA_RAG_SCORE_THRESHOLD", "0.18"))
MAX_CHUNKS_PER_FILE = int(os.getenv("BIA_RAG_MAX_CHUNKS_PER_FILE", "3"))

# Estados de validação (mission §11)
STATUS_VALIDATED = "validado"
STATUS_PARTIAL = "parcialmente_validado"
STATUS_PENDING = "pendente_validacao"
STATUS_ADMIN = "administrativo"
STATUS_HISTORICAL = "historico"

PENDING_MARKER = "[PENDENTE_VALIDACAO]"

# Diretórios de conteúdo semântico (allowlist). READMEs e _meta são navegação/
# administrativo → excluídos do corpus factual.
CONTENT_DIRS = [
    "00_persona", "01_empresa", "02_destinos", "03_tours", "04_precos",
    "05_politicas", "06_saude_seguranca", "07_faq_objecoes",
    "08_operacao_agente", "09_guardrails",
]
INDEX_VERSION = os.getenv("BIA_RAG_INDEX_VERSION", "bia_context_v1")
