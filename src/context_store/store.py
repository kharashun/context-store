"""Core note-storage operations.

All notes live in one SQLite database with an FTS5 full-text index kept in
sync by triggers (see db.py). `path` is the primary key and saves are
upserts: re-saving an existing path overwrites it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import db

KINDS = ("note", "doc", "session-summary", "decision", "howto")

MAX_PATH_LEN = 512
DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 50
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 500
MAX_BULK_DELETE = 200
SINGLE_PREVIEW_CHARS = 200
BULK_PREVIEW_CHARS = 120


class StoreError(ValueError):
    """Invalid input to a store operation (bad path, kind, query, ...)."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(int(value), hi))


def normalize_path(path: str) -> str:
    """Validate and canonicalize a hierarchical path like 'docs/fastapi/middleware'."""
    if not isinstance(path, str):
        raise StoreError("path must be a string")
    path = path.strip()
    if "\\" in path:
        raise StoreError("backslashes are not allowed; use '/' as the separator")
    path = re.sub(r"/{2,}", "/", path.strip("/"))
    if not path:
        raise StoreError("path must contain at least one segment, e.g. 'notes/project/topic'")
    if len(path) > MAX_PATH_LEN:
        raise StoreError(f"path must be at most {MAX_PATH_LEN} characters")
    for segment in path.split("/"):
        if segment in (".", ".."):
            raise StoreError("'.' and '..' path segments are not allowed")
        if not segment.strip():
            raise StoreError("empty path segment (double slash or trailing slash)")
        if any(ch.isspace() for ch in segment):
            raise StoreError(f"whitespace in path segment {segment!r}; use '-' instead")
    return path


def _normalize_tags(tags: "list[str] | str | None") -> str:
    if tags is None:
        return "[]"
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(tags, (list, tuple)):
        raise StoreError("tags must be a list of strings")
    cleaned: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            raise StoreError("tags must be strings")
        tag = tag.strip().lower()
        if tag and tag not in cleaned:
            cleaned.append(tag)
    return json.dumps(cleaned)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_prefix(prefix: str) -> str:
    """Validate and canonicalize a bulk-delete prefix (same shape as `list_notes`)."""
    if not isinstance(prefix, str) or not prefix.strip():
        raise StoreError("prefix must be a non-empty string, e.g. 'sessions/old-project/'")
    normalized = re.sub(r"/{2,}", "/", prefix.strip().strip("/"))
    if not normalized:
        raise StoreError("prefix must contain at least one segment, e.g. 'sessions/old-project/'")
    return normalized


def row_to_note(row: sqlite3.Row) -> dict:
    note = dict(row)
    note["tags"] = json.loads(note.get("tags") or "[]")
    return note


