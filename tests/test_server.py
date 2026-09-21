"""End-to-end tests: MCP tools via the in-process FastMCP client."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastmcp import Client

from context_store import main as server_main


def payload(result):
    """Extract the tool result payload across fastmcp result shapes."""
    data = getattr(result, "data", None)
    if isinstance(data, (dict, list)):
        return data
    for content in getattr(result, "content", None) or []:
        text = getattr(content, "text", None)
        if text:
            return json.loads(text)
    return data


@pytest.fixture
def env(tmp_path, monkeypatch, opencode_db):
    monkeypatch.setenv("CONTEXT_DB", str(tmp_path / "context.db"))
    monkeypatch.setenv("OPENCODE_DB", str(opencode_db))
    return tmp_path


def test_note_lifecycle_over_mcp(env):
    async def scenario():
        async with Client(server_main.mcp) as client:
            saved = payload(await client.call_tool("save", {
                "path": "sessions/test/2026-09-21-smoke",
                "content": "We built a local context store with FTS5.",
                "kind": "session-summary",
                "tags": ["context-store", "sqlite"],
            }))
            assert saved["status"] == "created"

            # upsert over MCP
            again = payload(await client.call_tool("save", {
                "path": "sessions/test/2026-09-21-smoke",
                "content": "We built a local context store with FTS5. Revised.",
                "kind": "session-summary",
            }))
            assert again["status"] == "updated"
            assert "previous_updated_at" in again

            note = payload(await client.call_tool("read", {
                "path": "sessions/test/2026-09-21-smoke",
            }))
            assert note["content"].endswith("Revised.")
            assert note["kind"] == "session-summary"
            assert note["origin_session"] is None  # no session meta in tests

            hits = payload(await client.call_tool("search", {"query": "context store"}))
            assert any(h["path"] == "sessions/test/2026-09-21-smoke" for h in hits)
            assert all("**" not in h["path"] for h in hits)

            listed = payload(await client.call_tool("list", {"prefix": "sessions/"}))
            assert [n["path"] for n in listed] == ["sessions/test/2026-09-21-smoke"]

            overview = payload(await client.call_tool("stats", {}))
            assert overview["total_notes"] == 1
            assert overview["by_kind"] == {"session-summary": 1}

            deleted = payload(await client.call_tool("delete", {
                "path": "sessions/test/2026-09-21-smoke",
            }))
            assert deleted["deleted"] is True
            assert payload(await client.call_tool("search", {"query": "context store"})) == []

    asyncio.run(scenario())


def test_tool_errors_surface_as_mcp_errors(env):
    async def scenario():
        async with Client(server_main.mcp) as client:
            with pytest.raises(Exception):
                await client.call_tool("read", {"path": "does/not/exist"})
            with pytest.raises(Exception):
                await client.call_tool("save", {"path": "bad path", "content": "x"})
            with pytest.raises(Exception):
                await client.call_tool("session_read", {"session_id": "ses_missing"})

    asyncio.run(scenario())


def test_session_tools_over_mcp(env):
    async def scenario():
        async with Client(server_main.mcp) as client:
            listed = payload(await client.call_tool("sessions_list", {"limit": 10}))
            assert [s["session_id"] for s in listed] == [
                "ses_beta", "ses_gamma", "ses_alpha",
            ]

            only_alpha = payload(await client.call_tool(
                "sessions_list", {"project": "alpha"}))
            assert [s["session_id"] for s in only_alpha] == ["ses_alpha"]

            transcript = payload(await client.call_tool(
                "session_read", {"session_id": "ses_alpha"}))
            assert "Fix the auth middleware please." in transcript["transcript"]
            assert "I'll refactor the auth middleware." in transcript["transcript"]
            assert "[tool:" not in transcript["transcript"]

    asyncio.run(scenario())


def test_bulk_delete_over_mcp(env):
    async def scenario():
        async with Client(server_main.mcp) as client:
            for i in range(3):
                payload(await client.call_tool("save", {
                    "path": f"sessions/test/2026-09-21-{i}",
                    "content": f"summary {i}",
                    "kind": "session-summary",
                }))
            payload(await client.call_tool("save", {
                "path": "notes/test/keep", "content": "kept",
            }))

            listed = payload(await client.call_tool("list", {"prefix": "sessions/test"}))
            assert len(listed) == 3

            result = payload(await client.call_tool("delete", {
                "prefix": "sessions/test", "expect": 3,
            }))
            assert result["deleted"] is True
            assert result["count"] == 3
            assert sorted(n["path"] for n in result["notes"]) == [
                "sessions/test/2026-09-21-0",
                "sessions/test/2026-09-21-1",
                "sessions/test/2026-09-21-2",
            ]
            backup = Path(result["backup"])
            assert backup.exists()
            assert backup.parent == env / "backups"

            remaining = payload(await client.call_tool("list", {}))
            assert [n["path"] for n in remaining] == ["notes/test/keep"]

    asyncio.run(scenario())


def test_bulk_delete_guards_over_mcp(env):
    async def scenario():
        async with Client(server_main.mcp) as client:
            payload(await client.call_tool("save", {
                "path": "sessions/test/one", "content": "x",
            }))

            for bad_args in (
                {"prefix": "sessions/test"},                                # no expect
                {"prefix": "sessions/test", "expect": 7},                   # wrong expect
                {"path": "a/b", "prefix": "sessions", "expect": 1},         # both modes
                {},                                                        # neither
                {"path": "a/b", "expect": 2},                              # expect w/o prefix
            ):
                with pytest.raises(Exception):
                    await client.call_tool("delete", bad_args)

            # every refused call deleted nothing
            listed = payload(await client.call_tool("list", {"prefix": "sessions/test"}))
            assert len(listed) == 1

    asyncio.run(scenario())
