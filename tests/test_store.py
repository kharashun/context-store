"""Unit tests for context_store.store."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from context_store import store
from context_store.store import StoreError


def test_save_and_read_roundtrip(store_db):
    result = store.save_note(
        store_db,
        "docs/fastapi/middleware",
        "Middleware processes HTTP requests.",
        kind="doc",
        tags=["FastAPI", "middleware"],
        title="Middleware",
        source_url="https://fastapi.tiangolo.com/middleware/",
    )
    assert result["status"] == "created"
    assert result["path"] == "docs/fastapi/middleware"

    note = store.read_note(store_db, "docs/fastapi/middleware")
    assert note["content"] == "Middleware processes HTTP requests."
    assert note["kind"] == "doc"
    assert note["tags"] == ["fastapi", "middleware"]  # normalized lowercase
    assert note["source_url"].startswith("https://")
    assert note["created_at"] == note["updated_at"]
    assert note["origin_session"] is None


def test_save_upsert(store_db):
    store.save_note(store_db, "notes/a/b", "v1")
    first = store.read_note(store_db, "notes/a/b")
    result = store.save_note(store_db, "notes/a/b", "v2")
    assert result["status"] == "updated"
    assert result["previous_updated_at"] == first["updated_at"]
    assert store.read_note(store_db, "notes/a/b")["content"] == "v2"


@pytest.mark.parametrize(
    "bad_path",
    ["", "   ", "/  /", "a b/c", "a/../b", "..", "a\\b", "x" * 600, None, 123],
)
def test_save_rejects_bad_paths(store_db, bad_path):
    with pytest.raises(StoreError):
        store.save_note(store_db, bad_path, "content")


def test_save_rejects_bad_kind_and_content(store_db):
    with pytest.raises(StoreError):
        store.save_note(store_db, "a/b", "content", kind="bogus")
    with pytest.raises(StoreError):
        store.save_note(store_db, "a/b", "   ")


def test_path_normalization(store_db):
    store.save_note(store_db, "//notes///x//", "content")
    assert store.read_note(store_db, "notes/x") is not None


def test_fts_index_sync(store_db):
    store.save_note(store_db, "docs/fastapi/middleware", "Middleware handles HTTP requests", kind="doc")
    store.save_note(store_db, "notes/auth/decision", "We chose JWT for auth", kind="decision")

    assert [h["path"] for h in store.search_notes(store_db, "auth")] == ["notes/auth/decision"]
    assert store.search_notes(store_db, "fastapi*")[0]["path"] == "docs/fastapi/middleware"

    # update: old terms disappear from the index
    store.save_note(store_db, "notes/auth/decision", "We chose cookies instead")
    assert store.search_notes(store_db, "JWT") == []
    assert store.search_notes(store_db, "cookies")[0]["path"] == "notes/auth/decision"

    # delete: no longer searchable
    store.delete_note(store_db, "docs/fastapi/middleware")
    assert store.search_notes(store_db, "fastapi") == []


def test_search_filters(store_db):
    store.save_note(store_db, "a", "alpha auth content", tags=["auth"], kind="note")
    store.save_note(store_db, "b", "beta auth content", tags=["security"], kind="decision")
    assert [h["path"] for h in store.search_notes(store_db, "auth", tag="auth")] == ["a"]
    assert [h["path"] for h in store.search_notes(store_db, "auth", kind="decision")] == ["b"]
    assert [h["path"] for h in store.search_notes(store_db, "auth")] == ["a", "b"]


def test_search_syntax_fallback(store_db):
    store.save_note(store_db, "a", "alpha auth content")
    store.save_note(store_db, "b", "beta content")
    # raw query has an unbalanced quote -> invalid FTS5 -> quoted-token fallback
    hits = store.search_notes(store_db, '"alpha')
    assert [h["path"] for h in hits] == ["a"]
    with pytest.raises(StoreError):
        store.search_notes(store_db, "   ")


def test_list_prefix_and_filters(store_db):
    store.save_note(store_db, "docs/fastapi/a", "x", kind="doc")
    store.save_note(store_db, "docs/fastapi/b", "y", kind="doc", tags=["auth"])
    store.save_note(store_db, "notes/z", "z", kind="note", tags=["auth"])

    assert [n["path"] for n in store.list_notes(store_db, prefix="docs/fastapi")] == [
        "docs/fastapi/a", "docs/fastapi/b",
    ]
    assert [n["path"] for n in store.list_notes(store_db, tag="auth")] == [
        "docs/fastapi/b", "notes/z",
    ]
    assert [n["path"] for n in store.list_notes(store_db, kind="note")] == ["notes/z"]
    assert [n["path"] for n in store.list_notes(store_db)] == [
        "docs/fastapi/a", "docs/fastapi/b", "notes/z",
    ]


def test_list_prefix_escapes_like_wildcards(store_db):
    store.save_note(store_db, "notes/100_percent", "x")
    store.save_note(store_db, "notes/100xpercent", "y")
    assert [n["path"] for n in store.list_notes(store_db, prefix="notes/100_p")] == [
        "notes/100_percent",
    ]


def test_delete(store_db):
    store.save_note(store_db, "notes/gone", "important " * 40, kind="note", tags=["t"])
    result = store.delete_note(store_db, "notes/gone")
    assert result["deleted"] is True
    assert result["kind"] == "note"
    assert result["tags"] == ["t"]
    assert len(result["content_preview"]) <= 201
    assert store.read_note(store_db, "notes/gone") is None
    assert store.delete_note(store_db, "notes/gone") is None


def test_delete_note_response_shape_unchanged(store_db):
    # regression: single-delete response keys (and their order) stay stable
    store.save_note(store_db, "notes/gone", "important " * 40, kind="howto", tags=["t"])
    result = store.delete_note(store_db, "notes/gone")
    assert list(result) == [
        "deleted", "path", "kind", "title", "tags",
        "content_preview", "created_at", "updated_at",
    ]
    assert len(result["content_preview"]) <= 201


def test_bulk_delete_happy_path(store_env):
    conn, db_path = store_env
    store.save_note(conn, "sessions/a/2026-01-one", "first summary", kind="session-summary")
    store.save_note(conn, "sessions/a/2026-02-two", "second summary", kind="session-summary")
    store.save_note(conn, "sessions/b/keep", "kept note")

    result = store.delete_notes(conn, db_path, prefix="sessions/a/", expect=2)

    assert result["deleted"] is True
    assert result["prefix"] == "sessions/a"
    assert result["count"] == 2
    assert sorted(n["path"] for n in result["notes"]) == [
        "sessions/a/2026-01-one", "sessions/a/2026-02-two",
    ]
    assert all(len(n["content_preview"]) <= 121 for n in result["notes"])
    assert Path(result["backup"]).exists()
    assert "restore" in result["recovery"]

    assert [n["path"] for n in store.list_notes(conn)] == ["sessions/b/keep"]
    assert store.search_notes(conn, "first summary") == []
    assert store.read_note(conn, "sessions/a/2026-01-one") is None


def test_bulk_delete_backup_is_restorable(store_env):
    conn, db_path = store_env
    store.save_note(conn, "sessions/x/one", "content one", kind="session-summary")
    result = store.delete_notes(conn, db_path, prefix="sessions/x", expect=1)

    snap = sqlite3.connect(f"file:{result['backup']}?mode=ro", uri=True)
    snap.row_factory = sqlite3.Row
    try:
        rows = snap.execute("SELECT path FROM notes ORDER BY path").fetchall()
    finally:
        snap.close()
    assert [r["path"] for r in rows] == ["sessions/x/one"]


def test_bulk_delete_expect_mismatch_refuses(store_env):
    conn, db_path = store_env
    store.save_note(conn, "sessions/a/one", "x")
    with pytest.raises(StoreError, match="expect"):
        store.delete_notes(conn, db_path, prefix="sessions/a", expect=5)
    assert store.read_note(conn, "sessions/a/one") is not None
    assert not (db_path.parent / "backups").exists()  # guards run before snapshot


def test_bulk_delete_no_matches(store_env):
    conn, db_path = store_env
    store.save_note(conn, "notes/keep", "x")
    with pytest.raises(StoreError, match="no notes under prefix"):
        store.delete_notes(conn, db_path, prefix="sessions/missing", expect=0)
    assert store.read_note(conn, "notes/keep") is not None


def test_bulk_delete_cap(store_env, monkeypatch):
    conn, db_path = store_env
    monkeypatch.setattr(store, "MAX_BULK_DELETE", 3)
    for i in range(4):
        store.save_note(conn, f"sessions/a/{i}", f"note {i}")
    with pytest.raises(StoreError, match="bulk cap"):
        store.delete_notes(conn, db_path, prefix="sessions/a", expect=4)
    assert len(store.list_notes(conn, prefix="sessions/a")) == 4


def test_bulk_delete_snapshot_failure_is_fail_closed(store_env, monkeypatch):
    conn, db_path = store_env
    from context_store import db

    store.save_note(conn, "sessions/a/one", "x")

    def boom(conn, db_path):
        raise OSError("disk full")

    monkeypatch.setattr(db, "snapshot", boom)
    with pytest.raises(StoreError, match="snapshot failed"):
        store.delete_notes(conn, db_path, prefix="sessions/a", expect=1)
    assert store.read_note(conn, "sessions/a/one") is not None


def test_bulk_delete_rejects_bad_prefix_and_expect(store_env):
    conn, db_path = store_env
    for bad_prefix in ("", "   ", "///", None):
        with pytest.raises(StoreError):
            store.delete_notes(conn, db_path, prefix=bad_prefix, expect=0)
    with pytest.raises(StoreError):
        store.delete_notes(conn, db_path, prefix="sessions/a", expect=-1)
    with pytest.raises(StoreError):
        store.delete_notes(conn, db_path, prefix="sessions/a", expect="2")


def test_bulk_delete_prunes_old_backups(store_env):
    conn, db_path = store_env
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir()
    stale = []
    for i in range(6):
        p = backup_dir / f"context-2026010{i}T000000.db"
        p.write_text("old")
        stamp = 1_000_000 + i
        os.utime(p, (stamp, stamp))
        stale.append(p)

    store.save_note(conn, "sessions/a/one", "x")
    result = store.delete_notes(conn, db_path, prefix="sessions/a", expect=1)
    fresh = Path(result["backup"])

    remaining = set(backup_dir.iterdir())
    assert fresh in remaining
    assert len(remaining) == 5  # fresh + 4 newest stale
    assert stale[0] not in remaining and stale[1] not in remaining
    assert all(s in remaining for s in stale[2:])


def test_stats(store_db):
    store.save_note(store_db, "a", "first", kind="note", tags=["t1"])
    store.save_note(store_db, "b", "second", kind="decision", tags=["t1", "t2"])
    summary = store.stats(store_db, store_db.execute("PRAGMA database_list").fetchone()["file"])
    assert summary["total_notes"] == 2
    assert summary["by_kind"] == {"note": 1, "decision": 1}
    assert {"tag": "t1", "count": 2} in summary["top_tags"]
    assert summary["db_size_bytes"] > 0
    assert len(summary["recent_saves"]) == 2
