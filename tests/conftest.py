"""Shared fixtures: a miniature OpenCode session database.

Mirrors the current OpenCode storage model:
- `session_v2` (id, title, directory, time_created, time_updated)
- `session_message` (id, session_id, type, seq, time_created, time_updated, data)
  where user messages carry {"text": ...} and assistant messages carry
  {"content": [{type: text|reasoning|tool|..., ...}]}.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

# Synthetic project directories for fixture sessions — deliberately neutral so
# no real home-directory layout leaks into the repository. Override with
# CONTEXT_STORE_TEST_PROJECT_ROOT to mimic a real layout locally.
PROJECT_ROOT = os.environ.get("CONTEXT_STORE_TEST_PROJECT_ROOT", "/projects")

OPENCODE_SCHEMA = """
CREATE TABLE session_v2 (
  id           TEXT PRIMARY KEY,
  title        TEXT,
  directory    TEXT NOT NULL,
  time_created INTEGER NOT NULL,
  time_updated INTEGER NOT NULL
);
CREATE TABLE session_message (
  id           TEXT PRIMARY KEY,
  session_id   TEXT NOT NULL,
  type         TEXT NOT NULL,
  seq          INTEGER NOT NULL,
  time_created INTEGER NOT NULL,
  time_updated INTEGER NOT NULL,
  data         TEXT NOT NULL
);
CREATE UNIQUE INDEX session_message_session_seq_idx ON session_message (session_id, seq);
"""

ALPHA = {
    "id": "ses_alpha",
    "title": "Alpha auth work",
    "directory": f"{PROJECT_ROOT}/alpha",
    "time_created": 1788256800000,  # 2026-09-01T10:00:00Z
    "time_updated": 1788260400000,
    "messages": [
        {"type": "system", "data": {"text": "system prompt noise"}},
        {"type": "user", "data": {"text": "Fix the auth middleware please.",
                                  "files": [], "agents": []}},
        {"type": "assistant", "data": {"content": [
            {"type": "reasoning", "text": "thinking about jwt vs cookies"},
            {"type": "text", "text": "I'll refactor the auth middleware."},
            {"type": "tool", "id": "t1", "name": "skill",
             "state": {"status": "completed"}},
            {"type": "patch", "files": [f"{PROJECT_ROOT}/alpha/auth.py"]},
        ]}},
        {"type": "assistant", "data": {"content": [{"type": "text", "text": "Done."}]}},
    ],
}

GAMMA = {
    "id": "ses_gamma",
    "title": "Gamma long session",
    "directory": f"{PROJECT_ROOT}/gamma",
    "time_created": 1789066800000,  # 2026-09-10T09:00:00Z
    "time_updated": 1789068600000,
    "messages": [
        {"type": "user", "data": {"text": "T" * 3000}},
        {"type": "assistant", "data": {"content": [{"type": "text", "text": "A" * 3000}]}},
        {"type": "assistant", "data": {"content": [{"type": "text", "text": "B" * 3000}]}},
        {"type": "assistant", "data": {"content": [{"type": "text", "text": "C" * 3000}]}},
    ],
}

BETA = {
    "id": "ses_beta",
    "title": "Beta debug",
    "directory": f"{PROJECT_ROOT}/beta",
    "time_created": 1789952400000,  # 2026-09-21T01:00:00Z (newest)
    "time_updated": 1789953600000,
    "messages": [
        {"type": "user", "data": {"text": "Why is beta crashing?"}},
    ],
}

ALL_SESSIONS = (ALPHA, GAMMA, BETA)


def build_opencode_db(path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(OPENCODE_SCHEMA)
    for spec in ALL_SESSIONS:
        conn.execute(
            "INSERT INTO session_v2 (id, title, directory, time_created, time_updated)"
            " VALUES (?,?,?,?,?)",
            (spec["id"], spec["title"], spec["directory"],
             spec["time_created"], spec["time_updated"]),
        )
        for seq, message in enumerate(spec["messages"]):
            conn.execute(
                "INSERT INTO session_message"
                " (id, session_id, type, seq, time_created, time_updated, data)"
                " VALUES (?,?,?,?,?,?,?)",
                (f"{spec['id']}_m{seq}", spec["id"], message["type"], seq,
                 spec["time_created"] + seq, spec["time_created"] + seq,
                 json.dumps(message["data"])),
            )
    conn.commit()
    conn.close()


@pytest.fixture
def opencode_db(tmp_path):
    path = tmp_path / "opencode.db"
    build_opencode_db(path)
    return path


@pytest.fixture(scope="session")
def project_root() -> str:
    """The synthetic project root used by fixture session directories."""
    return PROJECT_ROOT


@pytest.fixture
def store_db(tmp_path):
    from context_store.db import connect

    conn = connect(tmp_path / "context.db")
    yield conn
    conn.close()


@pytest.fixture
def store_env(tmp_path):
    """(conn, db_path) pair — for store operations that need the db file path."""
    from context_store.db import connect

    path = tmp_path / "context.db"
    conn = connect(path)
    yield conn, path
    conn.close()
