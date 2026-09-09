"""CLI: python -m closeout.cli run --register samples/register/register.csv --batch samples/evidence/batch-01"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

from .config import SETTINGS
from .pipeline import continue_run, import_register, open_store, process_batch


def _print_progress(event: str, data: dict) -> None:
    print(f"[{event}] {json.dumps(data, default=str)}", flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="closeout")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="import a register (optional) and process a batch folder")
    r.add_argument("--project", required=True, help="project slug; created empty if it does not exist yet")
    r.add_argument("--register", type=Path)
    r.add_argument("--batch", type=Path, required=True)
    r.add_argument("--label", default=None)
    r.add_argument("--reprocess-all", action="store_true", help="re-run the agent on files already processed")
    c = sub.add_parser("retry", help="re-run failed jobs of a run and rebuild its packet")
    c.add_argument("run_id")
    sub.add_parser("status", help="show runs and jobs")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)

    store = open_store(SETTINGS)
    if args.cmd == "run":
        prj = store.project_by_slug(args.project)
        pid = prj["id"] if prj else store.upsert_project(args.project, args.project, "")
        if args.register:
            items = import_register(store, pid, args.register)
            print(f"[register] imported {len(items)} items")
        files = sorted((p for p in args.batch.rglob("*")
                        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(args.batch).parts)),
                       key=lambda p: (str(p.parent.relative_to(args.batch)).lower(),
                                      re.sub(r" \(\d+\)$", "", p.stem).lower(), len(p.name), p.name))
        res = process_batch(store, pid, files, args.label or args.batch.name, SETTINGS, progress=_print_progress,
                            reprocess_all=args.reprocess_all, root=args.batch)
        print(json.dumps(res, indent=2))
        return 0 if res["status"] == "done" else 1
    if args.cmd == "retry":
        res = continue_run(store, args.run_id, SETTINGS, progress=_print_progress)
        print(json.dumps(res, indent=2))
        return 0 if res["status"] == "done" else 1
    if args.cmd == "status":
        for run in store.runs():
            print(run["id"], run["status"], run["started_at"], run.get("usage_json"))
            for j in store.jobs(run["id"]):
                print("   ", j["id"], j["kind"], j["subject"], j["status"], j.get("error") or "")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
