"""Read-only access to the OpenCode session database.

Current OpenCode storage model (verified against a live database):
- `session_v2`: sessions (id, title, directory, time_created, time_updated, ...)
  Legacy rows from the old `session` table are migrated here too.
- `session_message`: messages with `type` ('user' | 'assistant' | 'system' |
  'synthetic' | 'idle' | 'compaction' | ...), a per-session `seq`, and a JSON
  `data` payload. User messages carry `text`; assistant messages carry a
  `content` array of items: {type: 'text'|'reasoning'|'tool', ...} where tool
  items use `name` for the tool name.

Timestamps in that database are epoch milliseconds. We open fresh read-only
(`mode=ro`) connections per call and never write to it.
"""

from __future__ import annotations

import json
import re
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

PER_PART_CHAR_CAP = 2000
KEPT_MESSAGE_TYPES = ("user", "assistant")
DEFAULT_SESSIONS_LIMIT = 20
MAX_SESSIONS_LIMIT = 100
DEFAULT_MAX_CHARS = 24_000
MAX_TRANSCRIPT_CHARS = 100_000


class SessionsUnavailableError(RuntimeError):
    """The OpenCode database is missing, unreadable, or of an unsupported schema."""


def connect_ro(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path).expanduser()
    if not path.exists():
        raise SessionsUnavailableError(f"OpenCode database not found: {path}")
    uri = "file:" + urllib.parse.quote(str(path)) + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error as e:
        raise SessionsUnavailableError(f"cannot open {path}: {e}") from e
    conn.row_factory = sqlite3.Row
    return conn


def _ms_to_iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return None


def _parse_since(since: str) -> int:
    """Accept 'YYYY-MM-DD', ISO datetime, or epoch seconds/ms; return epoch ms."""
    text = str(since).strip()
    if re.fullmatch(r"\d{10,14}", text):
        value = int(text)
        return value if value > 10**12 else value * 1000
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(
            f"cannot parse since={since!r} — use an ISO date like '2026-09-01'"
        ) from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _list_sessions_query(conn: sqlite3.Connection, filters: list[str], params: list) -> list[sqlite3.Row]:
    sql = f"""
        SELECT s.id            AS session_id,
               s.title         AS title,
               s.directory     AS directory,
               s.time_created  AS time_created,
               s.time_updated  AS time_updated,
               (SELECT COUNT(*) FROM session_message m WHERE m.session_id = s.id) AS message_count
          FROM session_v2 s
         WHERE {' AND '.join(filters)}
         ORDER BY s.time_created DESC, s.id
         LIMIT ?
    """
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        raise SessionsUnavailableError(f"unsupported OpenCode schema: {e}") from e


def list_sessions(
    db_path: str | Path,
    *,
    project: str | None = None,
    since: str | None = None,
    limit: int = DEFAULT_SESSIONS_LIMIT,
) -> list[dict]:
    """List OpenCode sessions, newest first (read-only)."""
    limit = max(1, min(int(limit), MAX_SESSIONS_LIMIT))
    filters, params = ["1=1"], []
    if project:
        filters.append("s.directory LIKE ? ESCAPE '\\'")
        params.append("%" + _escape_like(str(project)) + "%")
    if since:
        filters.append("s.time_created >= ?")
        params.append(_parse_since(since))
    params.append(limit)

    conn = connect_ro(db_path)
    try:
        rows = _list_sessions_query(conn, filters, params)
    finally:
        conn.close()
    return [
        {
            "session_id": r["session_id"],
            "title": r["title"] or "",
            "directory": r["directory"],
            "created": _ms_to_iso(r["time_created"]),
            "updated": _ms_to_iso(r["time_updated"]),
            "message_count": r["message_count"],
        }
        for r in rows
    ]


def _cap_text(text: str, skipped: dict[str, int]) -> str:
    if len(text) > PER_PART_CHAR_CAP:
        skipped["parts_truncated"] = skipped.get("parts_truncated", 0) + 1
        return text[:PER_PART_CHAR_CAP] + " …[part truncated]"
    return text


