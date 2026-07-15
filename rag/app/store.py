"""Vector store persistente em SQLite (stdlib).

Persistência via arquivo (volume dedicado no container). Cosseno em Python puro
— adequado para a escala do corpus (centenas de chunks). Suporta upsert,
dedup por chunk_hash, delete de chunks órfãos, busca com filtros de metadados,
backup e restore. Sobrevive a reinício (é um arquivo em disco).
"""
import json
import math
import shutil
import sqlite3
from pathlib import Path

from . import config


class SqliteVectorStore:
    def __init__(self, db_path: Path | None = None, collection: str | None = None):
        self.db_path = Path(db_path or config.DB_PATH)
        self.collection = collection or config.COLLECTION
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_hash TEXT PRIMARY KEY,
                collection TEXT NOT NULL,
                source_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                validation_status TEXT NOT NULL,
                file_validation_status TEXT NOT NULL,
                canonical INTEGER NOT NULL,
                contains_pending INTEGER NOT NULL,
                category TEXT,
                destination TEXT,
                heading_path TEXT,
                title TEXT,
                language TEXT,
                tags TEXT,
                text TEXT NOT NULL,
                embedding TEXT NOT NULL,
                indexed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_source ON chunks(collection, source_path);
            CREATE INDEX IF NOT EXISTS idx_status ON chunks(collection, validation_status);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        self.conn.commit()

    # ---- escrita -------------------------------------------------------
    def upsert(self, chunk: dict, embedding: list[float], indexed_at: str) -> str:
        """Insere/atualiza um chunk. Retorna 'inserted'|'updated'|'unchanged'."""
        existing = self.conn.execute(
            "SELECT content_hash FROM chunks WHERE chunk_hash=? AND collection=?",
            (chunk["chunk_hash"], self.collection),
        ).fetchone()
        if existing and existing["content_hash"] == chunk["content_hash"]:
            return "unchanged"
        self.conn.execute(
            """
            INSERT INTO chunks (chunk_hash, collection, source_path, content_hash,
                chunk_index, validation_status, file_validation_status, canonical,
                contains_pending, category, destination, heading_path, title,
                language, tags, text, embedding, indexed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chunk_hash) DO UPDATE SET
                content_hash=excluded.content_hash,
                chunk_index=excluded.chunk_index,
                validation_status=excluded.validation_status,
                file_validation_status=excluded.file_validation_status,
                canonical=excluded.canonical,
                contains_pending=excluded.contains_pending,
                category=excluded.category, destination=excluded.destination,
                heading_path=excluded.heading_path, title=excluded.title,
                language=excluded.language, tags=excluded.tags,
                text=excluded.text, embedding=excluded.embedding,
                indexed_at=excluded.indexed_at
            """,
            (
                chunk["chunk_hash"], self.collection, chunk["source_path"],
                chunk["content_hash"], chunk["chunk_index"],
                chunk["validation_status"], chunk["file_validation_status"],
                int(bool(chunk["canonical"])), int(bool(chunk["contains_pending_validation"])),
                chunk.get("category", ""), chunk.get("destination", ""),
                chunk.get("heading_path", ""), chunk.get("title", ""),
                chunk.get("language", "pt-BR"), json.dumps(chunk.get("tags", [])),
                chunk["text"], json.dumps(embedding), indexed_at,
            ),
        )
        self.conn.commit()
        return "updated" if existing else "inserted"

    def delete_orphans(self, valid_hashes: set[str]) -> int:
        """Remove chunks cujo chunk_hash não está mais presente no corpus."""
        rows = self.conn.execute(
            "SELECT chunk_hash FROM chunks WHERE collection=?", (self.collection,)
        ).fetchall()
        to_del = [r["chunk_hash"] for r in rows if r["chunk_hash"] not in valid_hashes]
        for h in to_del:
            self.conn.execute(
                "DELETE FROM chunks WHERE chunk_hash=? AND collection=?", (h, self.collection)
            )
        self.conn.commit()
        return len(to_del)

    def delete_by_source(self, source_path: str) -> int:
        cur = self.conn.execute(
            "DELETE FROM chunks WHERE source_path=? AND collection=?",
            (source_path, self.collection),
        )
        self.conn.commit()
        return cur.rowcount

    def set_meta(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    # ---- leitura -------------------------------------------------------
    def count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) c FROM chunks WHERE collection=?", (self.collection,)
        ).fetchone()["c"]

    def all_hashes(self) -> set[str]:
        return {
            r["chunk_hash"]
            for r in self.conn.execute(
                "SELECT chunk_hash FROM chunks WHERE collection=?", (self.collection,)
            )
        }

    def stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT validation_status, COUNT(*) c FROM chunks WHERE collection=? GROUP BY validation_status",
            (self.collection,),
        ).fetchall()
        by_status = {r["validation_status"]: r["c"] for r in rows}
        files = self.conn.execute(
            "SELECT COUNT(DISTINCT source_path) c FROM chunks WHERE collection=?",
            (self.collection,),
        ).fetchone()["c"]
        return {"chunks": self.count(), "files": files, "by_status": by_status}

    @staticmethod
    def _cosine(a: list[float], b: list[float]) -> float:
        # vetores já normalizados no embeddings backend → dot = cosseno
        return sum(x * y for x, y in zip(a, b))

    def search(self, query_embedding: list[float], top_k: int, *,
               categories=None, include_pending=False,
               allowed_status=None) -> list[dict]:
        """Busca por similaridade com filtros de metadados. Retorna dicts com
        score e metadados (sem embedding)."""
        sql = "SELECT * FROM chunks WHERE collection=?"
        params = [self.collection]
        if categories:
            placeholders = ",".join("?" for _ in categories)
            sql += f" AND category IN ({placeholders})"
            params.extend(categories)
        if not include_pending:
            sql += " AND contains_pending=0"
        if allowed_status:
            placeholders = ",".join("?" for _ in allowed_status)
            sql += f" AND validation_status IN ({placeholders})"
            params.extend(allowed_status)
        rows = self.conn.execute(sql, params).fetchall()
        scored = []
        for r in rows:
            emb = json.loads(r["embedding"])
            score = self._cosine(query_embedding, emb)
            scored.append((score, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        results = []
        for score, r in scored[: max(top_k * 4, top_k)]:
            results.append({
                "score": round(score, 4),
                "source_path": r["source_path"],
                "heading_path": r["heading_path"],
                "title": r["title"],
                "validation_status": r["validation_status"],
                "file_validation_status": r["file_validation_status"],
                "canonical": bool(r["canonical"]),
                "contains_pending_validation": bool(r["contains_pending"]),
                "category": r["category"],
                "destination": r["destination"],
                "text": r["text"],
            })
        return results

    # ---- backup / restore ---------------------------------------------
    def backup(self, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        self.conn.commit()
        shutil.copyfile(str(self.db_path), str(dest))
        return dest

    def close(self):
        self.conn.close()

    @staticmethod
    def restore(backup_path: Path, db_path: Path):
        shutil.copyfile(str(backup_path), str(db_path))
