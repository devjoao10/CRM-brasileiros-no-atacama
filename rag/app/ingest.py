"""Pipeline de ingestão idempotente (full / incremental / dry-run / validate).

Fonte de verdade = Markdown. Gera manifest + chunks, calcula hashes, compara
com o índice anterior, gera embeddings SOMENTE para chunks novos/alterados
(via embed_documents, task_type de DOCUMENTO), faz upsert, remove órfãos e
deduplica por chunk_hash. Métricas seguras (sem conteúdo dos documentos).

BIA-RAG-EMBEDDINGS-01:
- metadados de compatibilidade persistidos (provider/model/dims/task types/
  index_version/corpus_hash/indexed_at);
- índice criado com backend/modelo/dimensão diferentes (ex.: text-embedding-004)
  é considerado INCOMPATÍVEL e força full rebuild — nunca mistura vetores;
- full rebuild ATÔMICO: constrói banco temporário, valida, faz smoke, e só então
  troca pelo ativo (preservando backup); em falha, mantém o ativo anterior.
"""
import hashlib
import json
import os
from pathlib import Path

from . import config
from .chunker import chunk_document
from .corpus import build_manifest
from .embeddings import get_backend
from .store import SqliteVectorStore

META_KEYS = [
    "embedding_provider", "embedding_model", "embedding_dimensions",
    "document_task_type", "query_task_type", "index_version", "corpus_hash",
    "indexed_at",
]


def _iso_now(indexed_at: str) -> str:
    return indexed_at or "unknown"


def _corpus_hash(included_files) -> str:
    h = hashlib.sha256()
    for f in sorted(included_files, key=lambda x: x["relative_path"]):
        h.update(f"{f['relative_path']}:{f['content_hash']}\n".encode("utf-8"))
    return h.hexdigest()


def _read_meta(db_path) -> dict:
    """Lê os metadados de compatibilidade de um índice existente (ou {})."""
    p = Path(db_path or config.DB_PATH)
    if not p.exists():
        return {}
    store = SqliteVectorStore(db_path=p)
    try:
        return {k: store.get_meta(k) for k in META_KEYS}
    finally:
        store.close()


def index_compatible(existing_meta: dict, desc: dict) -> tuple[bool, str]:
    """Índice é compatível se provider/model/dimensions/document_task_type batem
    com o backend atual. Metadados ausentes ⇒ incompatível."""
    if not existing_meta or not existing_meta.get("embedding_model"):
        return False, "metadados de embedding ausentes (indice antigo/parcial)"
    checks = {
        "embedding_provider": str(desc["embedding_provider"]),
        "embedding_model": str(desc["embedding_model"]),
        "embedding_dimensions": str(desc["embedding_dimensions"]),
        "document_task_type": str(desc["document_task_type"]),
    }
    for k, want in checks.items():
        got = str(existing_meta.get(k) or "")
        if got != want:
            return False, f"{k}: indice={got!r} != atual={want!r}"
    return True, "compativel"


def _write_meta(store, desc, index_version, corpus_hash, indexed_at):
    store.set_meta("embedding_provider", str(desc["embedding_provider"]))
    store.set_meta("embedding_model", str(desc["embedding_model"]))
    store.set_meta("embedding_dimensions", str(desc["embedding_dimensions"]))
    store.set_meta("document_task_type", str(desc["document_task_type"]))
    store.set_meta("query_task_type", str(desc["query_task_type"]))
    store.set_meta("index_version", index_version)
    store.set_meta("corpus_hash", corpus_hash)
    store.set_meta("indexed_at", _iso_now(indexed_at))


def _build_chunks(corpus_dir, manifest):
    included = [f for f in manifest["files"] if f["included_in_rag"]]
    all_chunks = []
    per_file = {}
    for f in included:
        text = (corpus_dir / f["relative_path"]).read_text(encoding="utf-8")
        chunks = chunk_document(f["relative_path"], text, f)
        per_file[f["relative_path"]] = len(chunks)
        all_chunks.extend(chunks)
    for f in manifest["files"]:
        f["chunk_count"] = per_file.get(f["relative_path"], 0)
    return all_chunks


