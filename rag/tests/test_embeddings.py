#!/usr/bin/env python3
"""Testes de embeddings e rebuild atômico (BIA-RAG-EMBEDDINGS-01).

Offline: usa um FAKE client injetado no GeminiBackend (sem rede, sem API key)
e o backend determinístico para atomicidade. Cobre §7 itens 1-15.
Rode:  python -m rag.tests.test_embeddings
"""
import math
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_fails = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


# ----------------------- Fake client do SDK google-genai -----------------------
class _FakeEmb:
    def __init__(self, values):
        self.values = values


class _FakeResp:
    def __init__(self, embeddings):
        self.embeddings = embeddings


class _FakeAPIError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f"fake api error status {code}")


def _task_of(cfg):
    return cfg["task_type"] if isinstance(cfg, dict) else getattr(cfg, "task_type", None)


def _dims_of(cfg):
    return cfg["output_dimensionality"] if isinstance(cfg, dict) else getattr(cfg, "output_dimensionality", None)


class _FakeModels:
    def __init__(self, parent):
        self.p = parent

    def embed_content(self, model, contents, config):
        task = _task_of(config)
        dims = _dims_of(config) or self.p.dims
        self.p.calls.append({"model": model, "task_type": task, "n": len(contents)})
        # sequência de erros programada (por task_type)
        if self.p.fail_for and task in self.p.fail_for:
            seq = self.p.fail_for[task]
            if seq:
                code = seq.pop(0)
                raise _FakeAPIError(code)
        # vetores determinísticos POR TEXTO (independentes da posição), para
        # que a ordem de entrada seja verificável na saída.
        embs = []
        for c in contents:
            seed = sum(c.encode("utf-8")) or 1
            vec = [float(((seed + j) % 7) + 1) for j in range(self.p.wrong_dims or dims)]
            embs.append(_FakeEmb(vec))
        return _FakeResp(embs)


class FakeClient:
    def __init__(self, dims=768, fail_for=None, wrong_dims=None):
        self.dims = dims
        self.fail_for = fail_for or {}
        self.wrong_dims = wrong_dims
        self.calls = []
        self.closed = False
        self.models = _FakeModels(self)

    def close(self):
        self.closed = True


