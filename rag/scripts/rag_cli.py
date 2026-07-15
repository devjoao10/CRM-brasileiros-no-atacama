#!/usr/bin/env python3
"""CLI do RAG da BIA. Sem efeitos externos além do vector store local.

Uso:
  python -m rag.scripts.rag_cli ingest-full   [--backend deterministic|gemini]
  python -m rag.scripts.rag_cli ingest        (incremental)
  python -m rag.scripts.rag_cli dry-run
  python -m rag.scripts.rag_cli validate      (varre corpus, sem escrever)
  python -m rag.scripts.rag_cli status
  python -m rag.scripts.rag_cli health
  python -m rag.scripts.rag_cli backup  <dest.sqlite3>
  python -m rag.scripts.rag_cli restore <src.sqlite3>
  python -m rag.scripts.rag_cli search "pergunta"  [--top-k 6] [--include-pending]
  python -m rag.scripts.rag_cli manifest  <out.json>
"""
import argparse
import datetime
import json
import sys
from pathlib import Path

# permite rodar como script direto (sem -m) adicionando a raiz do repo
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rag.app import config, ingest as ingest_mod  # noqa: E402
from rag.app.corpus import build_manifest  # noqa: E402
from rag.app.retrieve import retrieve  # noqa: E402
from rag.app.store import SqliteVectorStore  # noqa: E402
from rag.app.answer import build_agent_payload  # noqa: E402


def _now():
    # timestamp UTC ISO (sem depender de relógio para reprodutibilidade dos hashes,
    # que NÃO usam tempo; só o campo indexed_at recebe o tempo real)
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rag_cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("ingest-full", "ingest", "dry-run", "validate", "status", "health"):
        sub.add_parser(name)
    sp = sub.add_parser("search"); sp.add_argument("query"); sp.add_argument("--top-k", type=int, default=None); sp.add_argument("--include-pending", action="store_true"); sp.add_argument("--payload", action="store_true")
    bp = sub.add_parser("backup"); bp.add_argument("dest")
    rp = sub.add_parser("restore"); rp.add_argument("src")
    mp = sub.add_parser("manifest"); mp.add_argument("out")
    for p in (sub.choices["ingest-full"], sub.choices["ingest"], sub.choices["dry-run"], sub.choices["search"]):
        p.add_argument("--backend", default=None)
    args = ap.parse_args(argv)

    if args.cmd in ("ingest-full", "ingest", "dry-run"):
        res = ingest_mod.run_ingest(
            indexed_at=_now(),
            backend_name=getattr(args, "backend", None),
            dry_run=(args.cmd == "dry-run"),
            full=(args.cmd == "ingest-full"),
        )
        print(json.dumps(res["metrics"], ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "validate":
        man = build_manifest(indexed_at=_now())
        summary = {
            "total_files": man["total_files"],
            "included_files": man["included_files"],
            "excluded_files": man["excluded_files"],
            "by_status": {},
        }
        for f in man["files"]:
            if f["included_in_rag"]:
                summary["by_status"][f["validation_status"]] = summary["by_status"].get(f["validation_status"], 0) + 1
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "status":
        print(json.dumps(ingest_mod.status(), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "health":
        store = SqliteVectorStore()
        ok = store.count() > 0
        out = {"status": "healthy" if ok else "empty", "chunks": store.count(),
               "index_version": store.get_meta("index_version"), "db_path": str(config.DB_PATH.name)}
        store.close()
        print(json.dumps(out, ensure_ascii=False))
        return 0 if ok else 1

    if args.cmd == "backup":
        store = SqliteVectorStore(); dest = store.backup(Path(args.dest)); store.close()
        print(json.dumps({"backup": str(dest)}, ensure_ascii=False)); return 0

    if args.cmd == "restore":
        SqliteVectorStore.restore(Path(args.src), config.DB_PATH)
        print(json.dumps({"restored_from": args.src}, ensure_ascii=False)); return 0

    if args.cmd == "manifest":
        man = build_manifest(indexed_at=_now())
        Path(args.out).write_text(json.dumps(man, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"manifest": args.out, "included": man["included_files"], "excluded": man["excluded_files"]}, ensure_ascii=False))
        return 0

    if args.cmd == "search":
        res = retrieve(args.query, top_k=args.top_k, include_pending=args.include_pending,
                       backend_name=getattr(args, "backend", None))
        if args.payload:
            print(json.dumps(build_agent_payload(res), ensure_ascii=False, indent=2))
        else:
            slim = {k: res[k] for k in ("grounded", "answerable", "pending_validation",
                                        "conflict", "confidence", "warnings")}
            slim["sources"] = res["sources"]
            print(json.dumps(slim, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
