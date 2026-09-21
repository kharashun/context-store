"""Unit tests for context_store.sessions (read-only OpenCode DB access)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from context_store import sessions
from context_store.sessions import SessionsUnavailableError, _parse_since


def test_parse_since_variants():
    iso_day = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp() * 1000)
    assert _parse_since("2026-09-01") == iso_day
    assert _parse_since("2026-09-01T10:00:00Z") == iso_day + 10 * 3600 * 1000
    assert _parse_since("1756684800") == 1756684800 * 1000  # epoch seconds
    assert _parse_since("1788256800000") == 1788256800000  # already ms
    with pytest.raises(ValueError):
        _parse_since("not-a-date")


def test_list_sessions_order_and_filters(opencode_db):
    listed = sessions.list_sessions(opencode_db)
    assert [s["session_id"] for s in listed] == ["ses_beta", "ses_gamma", "ses_alpha"]
    assert listed[0]["message_count"] == 1
    assert listed[0]["created"].startswith("2026-09-21")

    only_alpha = sessions.list_sessions(opencode_db, project="alpha")
    assert [s["session_id"] for s in only_alpha] == ["ses_alpha"]

    since = sessions.list_sessions(opencode_db, since="2026-09-15")
    assert [s["session_id"] for s in since] == ["ses_beta"]


def test_list_sessions_missing_db(tmp_path):
    with pytest.raises(SessionsUnavailableError):
        sessions.list_sessions(tmp_path / "nope.db")


def test_read_session_compaction(opencode_db):
    result = sessions.read_session(opencode_db, "ses_alpha")
    transcript = result["transcript"]

    assert result["title"] == "Alpha auth work"
    assert result["message_count"] == 4  # includes the skipped system message
    assert "## user" in transcript and "## assistant" in transcript
    assert "Fix the auth middleware please." in transcript
    assert "I'll refactor the auth middleware." in transcript

    # skipped by default
    assert "thinking about jwt" not in transcript  # reasoning
    assert "[tool:" not in transcript
    skipped = result["stats"]["skipped"]
    assert skipped["reasoning"] == 1
    assert skipped["tool"] == 1
    assert skipped["system"] == 1
    assert skipped["patch"] == 1  # unknown content type in v2 messages

    # tools included on request (tool name comes from the `name` key)
    with_tools = sessions.read_session(opencode_db, "ses_alpha", include_tools=True)
    assert "[tool: skill (completed)]" in with_tools["transcript"]


def test_read_session_truncation(opencode_db):
    result = sessions.read_session(opencode_db, "ses_gamma", max_chars=3000)
    # each 3000-char part is capped at 2000 chars, transcript at max_chars
    assert result["stats"]["parts_truncated"] >= 1
    assert result["transcript_chars"] <= 3000
    assert result["stats"]["messages_omitted"] >= 1
    assert "…[transcript truncated]" in result["transcript"] or result["stats"]["messages_omitted"] > 0


def test_read_session_not_found(opencode_db):
    with pytest.raises(LookupError):
        sessions.read_session(opencode_db, "ses_missing")


def test_get_session_info(opencode_db, project_root):
    info = sessions.get_session_info(opencode_db, "ses_alpha")
    assert info == {"directory": f"{project_root}/alpha"}
    assert sessions.get_session_info(opencode_db, "ses_missing") is None
    # best-effort: missing database yields None, never raises
    import pathlib

    assert sessions.get_session_info(pathlib.Path("/nonexistent/opencode.db"), "x") is None
