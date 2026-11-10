import sqlite3

import pytest


def _make_db(tmp_path, rows):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE messages ("
        "id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
        "timestamp REAL, active INTEGER, api_content TEXT)"
    )
    for row in rows:
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, timestamp, "
            "active, api_content) VALUES (?,?,?,?,?,?,?)",
            row,
        )
    conn.commit()
    conn.close()
    return db


def test_state_db_injections_parse_and_count(core, tmp_path):
    mem = (
        "<memory-context>\n"
        "[System note: recalled 3 memories]\n"
        "- fact one\n"
        "- fact two\n"
        "</memory-context>"
    )
    plugin = "## Stable context (Hindsight mental model)\n\n## operator-context\n\nhello"
    api = "user question\n\n" + mem + "\n\n" + plugin
    _make_db(
        tmp_path,
        [(1, "sess1", "user", "user question", 1731234567.0, 1, api)],
    )

    result = core.read_state_db_injections("sess1", hermes_home=tmp_path, limit=200)

    assert result["source"] == "state.db"
    assert len(result["entries"]) == 2
    provider = [e for e in result["entries"] if e["source"] == "provider recall"][0]
    plugin_entry = [
        e for e in result["entries"] if e["source"] == "plugin mental-models"
    ][0]
    assert provider["count"] == 2
    assert plugin_entry["count"] == 2


def test_state_db_injections_empty_memory_status(core, tmp_path):
    mem = "<memory-context>\n\n</memory-context>"
    plugin = "## Stable context (Hindsight x)\n\nhi"
    api = "q2\n\n" + mem + "\n\n" + plugin
    _make_db(
        tmp_path,
        [(1, "sess1", "user", "q2", 1731234567.0, 1, api)],
    )

    result = core.read_state_db_injections("sess1", hermes_home=tmp_path, limit=200)

    provider = [e for e in result["entries"] if e["source"] == "provider recall"][0]
    assert provider["status"] == "empty"


def test_state_db_injections_unknown_session(core, tmp_path):
    _make_db(tmp_path, [])

    result = core.read_state_db_injections("unknown", hermes_home=tmp_path, limit=200)

    assert result["source"] == "state.db"
    assert result["entries"] == []


def test_state_db_injections_missing_db(core, tmp_path):
    result = core.read_state_db_injections("sess1", hermes_home=tmp_path, limit=200)

    assert result["source"] == "unavailable"
    assert result.get("reason")
    assert result["entries"] == []