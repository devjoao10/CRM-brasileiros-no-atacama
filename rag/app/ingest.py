"""Pipeline de ingestão idempotente (full / incremental / dry-run / validate).

Fonte de verdade = Markdown. Gera manifest + chunks, calcula hashes, compara
com o índice anterior, gera embeddings SOMENTE para chunks novos/alterados,
faz upsert, remove chunks órfãos e deduplica por chunk_hash. Métricas seguras
(sem conteúdo dos documentos).
"""
import json
from pathlib import Path

from . import config
from .chunker import chunk_document
from .corpus import build_manifest
from .embeddings import get_backend
from .store import SqliteVectorStore


def _iso_now(indexed_at: str) -> str:
    # timestamp injetado pelo chamador (CLI/serviço) para manter reprodutibilidade
    return indexed_at or "unknown"


def run_ingest(*, corpus_dir=None, db_path=None, backend_name=None,
               indexed_at="", dry_run=False, full=False) -> dict:
    corpus_dir = Path(corpus_dir or config.CORPUS_DIR)
    manifest = build_manifest(corpus_dir, indexed_at=indexed_at)
    backend = get_backend(backend_name)

    included = [f for f in manifest["files"] if f["included_in_rag"]]

    # gera todos os chunks do corpus atual
    all_chunks = []
    per_file_counts = {}
    for f in included:
        text = (corpus_dir / f["relative_path"]).read_text(encoding="utf-8")
        chunks = chunk_document(f["relative_path"], text, f)
        per_file_counts[f["relative_path"]] = len(chunks)
        all_chunks.extend(chunks)
    for f in manifest["files"]:
        f["chunk_count"] = per_file_counts.get(f["relative_path"], 0)

    metrics = {
        "index_version": manifest["index_version"],
        "collection": manifest["collection"],
        "backend": backend.name,
        "indexed_at": _iso_now(indexed_at),
        "total_files": manifest["total_files"],
        "included_files": manifest["included_files"],
        "excluded_files": manifest["excluded_files"],
        "total_chunks": len(all_chunks),
        "new_chunks": 0,
        "updated_chunks": 0,
        "unchanged_chunks": 0,
        "orphan_chunks_removed": 0,
        "dedup_skipped": 0,
        "dry_run": dry_run,
        "full_rebuild": full,
        "by_status": {},
    }
    for c in all_chunks:
        metrics["by_status"][c["validation_status"]] = metrics["by_status"].get(c["validation_status"], 0) + 1

    if dry_run:
        return {"manifest": manifest, "metrics": metrics}

    store = SqliteVectorStore(db_path=db_path)
    try:
        if full:
            # rebuild limpo mantendo o arquivo (apaga a coleção)
            store.conn.execute("DELETE FROM chunks WHERE collection=?", (store.collection,))
            store.conn.commit()

        # dedup por chunk_hash dentro do corpus atual
        seen = set()
        valid_hashes = set()
        # embeddings só para novos/alterados: descobrimos quais faltam
        existing = store.all_hashes()
        to_embed = []
        for c in all_chunks:
            if c["chunk_hash"] in seen:
                metrics["dedup_skipped"] += 1
                continue
            seen.add(c["chunk_hash"])
            valid_hashes.add(c["chunk_hash"])
            # precisa (re)embeddar? novo hash → sim
            if c["chunk_hash"] not in existing:
                to_embed.append(c)

        # gera embeddings em lote
        if to_embed:
            vectors = backend.embed([c["text"] for c in to_embed])
            emb_by_hash = {c["chunk_hash"]: v for c, v in zip(to_embed, vectors)}
        else:
            emb_by_hash = {}

        # upsert de todos os chunks atuais (reusa embedding existente quando
        # inalterado — evitamos recomputar: se já existe, mantemos o vetor)
        for c in all_chunks:
            if c["chunk_hash"] in emb_by_hash:
                res = store.upsert(c, emb_by_hash[c["chunk_hash"]], indexed_at)
                if res == "inserted":
                    metrics["new_chunks"] += 1
                elif res == "updated":
                    metrics["updated_chunks"] += 1
            else:
                # já existe com o mesmo hash → nada a fazer (unchanged)
                metrics["unchanged_chunks"] += 1

        metrics["orphan_chunks_removed"] = store.delete_orphans(valid_hashes)
        store.set_meta("index_version", manifest["index_version"])
        store.set_meta("indexed_at", _iso_now(indexed_at))
        store.set_meta("last_metrics", json.dumps(metrics))
        metrics["store_stats"] = store.stats()
    finally:
        store.close()

    return {"manifest": manifest, "metrics": metrics}


def status(db_path=None) -> dict:
    store = SqliteVectorStore(db_path=db_path)
    try:
        return {
            "index_version": store.get_meta("index_version"),
            "indexed_at": store.get_meta("indexed_at"),
            "stats": store.stats(),
        }
    finally:
        store.close()
