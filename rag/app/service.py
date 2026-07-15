"""Serviço RAG da BIA (FastAPI). Rede interna apenas; auth por header.

Endpoints: GET /health, GET /status, POST /ingest, POST /search, POST /validate.
Sem exposição pública. Corpus montado read-only. Logs sem conteúdo sensível.
"""
import datetime

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from . import config
from .answer import build_agent_payload
from .corpus import build_manifest
from .ingest import run_ingest, status as ingest_status
from .retrieve import retrieve
from .store import SqliteVectorStore

app = FastAPI(title="BIA RAG Context Service", version="1.0.0")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def require_auth(x_bia_rag_token: str = Header(default="", alias=config.AUTH_HEADER)):
    """Auth interna. Se BIA_RAG_INTERNAL_SECRET não estiver setado (dev/testes),
    o serviço fica aberto SOMENTE em ambiente sem secret — em produção o secret
    é obrigatório (documentado no runbook e no compose)."""
    if not config.INTERNAL_AUTH_SECRET:
        return  # dev/local
    if x_bia_rag_token != config.INTERNAL_AUTH_SECRET:
        raise HTTPException(status_code=401, detail="unauthorized")


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=config.DEFAULT_TOP_K, ge=1, le=20)
    categories: list[str] | None = None
    include_pending: bool = False
    as_payload: bool = True


class IngestRequest(BaseModel):
    mode: str = Field(default="incremental")  # incremental | full | dry-run


@app.get("/health")
def health():
    try:
        store = SqliteVectorStore()
        n = store.count()
        iv = store.get_meta("index_version")
        store.close()
        return {"status": "healthy" if n > 0 else "empty", "chunks": n, "index_version": iv}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"unhealthy: {type(exc).__name__}")


@app.get("/status")
def status(_=Depends(require_auth)):
    return ingest_status()


@app.post("/ingest")
def ingest(req: IngestRequest, _=Depends(require_auth)):
    mode = req.mode.lower()
    res = run_ingest(indexed_at=_now(),
                     dry_run=(mode == "dry-run"),
                     full=(mode == "full"))
    # nunca retornar conteúdo de documentos — só métricas
    return res["metrics"]


@app.post("/validate")
def validate(_=Depends(require_auth)):
    man = build_manifest(indexed_at=_now())
    by_status: dict[str, int] = {}
    for f in man["files"]:
        if f["included_in_rag"]:
            by_status[f["validation_status"]] = by_status.get(f["validation_status"], 0) + 1
    return {
        "total_files": man["total_files"],
        "included_files": man["included_files"],
        "excluded_files": man["excluded_files"],
        "by_status": by_status,
        "index_version": man["index_version"],
    }


@app.post("/search")
def search(req: SearchRequest, _=Depends(require_auth)):
    res = retrieve(req.query, top_k=req.top_k, categories=req.categories,
                   include_pending=req.include_pending)
    if req.as_payload:
        payload = build_agent_payload(res)
        payload["query"] = req.query
        return payload
    # versão "slim" (sem o texto dos chunks) para observabilidade
    return {
        "query": req.query, "grounded": res["grounded"], "answerable": res["answerable"],
        "pending_validation": res["pending_validation"], "conflict": res["conflict"],
        "confidence": res["confidence"], "sources": res["sources"],
        "warnings": res["warnings"], "index_version": res["index_version"],
    }