def _ingest_into(store, all_chunks, backend, indexed_at, metrics, reuse_existing):
    """Embeda (documentos) e faz upsert dos chunks no store. reuse_existing:
    quando True, só (re)embeda hashes ausentes (incremental)."""
    seen, valid = set(), set()
    existing = store.all_hashes() if reuse_existing else set()
    to_embed = []
    for c in all_chunks:
        if c["chunk_hash"] in seen:
            metrics["dedup_skipped"] += 1
            continue
        seen.add(c["chunk_hash"]); valid.add(c["chunk_hash"])
        if c["chunk_hash"] not in existing:
            to_embed.append(c)

    if to_embed:
        vectors = backend.embed_documents([c["text"] for c in to_embed])  # task DOCUMENTO
        if len(vectors) != len(to_embed):
            raise RuntimeError(
                f"embeddings inconsistentes: {len(to_embed)} textos, {len(vectors)} vetores")
        emb_by_hash = {c["chunk_hash"]: v for c, v in zip(to_embed, vectors)}
    else:
        emb_by_hash = {}

    for c in all_chunks:
        if c["chunk_hash"] in emb_by_hash:
            res = store.upsert(c, emb_by_hash[c["chunk_hash"]], indexed_at)
            if res == "inserted":
                metrics["new_chunks"] += 1
            elif res == "updated":
                metrics["updated_chunks"] += 1
        else:
            metrics["unchanged_chunks"] += 1
    metrics["orphan_chunks_removed"] = store.delete_orphans(valid)


def run_ingest(*, corpus_dir=None, db_path=None, backend_name=None,
               indexed_at="", dry_run=False, full=False) -> dict:
    corpus_dir = Path(corpus_dir or config.CORPUS_DIR)
    active_db = Path(db_path or config.DB_PATH)
    manifest = build_manifest(corpus_dir, indexed_at=indexed_at)
    backend = get_backend(backend_name)
    desc = backend.describe()

    all_chunks = _build_chunks(corpus_dir, manifest)
    corpus_hash = _corpus_hash([f for f in manifest["files"] if f["included_in_rag"]])

    metrics = {
        "index_version": manifest["index_version"], "collection": manifest["collection"],
        "backend": backend.name, "embedding_model": desc["embedding_model"],
        "embedding_dimensions": desc["embedding_dimensions"],
        "document_task_type": desc["document_task_type"],
        "query_task_type": desc["query_task_type"],
        "indexed_at": _iso_now(indexed_at), "corpus_hash": corpus_hash,
        "total_files": manifest["total_files"], "included_files": manifest["included_files"],
        "excluded_files": manifest["excluded_files"], "total_chunks": len(all_chunks),
        "new_chunks": 0, "updated_chunks": 0, "unchanged_chunks": 0,
        "orphan_chunks_removed": 0, "dedup_skipped": 0,
        "dry_run": dry_run, "full_rebuild": full,
        "incompatible_index_rebuilt": False, "by_status": {},
    }
    for c in all_chunks:
        metrics["by_status"][c["validation_status"]] = metrics["by_status"].get(c["validation_status"], 0) + 1

    if dry_run:
        backend.close()
        return {"manifest": manifest, "metrics": metrics}

    # Compatibilidade: índice existente com outro provider/model/dims/task ⇒
    # força full rebuild atômico (nunca mistura vetores de espaços diferentes).
    existing_meta = _read_meta(active_db)
    compatible, reason = index_compatible(existing_meta, desc)
    if not full and active_db.exists() and not compatible:
        metrics["incompatible_index_rebuilt"] = True
        metrics["incompatibility_reason"] = reason
        full = True
        metrics["full_rebuild"] = True

    try:
        if full:
            _atomic_full_rebuild(active_db, all_chunks, backend, desc,
                                 manifest["index_version"], corpus_hash, indexed_at, metrics)
        else:
            store = SqliteVectorStore(db_path=active_db)
            try:
                _ingest_into(store, all_chunks, backend, indexed_at, metrics, reuse_existing=True)
                _write_meta(store, desc, manifest["index_version"], corpus_hash, indexed_at)
                store.set_meta("last_metrics", json.dumps(metrics))
                metrics["store_stats"] = store.stats()
            finally:
                store.close()
    finally:
        backend.close()

    return {"manifest": manifest, "metrics": metrics}


