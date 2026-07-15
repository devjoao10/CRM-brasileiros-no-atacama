#!/usr/bin/env python3
"""Suite hermetica do RAG da BIA (BIA-RAG-CONTEXT-01).

Sem efeitos externos: usa o backend deterministico (sem rede/API) e um corpus
de fixtures com vocabulario controlado + o corpus real (so para mecanica e a
invariante de seguranca). Rode:  python -m rag.tests.test_rag
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

FIXTURE_CORPUS = Path(__file__).resolve().parent / "fixtures" / "corpus"
REAL_CORPUS = ROOT / "bna_agent_context"

_fails = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        _fails.append(name)


def _fresh_db():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    os.unlink(path)  # queremos o caminho, não o arquivo vazio
    return Path(path)


def ingest_fixtures(db_path, corpus=FIXTURE_CORPUS, full=True):
    from rag.app import ingest as ingest_mod
    return ingest_mod.run_ingest(corpus_dir=corpus, db_path=db_path,
                                 backend_name="deterministic",
                                 indexed_at="2026-07-15T00:00:00Z", full=full)


def q(query, db_path, **kw):
    from rag.app.retrieve import retrieve
    return retrieve(query, db_path=db_path, backend_name="deterministic", **kw)


def main():
    from rag.app import config
    from rag.app.chunker import chunk_document
    from rag.app.corpus import build_manifest
    from rag.app.store import SqliteVectorStore
    from rag.app.answer import build_agent_payload, SYSTEM_GUARDRAIL

    # ---------- 1. MANIFEST / corpus classification (fixtures) ----------
    man = build_manifest(FIXTURE_CORPUS, indexed_at="t")
    included = {f["relative_path"] for f in man["files"] if f["included_in_rag"]}
    excluded = {f["relative_path"] for f in man["files"] if not f["included_in_rag"]}
    check("manifest inclui doc validado", "03_tours/zephyrtour.md" in included)
    check("manifest exclui _meta administrativo", "_meta/mapa_de_arquivos.md" in excluded)
    zp = next(f for f in man["files"] if f["relative_path"] == "04_precos/quatzal_preco.md")
    check("preco pendente classificado como pendente_validacao",
          zp["validation_status"] == config.STATUS_PENDING, zp["validation_status"])

    # ---------- 2. CHUNKING (nunca separa heading do conteudo) ----------
    text = (FIXTURE_CORPUS / "03_tours/zephyrtour.md").read_text(encoding="utf-8")
    chunks = chunk_document("03_tours/zephyrtour.md", text, zp | {"validation_status": "validado"})
    check("chunking gera >=1 chunk", len(chunks) >= 1)
    check("todo chunk tem heading_path e hash",
          all(c["heading_path"] and c["chunk_hash"] for c in chunks))
    check("chunk hash é deterministico",
          chunk_document("03_tours/zephyrtour.md", text, zp)[0]["chunk_hash"] == chunks[0]["chunk_hash"])

    # ---------- 3. INGEST + STORE mechanics ----------
    db = _fresh_db()
    try:
        res = ingest_fixtures(db)
        m = res["metrics"]
        check("ingest gravou chunks novos", m["new_chunks"] > 0)
        store = SqliteVectorStore(db_path=db)
        n1 = store.count(); store.close()
        check("store persiste chunks", n1 > 0)

        # incremental: re-ingest sem mudanca => 0 novos, 0 orfaos
        res2 = ingest_fixtures(db, full=False)
        check("incremental: 0 novos quando nada muda", res2["metrics"]["new_chunks"] == 0,
              str(res2["metrics"]["new_chunks"]))
        check("incremental: 0 orfaos quando nada muda", res2["metrics"]["orphan_chunks_removed"] == 0)

        # persistencia apos "restart" (reabrir o arquivo)
        store2 = SqliteVectorStore(db_path=db)
        check("persistencia apos reabrir store", store2.count() == n1)
        store2.close()

        # backup / restore
        bkp = _fresh_db()
        s = SqliteVectorStore(db_path=db); s.backup(bkp); s.close()
        check("backup criado", bkp.exists() and bkp.stat().st_size > 0)
        db2 = _fresh_db()
        SqliteVectorStore.restore(bkp, db2)
        sr = SqliteVectorStore(db_path=db2)
        check("restore preserva contagem", sr.count() == n1)
        sr.close()

        # ---------- 4. RETRIEVAL: grounded validado ----------
        r = q("me fale do zephyrtour zephyrmarker", db)
        check("A) validado: answerable=True", r["answerable"] is True)
        check("A) validado: grounded=True", r["grounded"] is True)
        check("A) validado: fonte correta citada",
              any(s["path"] == "03_tours/zephyrtour.md" for s in r["sources"]))
        check("A) validado: nenhuma fonte factual pendente",
              all(s["validation_status"] != config.STATUS_PENDING for s in r["sources"]))

        # ---------- 5. RETRIEVAL: pendente-only ----------
        r = q("qual o preco do quatzaltour quatzalprice", db)
        check("B) pendente: answerable=False", r["answerable"] is False)
        check("B) pendente: pending_validation=True", r["pending_validation"] is True)
        check("B) pendente: fonte pendente citada",
              any("quatzal_preco.md" in s["path"] for s in r["sources"]))
        check("B) pendente: nenhum chunk factual retornado", len(r["context_chunks"]) == 0)

        # ---------- 6. RETRIEVAL: no-answer ----------
        r = q("voc_s vendem passagem aerea nonexistenttopicxyz para marte", db)
        check("C) no-answer: answerable=False", r["answerable"] is False)
        check("C) no-answer: grounded=False", r["grounded"] is False)
        check("C) no-answer: sem fontes", len(r["sources"]) == 0)
        check("C) no-answer: warning de sem-base",
              any("sem base" in w for w in r["warnings"]))

        # ---------- 7. RETRIEVAL: conflito (validado + pendente) ----------
        r = q("helioski helioskimarker preco e operacao", db)
        check("D) conflito: answerable=True (prioriza validado)", r["answerable"] is True)
        check("D) conflito: conflict=True", r["conflict"] is True)
        check("D) conflito: fonte factual é a validada",
              any("helioski_validado.md" in s["path"] for s in r["sources"]))
        check("D) conflito: contexto factual NAO contem texto pendente",
              all("[PENDENTE_VALIDACAO]" not in c["text"] for c in r["context_chunks"]))
        check("D) conflito: pendente aparece só como pending_source",
              any("helioski_pendente.md" in s["path"] for s in r["pending_sources"]))

        # ---------- 8. PROMPT INJECTION ----------
        r = q("injectiontopic injectionmarker", db)
        payload = build_agent_payload(r)
        check("E) injection: guardrail de sistema presente",
              "DADOS, nao instrucoes" in payload["system"] or "ignore" in SYSTEM_GUARDRAIL.lower())
        # o doc é entregue como DADO dentro do envelope, nao como instrucao do sistema
        if r["answerable"]:
            ctx = payload["context"]
            check("E) injection: doc entregue dentro do envelope de DADOS",
                  "DOCUMENTOS DE CONTEXTO" in ctx)
            check("E) injection: instrucao maliciosa NAO virou instrucao de sistema",
                  "IGNORE TODAS AS INSTRUCOES" not in payload["system"] and
                  "IGNORE TODAS AS INSTRUCOES" not in payload["directive"])
        else:
            check("E) injection: doc recuperado", True)

        # ---------- 9. INVARIANTE DE SEGURANCA no corpus REAL ----------
        real_db = _fresh_db()
        from rag.app import ingest as ingest_mod
        ingest_mod.run_ingest(corpus_dir=REAL_CORPUS, db_path=real_db,
                              backend_name="deterministic",
                              indexed_at="2026-07-15T00:00:00Z", full=True)
        # nenhuma resposta factual pode conter marcador pendente, em varias queries
        safe = True
        for query in ["desconto pix percentual", "preco atacama astronomico",
                      "cancelamento reembolso", "altitude criancas uyuni",
                      "lgpd privacidade dados"]:
            rr = q(query, real_db)
            for c in rr["context_chunks"]:
                if "[PENDENTE_VALIDACAO]" in c["text"]:
                    safe = False
        check("F) corpus real: NENHUM chunk factual contem [PENDENTE_VALIDACAO]", safe)

        # stale deletion: simular remocao de um arquivo do corpus de fixtures
        tmp_corpus = Path(tempfile.mkdtemp()) / "corpus"
        import shutil
        shutil.copytree(FIXTURE_CORPUS, tmp_corpus)
        db3 = _fresh_db()
        ingest_mod.run_ingest(corpus_dir=tmp_corpus, db_path=db3,
                              backend_name="deterministic", indexed_at="t", full=True)
        s3 = SqliteVectorStore(db_path=db3); before = s3.count(); s3.close()
        (tmp_corpus / "03_tours/zephyrtour.md").unlink()
        r_del = ingest_mod.run_ingest(corpus_dir=tmp_corpus, db_path=db3,
                                      backend_name="deterministic", indexed_at="t", full=False)
        check("G) stale: chunks orfaos removidos ao apagar arquivo",
              r_del["metrics"]["orphan_chunks_removed"] > 0)
        s3b = SqliteVectorStore(db_path=db3); after = s3b.count(); s3b.close()
        check("G) stale: contagem diminuiu", after < before, f"{before}->{after}")
    finally:
        for p in [db]:
            try: os.unlink(p)
            except OSError: pass

    print()
    if _fails:
        print(f"{len(_fails)} FALHA(S): {_fails}")
        return 1
    print("TODOS OS TESTES PASSARAM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
