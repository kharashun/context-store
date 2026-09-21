"""Debug and future-automation CLI sharing the library with the MCP tools.

Usage:
    python -m context_store.cli <command> ...

Every command mirrors one MCP tool. Paths come from CONTEXT_DB /
OPENCODE_DB environment variables (same defaults as the server). This CLI
exists so a future scheduler (systemd timer, cron) can run the same code
paths without redesign — no scheduling logic lives here.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import sessions, store
from .config import context_db_path, opencode_db_path
from .db import connect


def _print(result) -> None:
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))


def _fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="context-store-cli",
        description="Local context store CLI (same library as the MCP server)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("save", help="create or overwrite a note")
    p.add_argument("path")
    p.add_argument("--content", required=True, help="text to store")
    p.add_argument("--tags", nargs="*", default=[], help="lowercase topic tags")
    p.add_argument("--kind", default="note", choices=store.KINDS)
    p.add_argument("--title")
    p.add_argument("--source-url")

    p = sub.add_parser("read", help="read a note by exact path")
    p.add_argument("path")

    p = sub.add_parser("search", help="full-text search")
    p.add_argument("query")
    p.add_argument("--kind", choices=store.KINDS)
    p.add_argument("--tag")
    p.add_argument("--limit", type=int, default=5)

    p = sub.add_parser("list", help="browse note paths")
    p.add_argument("--prefix")
    p.add_argument("--kind", choices=store.KINDS)
    p.add_argument("--tag")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("delete", help="permanently delete a note")
    p.add_argument("path")

    sub.add_parser("stats", help="store overview")

    p = sub.add_parser("sessions", help="list OpenCode sessions")
    p.add_argument("--project", help="substring of the session directory")
    p.add_argument("--since", help="ISO date or epoch seconds/ms")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("session-read", help="read one session transcript")
    p.add_argument("session_id")
    p.add_argument("--include-tools", action="store_true")
    p.add_argument("--max-chars", type=int, default=24000)

    return parser


def main(argv: "list[str] | None" = None) -> None:
    args = build_parser().parse_args(argv)

    if args.command == "sessions":
        try:
            _print(sessions.list_sessions(
                opencode_db_path(), project=args.project, since=args.since, limit=args.limit))
        except (sessions.SessionsUnavailableError, ValueError) as e:
            _fail(str(e))
        return

    if args.command == "session-read":
        try:
            _print(sessions.read_session(
                opencode_db_path(), args.session_id,
                include_tools=args.include_tools, max_chars=args.max_chars))
        except (sessions.SessionsUnavailableError, LookupError, ValueError) as e:
            _fail(str(e))
        return

    db = context_db_path()
    conn = connect(db)
    try:
        if args.command == "save":
            try:
                _print(store.save_note(
                    conn, args.path, args.content, tags=args.tags,
                    kind=args.kind, title=args.title, source_url=args.source_url))
            except store.StoreError as e:
                _fail(str(e))
        elif args.command == "read":
            note = store.read_note(conn, args.path)
            if note is None:
                _fail(f"no note at path {args.path!r}")
            _print(note)
        elif args.command == "search":
            try:
                _print(store.search_notes(
                    conn, args.query, kind=args.kind, tag=args.tag, limit=args.limit))
            except store.StoreError as e:
                _fail(str(e))
        elif args.command == "list":
            try:
                _print(store.list_notes(
                    conn, prefix=args.prefix, kind=args.kind, tag=args.tag, limit=args.limit))
            except store.StoreError as e:
                _fail(str(e))
        elif args.command == "delete":
            result = store.delete_note(conn, args.path)
            if result is None:
                _fail(f"no note at path {args.path!r}")
            _print(result)
        elif args.command == "stats":
            _print(store.stats(conn, db))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