def _atomic_full_rebuild(active_db, all_chunks, backend, desc, index_version,
                         corpus_hash, indexed_at, metrics):
    """Constrói um banco temporário, valida, faz smoke, e só então troca pelo
    ativo (preservando backup). Em falha, o ativo anterior é mantido intacto."""
    active_db = Path(active_db)
    active_db.parent.mkdir(parents=True, exist_ok=True)
    tmp_db = active_db.with_suffix(active_db.suffix + ".rebuild.tmp")
    if tmp_db.exists():
        tmp_db.unlink()

    # backup do ativo anterior (mesmo vazio/parcial), se existir
    backup_path = None
    if active_db.exists():
        backup_path = active_db.with_suffix(active_db.suffix + f".bak-{_iso_now(indexed_at).replace(':','').replace('-','')}")
        try:
            import shutil
            shutil.copyfile(str(active_db), str(backup_path))
            metrics["previous_index_backup"] = backup_path.name
        except OSError:
            backup_path = None

    tmp_store = SqliteVectorStore(db_path=tmp_db)
    try:
        _ingest_into(tmp_store, all_chunks, backend, indexed_at, metrics, reuse_existing=False)
        _write_meta(tmp_store, desc, index_version, corpus_hash, indexed_at)
        tmp_store.set_meta("last_metrics", json.dumps(metrics))
        # validação: contagem coerente e vetores presentes
        cnt = tmp_store.count()
        if cnt == 0 or cnt < len({c["chunk_hash"] for c in all_chunks}) * 0.5:
            raise RuntimeError(f"rebuild produziu contagem implausivel ({cnt})")
        metrics["store_stats"] = tmp_store.stats()
    except Exception:
        tmp_store.close()
        if tmp_db.exists():
            tmp_db.unlink()
        raise  # ativo anterior permanece intacto
    finally:
        try:
            tmp_store.close()
        except Exception:  # noqa: BLE001
            pass

    # smoke no banco temporário ANTES da troca (sem tocar no ativo)
    _rebuild_smoke(tmp_db, backend)

    # troca atômica: substitui o ativo pelo temporário
    os.replace(str(tmp_db), str(active_db))
    metrics["atomic_swap"] = True


def _rebuild_smoke(db_path, backend):
    """Smoke mínimo no índice recém-construído (corpus-agnóstico): índice não
    vazio; uma consulta derivada de um chunk validado REAL do índice fundamenta;
    nenhum chunk factual contém marcador pendente."""
    from .retrieve import retrieve
    store = SqliteVectorStore(db_path=db_path)
    try:
        if store.count() == 0:
            raise RuntimeError("smoke: indice vazio apos rebuild")
        row = store.conn.execute(
            "SELECT text FROM chunks WHERE collection=? AND validation_status=? LIMIT 1",
            (store.collection, config.STATUS_VALIDATED)).fetchone()
        sample = row["text"] if row else None
    finally:
        store.close()
    if not sample:
        return  # sem chunk validado: só a checagem de não-vazio se aplica
    query = " ".join(sample.split()[:12])
    r = retrieve(query, db_path=db_path, backend_name=backend.name)
    if any(config.PENDING_MARKER in c["text"] for c in r["context_chunks"]):
        raise RuntimeError("smoke: filtro de pendencia falhou")
    if not r["grounded"]:
        raise RuntimeError("smoke: consulta de um chunk real nao fundamentou")


def status(db_path=None) -> dict:
    store = SqliteVectorStore(db_path=db_path)
    try:
        meta = {k: store.get_meta(k) for k in META_KEYS}
        return {
            "index_version": store.get_meta("index_version"),
            "indexed_at": store.get_meta("indexed_at"),
            "embedding": {
                "provider": meta.get("embedding_provider"),
                "model": meta.get("embedding_model"),
                "dimensions": meta.get("embedding_dimensions"),
                "document_task_type": meta.get("document_task_type"),
                "query_task_type": meta.get("query_task_type"),
            },
            "corpus_hash": meta.get("corpus_hash"),
            "stats": store.stats(),
        }
    finally:
        store.close()
