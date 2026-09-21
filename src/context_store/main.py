"""ContextStore MCP server (stdio).

Local persistent memory for AI agents: hierarchical notes in SQLite with
BM25 full-text search, plus read-only access to OpenCode sessions.
Everything is model-initiated — there are no background jobs.
"""

from __future__ import annotations

from typing import Literal

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError

from . import sessions, store
from .config import context_db_path, opencode_db_path
from .db import connect

mcp = FastMCP(name="context-store")


def _origin(ctx: Context) -> tuple[str | None, str | None]:
    """Best-effort (session_id, project_directory) for the current tool call.

    OpenCode passes the invoking session in CallToolRequest.params._meta.sessionID.
    """
    try:
        rc = ctx.request_context
        session_id = ((rc.meta if rc is not None else None) or {}).get("sessionID")
    except Exception:
        session_id = None
    if not session_id:
        return None, None
    info = sessions.get_session_info(opencode_db_path(), session_id)
    return session_id, (info or {}).get("directory")


PATH_CONVENTIONS = """\
Path conventions (slash-separated; no spaces — use '-'):
  sessions/<project>/<yyyy-mm-dd>-<slug>   session summaries
  decisions/<project>/<topic>              what was chosen and why
  docs/<library>/<topic>                   fetched API docs (set source_url)
  notes/<project>/<topic>                  general project knowledge
  howto/<task>                             reusable procedures / workarounds\
"""


@mcp.tool
def save(
    path: str,
    content: str,
    ctx: Context,
    tags: "list[str] | None" = None,
    kind: Literal["note", "doc", "session-summary", "decision", "howto"] = "note",
    title: str | None = None,
    source_url: str | None = None,
) -> dict:
    """Store durable knowledge at a hierarchical path. Upsert: saving to an
    existing path overwrites it. Use whenever the user asks to keep, remember,
    or store something — session summaries, decisions, fetched API docs,
    workarounds, project knowledge.

    {conventions}

    Args:
        path: hierarchical path, e.g. "docs/fastapi/middleware"
        content: text to store (markdown welcome)
        tags: optional lowercase topic tags, e.g. ["auth", "architecture"]
        kind: note | doc | session-summary | decision | howto
        title: optional human-readable title
        source_url: URL the content was fetched from (for docs)

    Returns:
        {{"status": "created"|"updated", "path", "updated_at"}}. Retrieve
        later with `read` (exact path) or `search` (keyword).
    """.format(conventions=PATH_CONVENTIONS)
    origin_session, origin_project = _origin(ctx)
    conn = connect(context_db_path())
    try:
        return store.save_note(
            conn,
            path,
            content,
            tags=tags,
            kind=kind,
            title=title,
            source_url=source_url,
            origin_session=origin_session,
            origin_project=origin_project,
        )
    except store.StoreError as e:
        raise ToolError(str(e)) from e
    finally:
        conn.close()


@mcp.tool
def read(path: str) -> dict:
    """Retrieve a note by its exact path (as previously given to `save`).

    Returns:
        {path, content, title, tags, kind, source_url, origin_session,
        origin_project, created_at, updated_at}.
    """
    conn = connect(context_db_path())
    try:
        note = store.read_note(conn, path)
    except store.StoreError as e:
        raise ToolError(str(e)) from e
    finally:
        conn.close()
    if note is None:
        raise ToolError(f"no note at path {path!r} — use `list` or `search` to find stored paths")
    return note


@mcp.tool
def search(query: str, kind: str | None = None, tag: str | None = None, limit: int = 5) -> list[dict]:
    """Keyword search over stored notes (SQLite FTS5, BM25-ranked). Lower
    score = better match.

    Supports FTS5 syntax: plain terms, "quoted phrases", OR, and prefix
    matching with term* — e.g. `fastapi* middleware` or `"connection pool"`.

    Args:
        query: search expression, e.g. "auth middleware decision"
        kind: optional filter: note | doc | session-summary | decision | howto
        tag: optional exact tag filter, e.g. "auth"
        limit: max results (default 5)

    Returns:
        [{path, title, kind, updated_at, snippet, score}] — snippet shows
        matched terms wrapped in **.
    """
    conn = connect(context_db_path())
    try:
        return store.search_notes(conn, query, kind=kind, tag=tag, limit=limit)
    except store.StoreError as e:
        raise ToolError(str(e)) from e
    finally:
        conn.close()