def main():
    from rag.app import config
    from rag.app.embeddings import GeminiBackend, EmbeddingError, normalize, status_code_of
    from rag.app import ingest as im
    from rag.app.store import SqliteVectorStore

    # 1. documentos usam RETRIEVAL_DOCUMENT
    fc = FakeClient(dims=768)
    gb = GeminiBackend(client=fc, dims=768, batch_size=32, sleep_fn=lambda s: None)
    docs = gb.embed_documents(["a", "b", "c"])
    check("1) documentos usam RETRIEVAL_DOCUMENT",
          all(c["task_type"] == "RETRIEVAL_DOCUMENT" for c in fc.calls))

    # 2. perguntas usam QUESTION_ANSWERING
    fc2 = FakeClient(dims=768)
    gb2 = GeminiBackend(client=fc2, dims=768, sleep_fn=lambda s: None)
    _ = gb2.embed_query("pergunta")
    check("2) perguntas usam QUESTION_ANSWERING",
          fc2.calls and fc2.calls[-1]["task_type"] == "QUESTION_ANSWERING")

    # 3. fallback RETRIEVAL_QUERY quando QUESTION_ANSWERING falha 400
    fc3 = FakeClient(dims=768, fail_for={"QUESTION_ANSWERING": [400]})
    gb3 = GeminiBackend(client=fc3, dims=768, sleep_fn=lambda s: None)
    v3 = gb3.embed_query("pergunta")
    tasks3 = [c["task_type"] for c in fc3.calls]
    check("3) fallback RETRIEVAL_QUERY apos 400",
          "QUESTION_ANSWERING" in tasks3 and "RETRIEVAL_QUERY" in tasks3 and len(v3) == 768)

    # 4. vetores têm 768 dimensões
    check("4) vetores documento têm 768 dims", all(len(v) == 768 for v in docs))

    # 5. vetores normalizados
    check("5) vetores normalizados (norma ~1)",
          all(abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-9 for v in docs))

    # 6. quantidade de textos == quantidade de vetores
    many = gb.embed_documents([str(i) for i in range(70)])  # > batch_size (32) → 3 lotes
    check("6) n_textos == n_vetores (com batching)", len(many) == 70)

    # 7. ordem preservada (vetores distinguíveis por texto)
    a = gb.embed_documents(["alpha", "beta"])
    b = gb.embed_documents(["beta", "alpha"])
    check("7) ordem preservada", a[0] != a[1] and a[0] == b[1] and a[1] == b[0])

    # 8. erro 404 NÃO é retentado
    fc8 = FakeClient(dims=768, fail_for={"RETRIEVAL_DOCUMENT": [404, 404, 404, 404]})
    gb8 = GeminiBackend(client=fc8, dims=768, max_retries=3, sleep_fn=lambda s: None)
    raised = False
    try:
        gb8.embed_documents(["x"])
    except EmbeddingError:
        raised = True
    check("8) 404 não é retentado (1 chamada, erro)", raised and len(fc8.calls) == 1,
          f"calls={len(fc8.calls)}")

    # 9. erro 429 é retentado de forma limitada
    fc9 = FakeClient(dims=768, fail_for={"RETRIEVAL_DOCUMENT": [429, 429]})  # 2 falhas, depois ok
    gb9 = GeminiBackend(client=fc9, dims=768, max_retries=3, sleep_fn=lambda s: None)
    v9 = gb9.embed_documents(["x"])
    check("9) 429 retentado de forma limitada (3 chamadas, sucesso)",
          len(v9) == 1 and len(fc9.calls) == 3, f"calls={len(fc9.calls)}")

    # 9b. 429 persistente respeita o teto de tentativas
    fc9b = FakeClient(dims=768, fail_for={"RETRIEVAL_DOCUMENT": [429, 429, 429, 429, 429]})
    gb9b = GeminiBackend(client=fc9b, dims=768, max_retries=2, sleep_fn=lambda s: None)
    persisted = False
    try:
        gb9b.embed_documents(["x"])
    except EmbeddingError:
        persisted = True
    check("9b) 429 persistente para após max_retries", persisted and len(fc9b.calls) == 3,
          f"calls={len(fc9b.calls)}")

    # 10. segredo não aparece no erro
    os.environ["GEMINI_API_KEY"] = "SENTINELA_SECRETA_NAO_VAZAR_123456"
    fc10 = FakeClient(dims=768, fail_for={"RETRIEVAL_DOCUMENT": [403]})
    gb10 = GeminiBackend(client=fc10, dims=768, sleep_fn=lambda s: None)
    msg = ""
    try:
        gb10.embed_documents(["x"])
    except EmbeddingError as e:
        msg = str(e)
    check("10) segredo não aparece na mensagem de erro",
          "SENTINELA_SECRETA" not in msg and msg != "")

    # dimensão inesperada falha ANTES do upsert
    fcW = FakeClient(dims=768, wrong_dims=512)
    gbW = GeminiBackend(client=fcW, dims=768, sleep_fn=lambda s: None)
    dim_raised = False
    try:
        gbW.embed_documents(["x"])
    except EmbeddingError:
        dim_raised = True
    check("dim inesperada (512!=768) falha antes do upsert", dim_raised)

    check("status_code_of extrai 429 de texto", status_code_of(Exception("boom 429 retry")) == 429)

    # ---------------- Índice: compatibilidade + rebuild atômico ----------------
    FIX = ROOT / "rag" / "tests" / "fixtures" / "corpus"

    def fresh():
        p = Path(tempfile.mktemp(suffix=".sqlite3"))
        return p

    # 11. índice com modelo antigo é incompatível
    db = fresh()
    s = SqliteVectorStore(db_path=db)
    s.set_meta("embedding_provider", "gemini")
    s.set_meta("embedding_model", "text-embedding-004")  # ANTIGO
    s.set_meta("embedding_dimensions", "768")
    s.set_meta("document_task_type", "RETRIEVAL_DOCUMENT")
    # insere um chunk fantasma do índice antigo
    s.conn.execute("INSERT INTO chunks (chunk_hash,collection,source_path,content_hash,chunk_index,validation_status,file_validation_status,canonical,contains_pending,category,destination,heading_path,title,language,tags,text,embedding,indexed_at) VALUES ('old1',?, 'x.md','h',0,'validado','validado',0,0,'','','','','pt-BR','[]','antigo','[0.1]','t')", (s.collection,))
    s.conn.commit()
    s.close()  # fecha o handle antes do rebuild (Windows exige; POSIX indiferente)
    from rag.app.embeddings import get_backend
    det = get_backend("deterministic")
    compat, reason = im.index_compatible(im._read_meta(db), det.describe())
    check("11) índice text-embedding-004 é incompatível", compat is False, reason)

    # 12 + 15. índice incompatível/parcial → rebuild atômico limpo (não mistura)
    res = im.run_ingest(corpus_dir=FIX, db_path=db, backend_name="deterministic",
                        indexed_at="2026-07-15T00:00:00Z", full=False)
    m = res["metrics"]
    check("12) índice incompatível dispara rebuild", m["incompatible_index_rebuilt"] is True)
    check("12) rebuild marcado como full", m["full_rebuild"] is True)
    s2 = SqliteVectorStore(db_path=db)
    has_old = s2.conn.execute("SELECT COUNT(*) c FROM chunks WHERE chunk_hash='old1'").fetchone()["c"]
    new_model = s2.get_meta("embedding_model")
    s2.close()
    check("15) chunk do índice antigo removido (não mistura)", has_old == 0)
    check("15) metadados atualizados p/ backend novo", new_model == "deterministic-hash-v1")

    # 13. banco temporário NÃO substitui o ativo em falha
    db2 = fresh()
    # ativo válido pré-existente (deterministic)
    im.run_ingest(corpus_dir=FIX, db_path=db2, backend_name="deterministic",
                  indexed_at="t", full=True)
    active_before = SqliteVectorStore(db_path=db2); n_before = active_before.count(); active_before.close()

    class FailingBackend:
        name = "failing"
        def describe(self):
            return {"embedding_provider": "deterministic", "embedding_model": "deterministic-hash-v1",
                    "embedding_dimensions": 256, "document_task_type": "DETERMINISTIC",
                    "query_task_type": "DETERMINISTIC"}
        def embed_documents(self, texts):
            raise RuntimeError("falha simulada no meio do rebuild")
        def embed_query(self, text):
            return [0.0]
        def close(self):
            pass

    from rag.app.chunker import chunk_document
    from rag.app.corpus import build_manifest
    man = build_manifest(FIX, indexed_at="t")
    chunks = []
    for f in man["files"]:
        if f["included_in_rag"]:
            txt = (FIX / f["relative_path"]).read_text(encoding="utf-8")
            chunks.extend(chunk_document(f["relative_path"], txt, f))
    failed = False
    try:
        im._atomic_full_rebuild(db2, chunks, FailingBackend(), FailingBackend().describe(),
                                "bia_context_v1", "hash", "t", {"new_chunks": 0, "updated_chunks": 0,
                                "unchanged_chunks": 0, "orphan_chunks_removed": 0, "dedup_skipped": 0})
    except Exception:
        failed = True
    active_after = SqliteVectorStore(db_path=db2); n_after = active_after.count(); active_after.close()
    check("13) falha no rebuild NÃO substitui o ativo", failed and n_after == n_before,
          f"before={n_before} after={n_after}")
    check("13) sem .tmp deixado para trás",
          not Path(str(db2) + ".rebuild.tmp").exists())

    # 14. troca atômica em sucesso
    db3 = fresh()
    r14 = im.run_ingest(corpus_dir=FIX, db_path=db3, backend_name="deterministic",
                        indexed_at="t", full=True)
    check("14) troca atômica marcada em sucesso", r14["metrics"].get("atomic_swap") is True)
    s3 = SqliteVectorStore(db_path=db3)
    check("14) índice ativo populado após swap", s3.count() > 0)
    check("14) metadados de embedding persistidos",
          s3.get_meta("embedding_model") == "deterministic-hash-v1" and s3.get_meta("corpus_hash"))
    s3.close()

    print()
    if _fails:
        print(f"{len(_fails)} FALHA(S): {_fails}")
        return 1
    print("TODOS OS TESTES DE EMBEDDINGS PASSARAM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
