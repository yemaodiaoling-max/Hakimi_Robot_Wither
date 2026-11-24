"""Explicit, idempotent repair for Gemini-manual scores lost by blank notes.

This module is safe to import: it opens and writes nothing until called.  The
canonical implementation lives in :func:`memory_social.repair_manual_scores`
and only changes ``social_assessments`` projection values plus their
``affinity_policy`` metadata.  Immutable note text/hashes are never rewritten.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from hakimi.social.social_repair import repair_manual_scores as _repair_manual_scores


def repair_manual_scores(target, *, now=None, dry_run=False):
    """Run the repair against an open DB connection, or an explicit base path.

    ``target`` is normally a live ``sqlite3.Connection`` from ``store.db()``;
    accepting a base directory/file keeps the module usable as a one-off script.
    """
    if hasattr(target, "execute"):
        return _repair_manual_scores(target, now=now, dry_run=dry_run)
    from hakimi.memory.memory_store import store

    base = Path(target).resolve()
    index = store(str(base))
    with index.db() as db:
        return _repair_manual_scores(db, now=now, dry_run=dry_run)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="一次性修复未评分新笔记覆盖旧评分的问题（不调用模型，不改原文）"
    )
    parser.add_argument("--base", required=True, help="记忆库所在目录或 sqlite 文件")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写数据库")
    args = parser.parse_args(argv)
    result = repair_manual_scores(args.base, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