def save_note(
    conn: sqlite3.Connection,
    path: str,
    content: str,
    *,
    tags: "list[str] | None" = None,
    kind: str = "note",
    title: str | None = None,
    source_url: str | None = None,
    origin_session: str | None = None,
    origin_project: str | None = None,
) -> dict:
    """Create or overwrite (upsert) the note at `path`."""
    path = normalize_path(path)
    if kind not in KINDS:
        raise StoreError(f"kind must be one of {KINDS}, got {kind!r}")
    if not isinstance(content, str) or not content.strip():
        raise StoreError("content must be a non-empty string")
    for name, value in (("title", title), ("source_url", source_url),
                        ("origin_session", origin_session), ("origin_project", origin_project)):
        if value is not None and not isinstance(value, str):
            raise StoreError(f"{name} must be a string or null")

    now = utcnow()
    tags_json = _normalize_tags(tags)
    previous = conn.execute("SELECT updated_at FROM notes WHERE path = ?", (path,)).fetchone()
    if previous is None:
        conn.execute(
            """INSERT INTO notes
                 (path, content, title, tags, kind, source_url,
                  origin_session, origin_project, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (path, content, title, tags_json, kind, source_url,
             origin_session, origin_project, now, now),
        )
        conn.commit()
        return {"status": "created", "path": path, "updated_at": now}

    conn.execute(
        """UPDATE notes SET
             content = ?, title = ?, tags = ?, kind = ?, source_url = ?,
             origin_session = COALESCE(?, origin_session),
             origin_project = COALESCE(?, origin_project),
             updated_at = ?
           WHERE path = ?""",
        (content, title, tags_json, kind, source_url,
         origin_session, origin_project, now, path),
    )
    conn.commit()
    return {
        "status": "updated",
        "path": path,
        "updated_at": now,
        "previous_updated_at": previous["updated_at"],
    }


def read_note(conn: sqlite3.Connection, path: str) -> dict | None:
    """Return the note at `path` or None."""
    path = normalize_path(path)
    row = conn.execute("SELECT * FROM notes WHERE path = ?", (path,)).fetchone()
    return row_to_note(row) if row is not None else None


def _quoted_match(query: str) -> str | None:
    """An always-valid FTS5 fallback: every token as a quoted phrase."""
    tokens = []
    for raw in query.split():
        token = re.sub(r"[^\w-]", "", raw, flags=re.UNICODE)
        if token:
            tokens.append(f'"{token}"')
    return " ".join(tokens) if tokens else None


def search_notes(
    conn: sqlite3.Connection,
    query: str,
    *,
    kind: str | None = None,
    tag: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> list[dict]:
    """BM25-ranked full-text search over notes.

    Supports FTS5 syntax (quoted phrases, OR, prefix `term*`). If the raw
    query is not valid FTS5 syntax, retries with every token quoted.
    """
    if not isinstance(query, str) or not query.strip():
        raise StoreError("query must be a non-empty string")
    if kind is not None and kind not in KINDS:
        raise StoreError(f"kind must be one of {KINDS}, got {kind!r}")
    limit = _clamp(limit, 1, MAX_SEARCH_LIMIT)

    filters, params = [], []
    if kind:
        filters.append("notes.kind = ?")
        params.append(kind)
    if tag:
        if not isinstance(tag, str) or not tag.strip():
            raise StoreError("tag must be a non-empty string")
        filters.append("EXISTS (SELECT 1 FROM json_each(notes.tags) je WHERE je.value = ?)")
        params.append(tag.strip().lower())

    where = " AND ".join(["notes_fts MATCH ?"] + filters)
    sql = f"""
        SELECT notes.path       AS path,
               notes.title      AS title,
               notes.kind       AS kind,
               notes.updated_at AS updated_at,
               snippet(notes_fts, 2, '**', '**', ' … ', 16) AS snippet,
               bm25(notes_fts)  AS score
          FROM notes_fts
          JOIN notes ON notes.rowid = notes_fts.rowid
         WHERE {where}
         ORDER BY bm25(notes_fts)
         LIMIT ?
    """

    candidates = [query.strip()]
    fallback = _quoted_match(query)
    if fallback and fallback != candidates[0]:
        candidates.append(fallback)

    last_error: Exception | None = None
    for match in candidates:
        try:
            rows = conn.execute(sql, [match, *params, limit]).fetchall()
        except sqlite3.OperationalError as e:
            message = str(e).lower()
            if any(
                marker in message
                for marker in ("syntax", "unterminated", "match", "fts5")
            ):
                last_error = e
                continue
            raise
        return [
            {
                "path": r["path"],
                "title": r["title"],
                "kind": r["kind"],
                "updated_at": r["updated_at"],
                "snippet": r["snippet"],
                "score": round(r["score"], 6),
            }
            for r in rows
        ]
    raise StoreError(f"could not parse search query {query!r}: {last_error}")


def list_notes(
    conn: sqlite3.Connection,
    *,
    prefix: str | None = None,
    kind: str | None = None,
    tag: str | None = None,
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[dict]:
    """Browse stored note paths (ordered by path)."""
    if kind is not None and kind not in KINDS:
        raise StoreError(f"kind must be one of {KINDS}, got {kind!r}")
    limit = _clamp(limit, 1, MAX_LIST_LIMIT)

    filters, params = [], []
    if prefix is not None and str(prefix).strip():
        prefix = re.sub(r"/{2,}", "/", str(prefix).strip().strip("/"))
        if prefix:
            filters.append("path LIKE ? ESCAPE '\\'")
            params.append(_escape_like(prefix) + "%")
    if kind:
        filters.append("kind = ?")
        params.append(kind)
    if tag:
        if not isinstance(tag, str) or not tag.strip():
            raise StoreError("tag must be a non-empty string")
        filters.append("EXISTS (SELECT 1 FROM json_each(notes.tags) je WHERE je.value = ?)")
        params.append(tag.strip().lower())

    sql = "SELECT path, title, kind, tags, updated_at FROM notes"
    if filters:
        sql += " WHERE " + " AND ".join(filters)
    sql += " ORDER BY path LIMIT ?"
    rows = conn.execute(sql, [*params, limit]).fetchall()
    return [dict(r, tags=json.loads(r["tags"] or "[]")) for r in rows]


def _note_delete_summary(note: dict, *, preview_chars: int) -> dict:
    """Shared note summary for delete responses (single and bulk)."""
    content = note["content"]
    preview = content[:preview_chars] + ("…" if len(content) > preview_chars else "")
    return {
        "path": note["path"],
        "kind": note["kind"],
        "title": note["title"],
        "tags": note["tags"],
        "content_preview": preview,
        "created_at": note["created_at"],
        "updated_at": note["updated_at"],
    }


def delete_note(conn: sqlite3.Connection, path: str) -> dict | None:
    """Permanently delete the note at `path`; return a summary of what was deleted."""
    path = normalize_path(path)
    row = conn.execute("SELECT * FROM notes WHERE path = ?", (path,)).fetchone()
    if row is None:
        return None
    conn.execute("DELETE FROM notes WHERE path = ?", (path,))
    conn.commit()
    summary = _note_delete_summary(row_to_note(row), preview_chars=SINGLE_PREVIEW_CHARS)
    return {"deleted": True, **summary}


def delete_notes(
    conn: sqlite3.Connection,
    db_path: str | Path,
    *,
    prefix: str,
    expect: int,
) -> dict:
    """Delete ALL notes under `prefix` after safety checks + snapshot.

    Guards, all evaluated before any write:
    - `prefix` is normalized like `list_notes` (string-prefix match)
    - the match count must be non-zero and at most MAX_BULK_DELETE
    - `expect` must equal the match count (stale-listing guard — the caller
      must have run `list` with the same prefix)
    - a `VACUUM INTO` snapshot is taken first; if it fails, nothing is
      deleted (fail-closed)
    """
    if isinstance(expect, bool) or not isinstance(expect, int) or expect < 0:
        raise StoreError("expect must be a non-negative integer")
    prefix = _normalize_prefix(prefix)
    pattern = _escape_like(prefix) + "%"
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM notes WHERE path LIKE ? ESCAPE '\\'", (pattern,)
    ).fetchone()["c"]
    if count == 0:
        raise StoreError(f"no notes under prefix {prefix!r} — nothing deleted")
    if count > MAX_BULK_DELETE:
        raise StoreError(
            f"{count} notes under prefix {prefix!r} exceed the bulk cap of"
            f" {MAX_BULK_DELETE} — narrow the prefix or delete in batches"
        )
    if count != expect:
        raise StoreError(
            f"expect={expect} but {count} notes match prefix {prefix!r} —"
            " re-run `list` with this prefix and pass the current row count"
        )
    try:
        backup = db.snapshot(conn, db_path)
    except Exception as e:
        raise StoreError(f"snapshot failed, bulk delete refused: {e}") from e
    rows = conn.execute(
        "SELECT * FROM notes WHERE path LIKE ? ESCAPE '\\'", (pattern,)
    ).fetchall()
    conn.execute("DELETE FROM notes WHERE path LIKE ? ESCAPE '\\'", (pattern,))
    conn.commit()
    return {
        "deleted": True,
        "prefix": prefix,
        "count": count,
        "backup": str(backup),
        "recovery": "copy the backup file over the store db to restore",
        "notes": [
            _note_delete_summary(row_to_note(row), preview_chars=BULK_PREVIEW_CHARS)
            for row in rows
        ],
    }


def stats(conn: sqlite3.Connection, db_path: str | Path) -> dict:
    """Overview: totals by kind, top tags, recent saves, database size."""
    path = Path(db_path).expanduser()
    total = conn.execute("SELECT COUNT(*) AS c FROM notes").fetchone()["c"]
    by_kind = {
        r["kind"]: r["c"]
        for r in conn.execute("SELECT kind, COUNT(*) AS c FROM notes GROUP BY kind ORDER BY c DESC")
    }
    top_tags = [
        {"tag": r["tag"], "count": r["c"]}
        for r in conn.execute(
            """SELECT je.value AS tag, COUNT(*) AS c
                 FROM notes, json_each(notes.tags) je
                GROUP BY je.value ORDER BY c DESC, tag LIMIT 20"""
        )
    ]
    recent = [
        dict(r)
        for r in conn.execute(
            "SELECT path, title, kind, updated_at FROM notes ORDER BY updated_at DESC, path LIMIT 10"
        )
    ]
    size = path.stat().st_size if path.exists() else 0
    wal = Path(str(path) + "-wal")
    size += wal.stat().st_size if wal.exists() else 0
    return {
        "total_notes": total,
        "by_kind": by_kind,
        "top_tags": top_tags,
        "recent_saves": recent,
        "db_size_bytes": size,
    }