def _message_chunks(
    message_type: str,
    data: dict,
    *,
    include_tools: bool,
    skipped: dict[str, int],
) -> tuple[str, list[str]]:
    """Turn one session_message row into (role, text chunks)."""
    if message_type == "user":
        text = str(data.get("text") or "").strip()
        return "user", [_cap_text(text, skipped)] if text else []

    chunks: list[str] = []
    for item in data.get("content") or []:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            chunks.append(_cap_text(text, skipped))
        elif item_type == "tool":
            if include_tools:
                name = item.get("name") or item.get("tool") or "?"
                state = item.get("state") or {}
                chunks.append(f"[tool: {name} ({state.get('status', '?')})]")
            else:
                skipped["tool"] = skipped.get("tool", 0) + 1
        elif item_type == "reasoning":
            skipped["reasoning"] = skipped.get("reasoning", 0) + 1
        else:
            skipped[str(item_type or "unknown")] = skipped.get(str(item_type or "unknown"), 0) + 1
    return "assistant", chunks


def read_session(
    db_path: str | Path,
    session_id: str,
    *,
    include_tools: bool = False,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict:
    """Read one session as a compact transcript (read-only).

    Includes user and assistant text; tool calls become one-liners only when
    include_tools is true. Skips reasoning, system/synthetic/idle/compaction
    and other non-conversational messages. Truncates long parts and the whole
    transcript to max_chars.
    """
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("session_id must be a non-empty string")
    max_chars = max(1000, min(int(max_chars), MAX_TRANSCRIPT_CHARS))

    conn = connect_ro(db_path)
    try:
        try:
            sess = conn.execute(
                "SELECT id, title, directory, time_created, time_updated"
                " FROM session_v2 WHERE id = ?",
                (session_id,),
            ).fetchone()
            if sess is None:
                raise LookupError(f"session {session_id!r} not found")
            messages = conn.execute(
                "SELECT type, data FROM session_message"
                " WHERE session_id = ? ORDER BY seq, time_created, id",
                (session_id,),
            ).fetchall()
        except sqlite3.OperationalError as e:
            raise SessionsUnavailableError(f"unsupported OpenCode schema: {e}") from e
    finally:
        conn.close()

    blocks: list[str] = []
    skipped: dict[str, int] = {}
    omitted_messages = 0
    used = 0
    for index, message in enumerate(messages):
        mtype = message["type"]
        if mtype not in KEPT_MESSAGE_TYPES:
            skipped[mtype] = skipped.get(mtype, 0) + 1
            continue
        try:
            data = json.loads(message["data"])
        except (json.JSONDecodeError, TypeError):
            skipped["unparsable"] = skipped.get("unparsable", 0) + 1
            continue
        role, chunks = _message_chunks(mtype, data, include_tools=include_tools, skipped=skipped)
        if not chunks:
            continue
        block = f"## {role}\n\n" + "\n\n".join(chunks)
        if used + len(block) + 2 > max_chars:
            marker = "\n\n…[transcript truncated]"
            room = max_chars - used - len(marker)
            if room >= 200:
                blocks.append(block[:room] + marker)
                omitted_messages = len(messages) - index - 1
            else:
                omitted_messages = len(messages) - index
            break
        blocks.append(block)
        used += len(block) + 2

    transcript = "\n\n".join(blocks)
    parts_truncated = skipped.pop("parts_truncated", 0)
    return {
        "session_id": sess["id"],
        "title": sess["title"] or "",
        "directory": sess["directory"],
        "created": _ms_to_iso(sess["time_created"]),
        "updated": _ms_to_iso(sess["time_updated"]),
        "message_count": len(messages),
        "transcript": transcript,
        "transcript_chars": len(transcript),
        "stats": {
            "parts_truncated": parts_truncated,
            "messages_omitted": omitted_messages,
            "skipped": skipped,
            "tools_included": bool(include_tools),
        },
    }


def get_session_info(db_path: str | Path, session_id: str) -> dict | None:
    """Best-effort session metadata for provenance; None on any failure."""
    try:
        conn = connect_ro(db_path)
        try:
            row = conn.execute(
                "SELECT directory FROM session_v2 WHERE id = ?", (session_id,)
            ).fetchone()
        finally:
            conn.close()
    except (SessionsUnavailableError, sqlite3.Error, OSError):
        return None
    if row is None:
        return None
    return {"directory": row["directory"]}
