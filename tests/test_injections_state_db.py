import json
import sqlite3

import pytest

PREAMBLE = (
    "The following memories were recalled from your long-term memory.\n"
    "Use them when they are relevant to the conversation."
)


def _write_config(tmp_path, payload):
    config_dir = tmp_path / "hindsight"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(json.dumps(payload), "utf-8")


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
    assert "System note" not in (provider["text"] or "")


def test_state_db_injections_prose_without_bullet(core, tmp_path):
    mem = (
        "<memory-context>\n"
        "[System note: recalled 1 memories]\n"
        "a plain memory line without bullet\n"
        "</memory-context>"
    )
    api = "q\n\n" + mem + "\n\n## Stable context (Hindsight x)\n\nhi"
    _make_db(
        tmp_path,
        [(1, "sess1", "user", "q", 1731234567.0, 1, api)],
    )

    result = core.read_state_db_injections("sess1", hermes_home=tmp_path, limit=200)

    provider = [e for e in result["entries"] if e["source"] == "provider recall"][0]
    assert provider["count"] is None
    assert "System note" not in (provider["text"] or "")


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


def test_state_db_injections_mental_models_without_tags(core, tmp_path):
    plugin = (
        "## Stable context (Hindsight mental models)\n\n"
        "## operator-context\n\nhello"
    )
    api = "a question with no recall tags\n\n" + plugin
    _make_db(
        tmp_path,
        [(503670, "sess1", "user", "a question", 1731234567.0, 1, api)],
    )

    result = core.read_state_db_injections("sess1", hermes_home=tmp_path, limit=200)

    assert result["source"] == "state.db"
    assert len(result["entries"]) == 1
    entry = result["entries"][0]
    assert entry["kind"] == "mental-models"
    assert entry["text"].startswith("## Stable context (Hindsight mental models)")
    assert entry["session_id"] == "sess1"
    assert entry["recorded_at"]


def test_state_db_injections_provider_recall_preamble(core, tmp_path):
    _write_config(tmp_path, {"recall_prompt_preamble": PREAMBLE})
    api = "# " + PREAMBLE + "\n\n- a recalled fact"
    _make_db(
        tmp_path,
        [(503758, "sess1", "user", "a question", 1731234567.0, 1, api)],
    )

    result = core.read_state_db_injections("sess1", hermes_home=tmp_path, limit=200)

    assert len(result["entries"]) == 1
    entry = result["entries"][0]
    assert entry["kind"] == "provider-recall"
    assert entry["source"] == "provider recall"
    assert entry["text"].startswith(PREAMBLE.splitlines()[0])
    assert "- a recalled fact" in entry["text"]
    assert entry["injected_chars"] == len(entry["text"])


def test_state_db_injections_both_markers(core, tmp_path):
    _write_config(tmp_path, {"recall_prompt_preamble": PREAMBLE})
    plugin = "## Stable context (Hindsight mental model)\n\n## operator-context\n\nhi"
    api = "q\n\n" + PREAMBLE + "\n\n- fact one\n\n" + plugin
    _make_db(
        tmp_path,
        [(505392, "sess1", "user", "q", 1731234567.0, 1, api)],
    )

    result = core.read_state_db_injections("sess1", hermes_home=tmp_path, limit=200)

    assert len(result["entries"]) == 2
    by_kind = {entry["kind"]: entry for entry in result["entries"]}
    assert set(by_kind) == {"provider-recall", "mental-models"}
    assert by_kind["provider-recall"]["text"].startswith(PREAMBLE.splitlines()[0])
    assert by_kind["mental-models"]["text"].startswith(
        "## Stable context (Hindsight mental model)"
    )


def test_state_db_injections_preamble_inside_tags_is_not_duplicated(core, tmp_path):
    _write_config(tmp_path, {"recall_prompt_preamble": PREAMBLE})
    mem = "<memory-context>\n" + PREAMBLE + "\n- fact one\n</memory-context>"
    _make_db(
        tmp_path,
        [(1, "sess1", "user", "q", 1731234567.0, 1, "q\n\n" + mem)],
    )

    result = core.read_state_db_injections("sess1", hermes_home=tmp_path, limit=200)

    assert [entry["kind"] for entry in result["entries"]] == ["memory-context"]


def test_state_db_injections_plain_user_text(core, tmp_path):
    _write_config(tmp_path, {"recall_prompt_preamble": PREAMBLE})
    _make_db(
        tmp_path,
        [(1, "sess1", "user", "just a question", 1731234567.0, 1, "just a question")],
    )

    result = core.read_state_db_injections("sess1", hermes_home=tmp_path, limit=200)

    assert result["source"] == "state.db"
    assert result["entries"] == []


def test_state_db_injections_no_config_skips_preamble(core, tmp_path):
    api = "q\n\n" + PREAMBLE + "\n\n- fact one"
    _make_db(
        tmp_path,
        [(1, "sess1", "user", "q", 1731234567.0, 1, api)],
    )

    result = core.read_state_db_injections("sess1", hermes_home=tmp_path, limit=200)

    assert result["source"] == "state.db"
    assert result["entries"] == []
