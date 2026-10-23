import json
from datetime import datetime, timedelta, timezone

NOW = datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _make_config(**overrides):
    cfg = {
        "mode": "first_turn",
        "mental_model_ids": ["operator-context"],
        "max_chars": 6000,
        "cache_ttl_s": 21600,
        "timeout_s": 3.0,
        "skip_platforms": ["cron"],
    }
    cfg.update(overrides)
    return cfg


def _fake_transport(calls, content="remember to be terse", fail=False):
    """Stand-in for core._http_get_json. Records each URL and, unless ``fail``,
    returns a plausible list / single-model payload."""

    def transport(url, timeout_s, api_key):
        calls.append(url)
        if fail:
            raise RuntimeError("boom")
        if url.rstrip("/").endswith("/mental-models"):
            return {
                "items": [
                    {
                        "id": "operator-context",
                        "name": "Stable operating context",
                        "is_stale": False,
                        "last_refreshed_at": _iso(NOW),
                    }
                ]
            }
        if "/mental-models/operator-context" in url:
            return {
                "content": content,
                "name": "Stable operating context",
                "last_refreshed_at": _iso(NOW),
                "is_stale": False,
            }
        raise AssertionError(f"unexpected URL: {url}")

    return transport


def _setup_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HINDSIGHT_API_URL", "http://localhost:9999")
    monkeypatch.setenv("HINDSIGHT_BANK_ID", "bank-1")
    return tmp_path / "plugin-data" / "hermes-hindsight-plus"


