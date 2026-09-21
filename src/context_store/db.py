"""SQLite setup and schema for the context store.

One database file, WAL mode, with an external-content FTS5 index kept in
sync with the `notes` table by triggers.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
  path           TEXT PRIMARY KEY,
  content        TEXT NOT NULL,
  title          TEXT,
  tags           TEXT NOT NULL DEFAULT '[]',
  kind           TEXT NOT NULL DEFAULT 'note',
  source_url     TEXT,
  origin_session TEXT,
  origin_project TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  path, title, content, tags,
  content='notes', content_rowid='rowid',
  tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid, path, title, content, tags)
  VALUES (new.rowid, new.path, new.title, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, path, title, content, tags)
  VALUES ('delete', old.rowid, old.path, old.title, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, path, title, content, tags)
  VALUES ('delete', old.rowid, old.path, old.title, old.content, old.tags);
  INSERT INTO notes_fts(rowid, path, title, content, tags)
  VALUES (new.rowid, new.path, new.title, new.content, new.tags);
END;
"""


def connect(db_path: str | Path, *, init: bool = True) -> sqlite3.Connection:
    """Open (and by default initialize) the context store database."""
    path = Path(db_path).expanduser()
    if init:
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if init:
        conn.executescript(SCHEMA)
        conn.commit()
    return conn
