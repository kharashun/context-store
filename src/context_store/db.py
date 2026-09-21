"""SQLite setup and schema for the context store.

One database file, WAL mode, with an external-content FTS5 index kept in
sync with the `notes` table by triggers.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BACKUP_KEEP = 5  # newest snapshots retained in <db dir>/backups/

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


def snapshot(conn: sqlite3.Connection, db_path: str | Path) -> Path:
    """VACUUM INTO a timestamped copy in <db dir>/backups/; prune old ones.

    Fail-closed: any error propagates — callers must treat a failure as
    "refuse the operation".
    """
    db = Path(db_path).expanduser()
    backup_dir = db.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    target = backup_dir / f"{db.stem}-{stamp}.db"
    suffix = 0
    while target.exists():
        suffix += 1
        target = backup_dir / f"{db.stem}-{stamp}-{suffix}.db"
    conn.commit()  # VACUUM cannot run inside an open transaction
    conn.execute("VACUUM INTO ?", (str(target),))
    _prune_backups(backup_dir)
    return target


def _prune_backups(backup_dir: Path, *, keep: int = BACKUP_KEEP) -> None:
    """Keep the newest `keep` backups by mtime; best-effort, never raises."""
    try:
        backups = sorted(
            (p for p in backup_dir.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for old in backups[keep:]:
        try:
            old.unlink()
        except OSError:
            pass