def test_first_turn_inject_once_not_turn_two(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(core, "_http_get_json", _fake_transport(calls))

    state = plugin._State(_make_config())
    r1 = plugin._hook(state, session_id="s1", is_first_turn=True, platform="chat")
    assert r1 is not None
    assert "Stable operating context" in r1["context"]

    r2 = plugin._hook(state, session_id="s1", is_first_turn=False, platform="chat")
    assert r2 is None


def test_every_turn(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(core, "_http_get_json", _fake_transport(calls))

    state = plugin._State(_make_config(mode="every_turn"))
    r1 = plugin._hook(state, session_id="s1", is_first_turn=True, platform="chat")
    r2 = plugin._hook(state, session_id="s1", is_first_turn=False, platform="chat")
    assert r1 is not None
    assert r2 is not None


def test_off_is_cheap_no_io(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(core, "_http_get_json", _fake_transport(calls, fail=True))

    state = plugin._State(_make_config(mode="off"))
    assert plugin._hook(state, session_id="s1", is_first_turn=True, platform="chat") is None
    assert calls == []


def test_empty_ids_is_cheap_no_io(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(core, "_http_get_json", _fake_transport(calls, fail=True))

    state = plugin._State(_make_config(mental_model_ids=[]))
    assert plugin._hook(state, session_id="s1", is_first_turn=True, platform="chat") is None
    assert calls == []


def test_truncate_lands_on_line_boundary_and_respects_max_chars(core):
    text = "\n".join(f"line {i:03d} " + "x" * 40 for i in range(500))
    max_chars = 512
    out = core.truncate(text, max_chars, ["operator-context"])
    assert len(out) <= max_chars
    assert "[... truncated" in out
    body = out.split("[... truncated")[0].rstrip("\n")
    assert text.startswith(body)
    assert len(body) < len(text)
    assert text[len(body)] == "\n"  # cut happens at a complete line boundary


def test_truncate_noop_when_short(core):
    text = "hello\nworld"
    assert core.truncate(text, 1000, ["operator-context"]) == text


def test_cache_hit_performs_no_http(plugin, core, monkeypatch, tmp_path):
    cache_dir = _setup_env(monkeypatch, tmp_path)
    cache_dir.mkdir(parents=True)
    cache_file = cache_dir / "bank-1--operator-context.json"
    cache_file.write_text(
        json.dumps(
            {
                "content": "cached content here",
                "name": "Stable operating context",
                "fetched_at": _iso(NOW),
                "last_refreshed_at": _iso(NOW),
            }
        ),
        "utf-8",
    )

    calls = []
    monkeypatch.setattr(core, "_http_get_json", _fake_transport(calls, fail=True))

    text = core.build_injection(_make_config())
    assert text is not None
    assert "cached content here" in text
    assert calls == []


def test_stale_cache_fallback_on_http_error(plugin, core, monkeypatch, tmp_path):
    cache_dir = _setup_env(monkeypatch, tmp_path)
    cache_dir.mkdir(parents=True)
    cache_file = cache_dir / "bank-1--operator-context.json"
    cache_file.write_text(
        json.dumps(
            {
                "content": "old but usable",
                "name": "Stable operating context",
                "fetched_at": _iso(NOW - timedelta(days=3)),
                "last_refreshed_at": _iso(NOW - timedelta(days=3)),
            }
        ),
        "utf-8",
    )

    calls = []
    monkeypatch.setattr(core, "_http_get_json", _fake_transport(calls, fail=True))

    text = core.build_injection(_make_config())
    assert text is not None
    assert "old but usable" in text
    assert "repli cache" in text  # stale-cache age surfaced in the header


def test_fail_open_when_no_cache_and_network_dead(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(core, "_http_get_json", _fake_transport(calls, fail=True))

    assert core.build_injection(_make_config()) is None


def test_skip_platforms(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(core, "_http_get_json", _fake_transport(calls))

    state = plugin._State(_make_config())
    assert plugin._hook(state, session_id="s1", is_first_turn=True, platform="cron") is None
    assert calls == []


def test_no_api_key_in_header_or_cache(plugin, core, monkeypatch, tmp_path):
    cache_dir = _setup_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HINDSIGHT_API_KEY", "sk-SECRET-1234")
    calls = []
    monkeypatch.setattr(core, "_http_get_json", _fake_transport(calls))

    text = core.build_injection(_make_config())
    assert text is not None
    assert "sk-SECRET-1234" not in text

    cache_file = cache_dir / "bank-1--operator-context.json"
    assert cache_file.exists()
    assert "sk-SECRET-1234" not in cache_file.read_text("utf-8")


def test_per_session_guard(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(core, "_http_get_json", _fake_transport(calls))

    state = plugin._State(_make_config())
    r1 = plugin._hook(state, session_id="s1", is_first_turn=True, platform="chat")
    assert r1 is not None
    r2 = plugin._hook(state, session_id="s1", is_first_turn=True, platform="chat")
    assert r2 is None  # retried first turn: no double-inject
    r3 = plugin._hook(state, session_id="s2", is_first_turn=True, platform="chat")
    assert r3 is not None  # a different session still injects


def test_register_wires_hook(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(core, "_http_get_json", _fake_transport(calls))

    class FakeCtx:
        def __init__(self):
            self.config = {}
            self.hooks = {}

        def register_hook(self, name, callback):
            self.hooks[name] = callback

    ctx = FakeCtx()
    plugin.register(ctx)
    assert "pre_llm_call" in ctx.hooks

    result = ctx.hooks["pre_llm_call"](
        session_id="s1",
        is_first_turn=True,
        platform="chat",
        model="gpt",
        user_message="hello",
        turn_id="t1",
    )
    assert result == {"context": result["context"]}
    assert "Stable operating context" in result["context"]


def test_config_json_resolution_and_bearer_key(plugin, core, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HINDSIGHT_API_URL", raising=False)
    monkeypatch.delenv("HINDSIGHT_BANK_ID", raising=False)
    monkeypatch.delenv("HINDSIGHT_API_KEY", raising=False)

    (tmp_path / "hindsight").mkdir()
    (tmp_path / "hindsight" / "config.json").write_text(
        json.dumps({"api_url": "http://cfg.local", "bank_id": "cfg-bank", "api_key": "cfg-key"}),
        "utf-8",
    )

    calls = []

    def transport(url, timeout_s, api_key):
        calls.append((url, api_key))
        if url.rstrip("/").endswith("/mental-models"):
            return {
                "items": [
                    {"id": "operator-context", "name": "N", "is_stale": False,
                     "last_refreshed_at": _iso(NOW)}
                ]
            }
        return {"content": "from config", "name": "N",
                "last_refreshed_at": _iso(NOW), "is_stale": False}

    monkeypatch.setattr(core, "_http_get_json", transport)

    text = core.build_injection(_make_config())
    assert text is not None
    assert "from config" in text
    assert calls and calls[0][1] == "cfg-key"