@mcp.tool(name="list")
def list_paths(
    prefix: str | None = None,
    kind: str | None = None,
    tag: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Browse stored note paths (ordered by path). Call this before saving to
    discover what is already stored and avoid divergent or duplicate paths.

    Args:
        prefix: optional path prefix, e.g. "docs/fastapi"
        kind: optional filter: note | doc | session-summary | decision | howto
        tag: optional exact tag filter
        limit: max rows (default 50)

    Returns:
        [{path, title, kind, tags, updated_at}]
    """
    conn = connect(context_db_path())
    try:
        return store.list_notes(conn, prefix=prefix, kind=kind, tag=tag, limit=limit)
    except store.StoreError as e:
        raise ToolError(str(e)) from e
    finally:
        conn.close()


@mcp.tool
def delete(path: str) -> dict:
    """Permanently delete the note at `path`. Use when stored knowledge is
    outdated or wrong — stale memory is worse than none.

    Returns:
        A summary of what was deleted ({deleted, path, kind, title, tags,
        content_preview, created_at, updated_at}) so the caller can confirm
        it removed the right note.
    """
    conn = connect(context_db_path())
    try:
        result = store.delete_note(conn, path)
    except store.StoreError as e:
        raise ToolError(str(e)) from e
    finally:
        conn.close()
    if result is None:
        raise ToolError(f"no note at path {path!r} — nothing deleted")
    return result


@mcp.tool
def stats() -> dict:
    """Overview of the context store: total notes, counts by kind, top tags,
    10 most recent saves, and database size. Useful at the start of a session
    to decide what to search for."""
    db_path = context_db_path()
    conn = connect(db_path)
    try:
        return store.stats(conn, db_path)
    finally:
        conn.close()


@mcp.tool
def sessions_list(
    project: str | None = None,
    since: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """List OpenCode sessions (newest first), read-only. Use before
    `session_read` to find a session id.

    Args:
        project: optional substring of the session's working directory, e.g. "context-store"
        since: optional cutoff — ISO date ("2026-09-01") or epoch seconds/ms
        limit: max sessions (default 20)

    Returns:
        [{session_id, title, directory, created, updated, message_count}]
    """
    try:
        return sessions.list_sessions(
            opencode_db_path(), project=project, since=since, limit=limit
        )
    except sessions.SessionsUnavailableError as e:
        raise ToolError(str(e)) from e
    except ValueError as e:
        raise ToolError(str(e)) from e


@mcp.tool
def session_read(
    session_id: str,
    include_tools: bool = False,
    max_chars: int = 24000,
) -> dict:
    """Read a past OpenCode session as a compact transcript (read-only).
    Use for requests like "summarize session X": read the transcript,
    summarize it, then persist the summary with `save`
    (path "sessions/<project>/<yyyy-mm-dd>-<slug>", kind "session-summary").

    Includes user/assistant text; skips reasoning, system/synthetic/idle
    messages, and other non-conversational rows; tool calls become one-liners
    only when include_tools is true.

    Args:
        session_id: session id from `sessions_list`
        include_tools: include "[tool: name (status)]" one-liners
        max_chars: transcript cap (default 24000)

    Returns:
        {session_id, title, directory, created, updated, message_count,
        transcript, transcript_chars, stats}
    """
    try:
        return sessions.read_session(
            opencode_db_path(),
            session_id,
            include_tools=include_tools,
            max_chars=max_chars,
        )
    except sessions.SessionsUnavailableError as e:
        raise ToolError(str(e)) from e
    except (LookupError, ValueError) as e:
        raise ToolError(str(e)) from e


@mcp.prompt
def summarize_session(session_id: str | None = None) -> str:
    """Summarize a session and persist it to the context store."""
    if session_id:
        target = f"session {session_id} (first call `session_read` to get its transcript)"
    else:
        target = "the current conversation"
    return f"""Summarize {target} and persist it to the context store.

1. Determine the project name from the working directory of the session.
2. Write a compact summary covering: the goal, key decisions AND the
   reasoning behind them, important fixes or workarounds, and any
   project-specific configuration discovered.
3. Check existing summaries with `list` (prefix "sessions/<project>/");
   overwrite only if this session supersedes an earlier one, otherwise
   pick a distinct slug.
4. Call `save` with path "sessions/<project>/<yyyy-mm-dd>-<short-slug>",
   kind "session-summary", the summary as markdown content, and a few
   lowercase topic tags.
5. Reply with the saved path."""


def main() -> None:
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
