import json
import os
import sqlite3
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

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


def _audit_details(text="## Stable operating context\n\nremember to be terse"):
    return {
        "text": text,
        "assembled_chars": len(text) + 20,
        "max_chars": 6000,
        "truncated": False,
        "context_note_chars": 0,
        "models": [
            {
                "id": "operator-context",
                "name": "Stable operating context",
                "last_refreshed_at": _iso(NOW),
                "is_stale": False,
                "cache_age_s": None,
                "content_chars": len(text),
            }
        ],
    }


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


def test_unquoted_off_boolean_is_treated_as_off(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(core, "_http_get_json", _fake_transport(calls, fail=True))

    # Hermes parses YAML 1.1: an unquoted `mode: off` in config.yaml is False.
    state = plugin._State(_make_config(mode=False))
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


def test_truncate_respects_tiny_max_chars(core):
    text = "a" * 100
    for limit in (1, 5, 20):
        out = core.truncate(text, limit, ["operator-context"])
        assert len(out) <= limit


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


def test_mental_model_url_uses_v1_default_and_content_detail(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    seen = []

    def transport(url, timeout_s, api_key):
        seen.append((url, timeout_s, api_key))
        return {
            "id": "operator-context",
            "name": "Stable operating context",
            "content": "hello",
            "last_refreshed_at": _iso(NOW),
            "is_stale": False,
        }

    monkeypatch.setattr(core, "_http_get_json", transport)
    text = core.build_injection(_make_config())
    assert text is not None
    assert len(seen) == 1
    url, timeout_s, _key = seen[0]
    assert "/v1/default/banks/bank-1/mental-models/operator-context" in url
    assert "detail=content" in url
    assert 0 < timeout_s <= 3.0


def test_live_http_round_trip(plugin, core, monkeypatch, tmp_path):
    import http.server
    import threading

    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization")
            body = json.dumps(
                {
                    "id": "operator-context",
                    "name": "Stable operating context",
                    "content": "live content",
                    "last_refreshed_at": _iso(NOW),
                    "is_stale": False,
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("HINDSIGHT_API_URL", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setenv("HINDSIGHT_BANK_ID", "bank-1")
        monkeypatch.setenv("HINDSIGHT_API_KEY", "sk-live-secret")

        text = core.build_injection(_make_config())
        assert text is not None
        assert "live content" in text
        assert seen["path"] == (
            "/v1/default/banks/bank-1/mental-models/operator-context?detail=content"
        )
        assert seen["auth"] == "Bearer sk-live-secret"

        cache_file = (
            tmp_path / "plugin-data" / "hermes-hindsight-plus"
            / "bank-1--operator-context.json"
        )
        assert cache_file.exists()
        assert "sk-live-secret" not in cache_file.read_text("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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
    assert "cache fallback" in text  # stale-cache age surfaced in the header


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
        def __init__(self, settings=None):
            self.settings = dict(settings or {})
            self.hooks = {}

        def get_config(self, key, default=None):
            return self.settings.get(key, default)

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


def test_register_reads_ctx_get_config_and_opts_into_kwargs(plugin, core, monkeypatch, tmp_path):
    import inspect

    _setup_env(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(core, "_http_get_json", _fake_transport(calls, fail=True))

    class FakeCtx:
        def __init__(self, settings):
            self.settings = settings
            self.hooks = {}

        def get_config(self, key, default=None):
            return self.settings.get(key, default)

        def register_hook(self, name, callback):
            self.hooks[name] = callback

    ctx = FakeCtx({"mode": "off"})
    plugin.register(ctx)
    hook = ctx.hooks["pre_llm_call"]

    # A **kwargs signature is what Hermes' additive hook contract expects.
    kinds = {p.kind for p in inspect.signature(hook).parameters.values()}
    assert inspect.Parameter.VAR_KEYWORD in kinds

    # ctx.get_config("mode") == "off" must win over the default.
    assert hook(session_id="s1", is_first_turn=True, platform="chat") is None
    assert calls == []


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


def _get_404(url, timeout_s, api_key):
    raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


def test_create_missing_posts_on_404_with_daily_cron(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    calls = []

    def get(url, timeout_s, api_key):
        calls.append(("GET", url))
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    def post(url, timeout_s, api_key, payload):
        calls.append(("POST", url, payload, api_key))
        return {"mental_model_id": payload["id"], "operation_id": "op-1"}

    monkeypatch.setattr(core, "_http_get_json", get)
    monkeypatch.setattr(core, "_http_post_json", post)

    assert core.build_injection(_make_config()) is None

    posts = [call for call in calls if call[0] == "POST"]
    assert len(posts) == 1
    _, url, payload, _key = posts[0]
    assert url.endswith("/v1/default/banks/bank-1/mental-models")
    assert payload["id"] == "operator-context"
    assert payload["name"] == core.DEFAULT_CREATE_NAME
    assert payload["source_query"] == core.DEFAULT_CREATE_SOURCE_QUERY
    assert payload["max_tokens"] == 2048
    assert payload["trigger"] == {
        "mode": "full",
        "refresh_after_consolidation": False,
        "refresh_cron": "0 3 * * *",
    }


def test_existing_model_never_posts(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    posts = []
    monkeypatch.setattr(core, "_http_get_json", _fake_transport([]))
    monkeypatch.setattr(core, "_http_post_json", lambda *a, **k: posts.append(a) or {})

    text = core.build_injection(_make_config())
    assert text is not None
    assert posts == []


def test_create_missing_disabled_does_not_post(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    posts = []
    monkeypatch.setattr(core, "_http_get_json", _get_404)
    monkeypatch.setattr(core, "_http_post_json", lambda *a, **k: posts.append(a) or {})

    assert core.build_injection(_make_config(create_missing=False)) is None
    assert posts == []


def test_create_options_are_configurable(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    posts = []

    def post(url, timeout_s, api_key, payload):
        posts.append(payload)
        return {}

    monkeypatch.setattr(core, "_http_get_json", _get_404)
    monkeypatch.setattr(core, "_http_post_json", post)

    core.build_injection(
        _make_config(
            create_name="My context",
            create_source_query="My query",
            create_refresh_cron="15 4 * * *",
            create_max_tokens=4096,
            create_mode="delta",
        )
    )

    assert len(posts) == 1
    payload = posts[0]
    assert payload["name"] == "My context"
    assert payload["source_query"] == "My query"
    assert payload["max_tokens"] == 4096
    assert payload["trigger"] == {
        "mode": "delta",
        "refresh_after_consolidation": False,
        "refresh_cron": "15 4 * * *",
    }


def test_create_refresh_cron_empty_omits_schedule(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    posts = []

    def post(url, timeout_s, api_key, payload):
        posts.append(payload)
        return {}

    monkeypatch.setattr(core, "_http_get_json", _get_404)
    monkeypatch.setattr(core, "_http_post_json", post)

    core.build_injection(_make_config(create_refresh_cron=""))

    assert len(posts) == 1
    assert "refresh_cron" not in posts[0]["trigger"]
    assert posts[0]["trigger"]["refresh_after_consolidation"] is False


def test_create_missing_invalid_id_does_not_post(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    posts = []
    monkeypatch.setattr(core, "_http_get_json", _get_404)
    monkeypatch.setattr(core, "_http_post_json", lambda *a, **k: posts.append(a) or {})

    assert core.build_injection(_make_config(mental_model_ids=["Bad_ID"])) is None
    assert posts == []


def test_create_failure_is_fail_open(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)

    def post(url, timeout_s, api_key, payload):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(core, "_http_get_json", _get_404)
    monkeypatch.setattr(core, "_http_post_json", post)

    assert core.build_injection(_make_config()) is None


def test_create_403_keeps_stale_cache(plugin, core, monkeypatch, tmp_path):
    cache_dir = _setup_env(monkeypatch, tmp_path)
    cache_dir.mkdir(parents=True)
    (cache_dir / "bank-1--operator-context.json").write_text(
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

    def post(url, timeout_s, api_key, payload):
        raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(core, "_http_get_json", _get_404)
    monkeypatch.setattr(core, "_http_post_json", post)

    text = core.build_injection(_make_config())
    assert text is not None
    assert "old but usable" in text
    assert "cache fallback" in text


def test_create_409_is_treated_as_exists(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)

    def post(url, timeout_s, api_key, payload):
        raise urllib.error.HTTPError(url, 409, "Conflict", {}, None)

    monkeypatch.setattr(core, "_http_get_json", _get_404)
    monkeypatch.setattr(core, "_http_post_json", post)

    assert core.build_injection(_make_config()) is None


def test_create_only_on_404_not_500(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    posts = []

    def get(url, timeout_s, api_key):
        raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)

    monkeypatch.setattr(core, "_http_get_json", get)
    monkeypatch.setattr(core, "_http_post_json", lambda *a, **k: posts.append(a) or {})

    assert core.build_injection(_make_config()) is None
    assert posts == []


def test_create_missing_sends_bearer_and_writes_no_key(plugin, core, monkeypatch, tmp_path):
    cache_dir = _setup_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HINDSIGHT_API_KEY", "sk-CREATE-SECRET")
    seen = {}

    def post(url, timeout_s, api_key, payload):
        seen["api_key"] = api_key
        return {}

    monkeypatch.setattr(core, "_http_get_json", _get_404)
    monkeypatch.setattr(core, "_http_post_json", post)

    assert core.build_injection(_make_config()) is None
    assert seen["api_key"] == "sk-CREATE-SECRET"
    assert list(cache_dir.glob("*.json")) == []


def test_success_logs_injection_metadata_only(
    plugin, core, monkeypatch, tmp_path, caplog
):
    import logging

    _setup_env(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(core, "_http_get_json", _fake_transport(calls))
    caplog.set_level(logging.INFO, logger="hermes_hindsight_plus")

    state = plugin._State(_make_config())
    result = plugin._hook(
        state, session_id="s-log", is_first_turn=True, platform="chat"
    )
    assert result is not None

    log = caplog.text
    assert "hindsight-plus: injected" in log
    assert "first_turn=True" in log
    assert "session=s-log" in log
    # The log carries metadata only, never the model content.
    assert "remember to be terse" not in log


def test_context_note_prepended_before_model(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.setattr(core, "_http_get_json", _fake_transport([]))

    text = core.build_injection(
        _make_config(context_note="DEPLOYMENT: profile X on instance Y")
    )
    assert text is not None
    assert text.index("DEPLOYMENT:") < text.index("## Stable operating context")
    assert "remember to be terse" in text


def test_context_note_alone_does_not_inject(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.setattr(core, "_http_get_json", _fake_transport([], fail=True))

    assert (
        core.build_injection(
            _make_config(context_note="DEPLOYMENT: profile X on instance Y")
        )
        is None
    )


def test_as_bool(core):
    assert core._as_bool(True, False) is True
    assert core._as_bool("false", True) is False
    assert core._as_bool("off", True) is False
    assert core._as_bool("yes", False) is True
    assert core._as_bool(None, True) is True


def test_live_create_then_fetch(plugin, core, monkeypatch, tmp_path):
    import http.server
    import threading

    state = {"created": False, "methods": [], "post_body": None}

    class Handler(http.server.BaseHTTPRequestHandler):
        def _json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state["methods"].append("GET")
            if not state["created"]:
                self._json(404, {"detail": "not found"})
                return
            self._json(
                200,
                {
                    "id": "operator-context",
                    "name": "Stable operating context",
                    "content": "created live content",
                    "last_refreshed_at": _iso(NOW),
                    "is_stale": False,
                },
            )

        def do_POST(self):
            state["methods"].append("POST")
            length = int(self.headers.get("Content-Length") or 0)
            state["post_body"] = json.loads(self.rfile.read(length).decode("utf-8"))
            state["created"] = True
            self._json(
                200,
                {"mental_model_id": "operator-context", "operation_id": "op-1"},
            )

        def do_PATCH(self):
            state["methods"].append("PATCH")
            self._json(500, {"detail": "must never be called"})

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("HINDSIGHT_API_URL", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setenv("HINDSIGHT_BANK_ID", "bank-1")
        monkeypatch.delenv("HINDSIGHT_API_KEY", raising=False)

        # First call: 404 -> create, no content yet, no injection.
        assert core.build_injection(_make_config()) is None
        assert state["created"] is True
        assert state["methods"] == ["GET", "POST"]
        assert state["post_body"]["id"] == "operator-context"
        assert state["post_body"]["trigger"]["refresh_cron"] == "0 3 * * *"
        assert "PATCH" not in state["methods"]

        # Second call: the model now exists -> GET returns content, no re-POST.
        text = core.build_injection(_make_config())
        assert text is not None
        assert "created live content" in text
        assert state["methods"] == ["GET", "POST", "GET"]
        assert state["methods"].count("POST") == 1
        assert "PATCH" not in state["methods"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_build_injection_details_exposes_text_and_metadata(
    plugin, core, monkeypatch, tmp_path
):
    _setup_env(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(core, "_http_get_json", _fake_transport(calls))

    config = _make_config(context_note="DEPLOYMENT: test profile")
    details = core.build_injection_details(config)
    text = core.build_injection(config)

    assert details is not None
    assert details["text"] == text
    assert "## Stable operating context" in text
    assert text.startswith("## Stable context")
    assert "DEPLOYMENT: test profile" in text
    assert details["context_note_chars"] == len("DEPLOYMENT: test profile")
    assert details["assembled_chars"] == len(details["text"])
    assert details["truncated"] is False
    assert details["max_chars"] == 6000
    assert details["models"] == [
        {
            "id": "operator-context",
            "name": "Stable operating context",
            "last_refreshed_at": _iso(NOW),
            "is_stale": False,
            "cache_age_s": None,
            "content_chars": len("remember to be terse"),
        }
    ]
    # Model metadata never carries the raw model content.
    assert "content" not in details["models"][0]


def test_build_injection_details_reports_truncation(
    plugin, core, monkeypatch, tmp_path
):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        core, "_http_get_json", _fake_transport([], content="x" * 2000)
    )

    details = core.build_injection_details(_make_config(max_chars=300))
    assert details is not None
    assert details["truncated"] is True
    assert details["max_chars"] == 300
    assert len(details["text"]) <= 300
    assert details["models"][0]["content_chars"] == 2000


def test_record_injection_persists_content_and_never_the_api_key(
    plugin, core, monkeypatch, tmp_path
):
    data_dir = _setup_env(monkeypatch, tmp_path)
    monkeypatch.setenv("HINDSIGHT_API_KEY", "sk-AUDIT-SECRET")

    summary = core.record_injection(
        _audit_details(),
        _make_config(),
        session_id="live-1",
        task_id="task-1",
        turn_id="turn-1",
        user_message="please remember this question",
        is_first_turn=True,
        platform="chat",
        model="gpt-test",
    )

    assert summary is not None
    assert summary["session_key"] == "task-1"
    assert summary["entry_count"] == 1

    audit_path = data_dir / "injection-audit.json"
    raw = audit_path.read_text("utf-8")
    assert "sk-AUDIT-SECRET" not in raw
    assert "remember to be terse" in raw
    assert "please remember this question" in raw

    history = core.read_injection_history("live-1")
    assert history["session"]["entry_count"] == 1
    entry = history["entries"][0]
    assert entry["turn_id"] == "turn-1"
    assert entry["task_id"] == "task-1"
    assert entry["content"] == _audit_details()["text"]
    assert entry["content_recorded"] is True
    assert entry["user_preview"] == "please remember this question"
    assert entry["models"][0]["name"] == "Stable operating context"

    stats = core.injection_audit_stats()
    assert stats == {
        "schema": core.AUDIT_SCHEMA,
        "session_count": 1,
        "entry_count": 1,
        "file_present": True,
    }


def test_audit_matches_rotated_session_id_by_task_alias(
    plugin, core, monkeypatch, tmp_path
):
    _setup_env(monkeypatch, tmp_path)

    core.record_injection(
        _audit_details("first injection"),
        _make_config(),
        session_id="live-before-compression",
        task_id="durable-task",
        is_first_turn=True,
    )
    core.record_injection(
        _audit_details("second injection"),
        _make_config(),
        session_id="live-after-compression",
        task_id="durable-task",
        is_first_turn=False,
    )

    by_new_live_id = core.read_injection_history("live-after-compression")
    assert by_new_live_id["session"]["entry_count"] == 2
    assert [entry["content"] for entry in by_new_live_id["entries"]] == [
        "second injection",
        "first injection",
    ]

    by_old_live_id = core.read_injection_history("live-before-compression")
    assert by_old_live_id["session"]["entry_count"] == 2

    by_task_id = core.read_injection_history("durable-task")
    assert by_task_id["session"]["entry_count"] == 2
    assert "live-before-compression" in by_task_id["session"]["aliases"]
    assert "live-after-compression" in by_task_id["session"]["aliases"]


def test_record_content_off_keeps_metadata_only(
    plugin, core, monkeypatch, tmp_path
):
    data_dir = _setup_env(monkeypatch, tmp_path)
    config = _make_config(record_content=False)

    summary = core.record_injection(
        _audit_details("very private injected text"),
        config,
        session_id="s-private",
        task_id="task-private",
        user_message="very private user question",
        is_first_turn=True,
    )
    assert summary is not None

    raw = (data_dir / "injection-audit.json").read_text("utf-8")
    assert "very private injected text" not in raw
    assert "very private user question" not in raw

    entry = core.read_injection_history("task-private")["entries"][0]
    assert entry["content"] is None
    assert entry["content_recorded"] is False
    assert entry["user_preview"] == ""
    assert entry["injected_chars"] == len("very private injected text")
    assert entry["models"][0]["name"] == "Stable operating context"


def test_audit_retention_per_session_and_total(
    plugin, core, monkeypatch, tmp_path
):
    _setup_env(monkeypatch, tmp_path)

    per_session = _make_config(audit_max_entries=10, audit_max_per_session=2)
    for index in range(3):
        core.record_injection(
            _audit_details(f"session-one-{index}"),
            per_session,
            session_id="s-one",
            task_id="t-one",
        )
    one = core.read_injection_history("t-one")
    assert [entry["content"] for entry in one["entries"]] == [
        "session-one-2",
        "session-one-1",
    ]

    total = _make_config(
        audit_max_entries=2, audit_max_per_session=10, audit_max_sessions=10
    )
    core.record_injection(
        _audit_details("two-a"), total, session_id="s-two", task_id="t-two"
    )
    core.record_injection(
        _audit_details("two-b"), total, session_id="s-two", task_id="t-two"
    )
    core.record_injection(
        _audit_details("three-a"), total, session_id="s-three", task_id="t-three"
    )

    stats = core.injection_audit_stats()
    assert stats["entry_count"] == 2
    sessions = core.list_injection_sessions()["sessions"]
    assert [session["session_id"] for session in sessions] == ["s-three", "s-two"]
    assert core.read_injection_history("t-two")["session"]["entry_count"] == 1
    assert core.read_injection_history("t-three")["session"]["entry_count"] == 1


def test_audit_retention_session_cap(
    plugin, core, monkeypatch, tmp_path
):
    _setup_env(monkeypatch, tmp_path)
    config = _make_config(
        audit_max_entries=10, audit_max_per_session=10, audit_max_sessions=2
    )
    for session_id in ("s-old", "s-mid", "s-new"):
        core.record_injection(
            _audit_details(session_id),
            config,
            session_id=session_id,
            task_id=f"task-{session_id}",
        )

    sessions = core.list_injection_sessions()["sessions"]
    assert [session["session_id"] for session in sessions] == ["s-new", "s-mid"]
    assert core.read_injection_history("s-old")["entries"] == []


def test_malformed_audit_fails_open_and_is_repaired(
    plugin, core, monkeypatch, tmp_path
):
    data_dir = _setup_env(monkeypatch, tmp_path)
    data_dir.mkdir(parents=True)
    audit_path = data_dir / "injection-audit.json"
    audit_path.write_text('{"sessions": "not-a-map"}', "utf-8")

    assert core.read_injection_history("missing")["entries"] == []
    assert core.injection_audit_stats()["session_count"] == 0

    summary = core.record_injection(
        _audit_details(),
        _make_config(),
        session_id="s-repaired",
        task_id="task-repaired",
    )
    assert summary is not None
    assert core.injection_audit_stats()["entry_count"] == 1
    assert json.loads(audit_path.read_text("utf-8"))["schema"] == core.AUDIT_SCHEMA


def test_hook_records_audit_and_broadcasts_metadata_only(
    plugin, core, monkeypatch, tmp_path
):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.setattr(core, "_http_get_json", _fake_transport([]))
    broadcasts = []
    monkeypatch.setattr(
        plugin,
        "_broadcast_injection_recorded",
        lambda summary: broadcasts.append(summary),
    )

    state = plugin._State(_make_config())
    result = plugin._hook(
        state,
        session_id="live-hook",
        task_id="task-hook",
        turn_id="turn-hook",
        user_message="hook user question",
        is_first_turn=True,
        platform="chat",
        model="gpt-hook",
    )

    assert result is not None
    assert "remember to be terse" in result["context"]
    assert broadcasts and "content" not in broadcasts[0]
    assert "context" not in broadcasts[0]

    entry = core.read_injection_history("task-hook")["entries"][0]
    assert entry["session_id"] == "live-hook"
    assert entry["task_id"] == "task-hook"
    assert entry["turn_id"] == "turn-hook"
    assert entry["user_preview"] == "hook user question"
    assert entry["content"] == result["context"]


def test_hook_survives_audit_failure(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.setattr(core, "_http_get_json", _fake_transport([]))

    def broken_audit(*args, **kwargs):
        raise RuntimeError("audit disk is on fire")

    monkeypatch.setattr(core, "record_injection", broken_audit)

    state = plugin._State(_make_config())
    result = plugin._hook(
        state, session_id="s-audit-fail", is_first_turn=True, platform="chat"
    )
    assert result is not None
    assert "remember to be terse" in result["context"]


def test_broadcast_injection_event_payload_is_content_free(plugin, monkeypatch):
    import sys
    import types

    calls = []
    events = types.ModuleType("hermes_cli.plugin_events")
    events.broadcast_plugin_event = (
        lambda plugin_id, event_name, payload: calls.append(
            (plugin_id, event_name, payload)
        )
    )
    package = types.ModuleType("hermes_cli")
    package.__path__ = []
    package.plugin_events = events
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.plugin_events", events)

    plugin._broadcast_injection_recorded(
        {
            "entry_id": "entry-1",
            "session_id": "live-1",
            "task_id": "task-1",
            "session_key": "task-1",
            "recorded_at": _iso(NOW),
            "entry_count": 1,
            "content": "must not travel on the event bus",
        }
    )

    assert len(calls) == 1
    plugin_id, event_name, payload = calls[0]
    assert plugin_id == "hermes-hindsight-plus"
    assert event_name == "injection.recorded"
    assert payload == {
        "entry_id": "entry-1",
        "session_id": "live-1",
        "task_id": "task-1",
        "session_key": "task-1",
        "recorded_at": _iso(NOW),
        "entry_count": 1,
    }


def test_dashboard_api_reads_the_same_audit(
    plugin, core, monkeypatch, tmp_path
):
    import importlib.util
    import pathlib
    import sys
    import types

    _setup_env(monkeypatch, tmp_path)
    assert (
        core.record_injection(
            _audit_details(),
            _make_config(),
            session_id="dash-live",
            task_id="dash-task",
            user_message="dashboard question",
            is_first_turn=True,
        )
        is not None
    )

    class FakeRouter:
        def __init__(self):
            self.routes = {}

        def get(self, path):
            def decorator(fn):
                self.routes[path] = fn
                return fn

            return decorator

        post = get

    class FakeHTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fake_fastapi = types.ModuleType("fastapi")
    fake_fastapi.APIRouter = FakeRouter
    fake_fastapi.Query = lambda default=None, **kwargs: default
    fake_fastapi.Body = lambda default=None, **kwargs: default
    fake_fastapi.HTTPException = FakeHTTPException
    monkeypatch.setitem(sys.modules, "fastapi", fake_fastapi)
    sys.modules.pop("hermes_hindsight_plus_dashboard_core", None)

    api_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "dashboard"
        / "plugin_api.py"
    )
    spec = importlib.util.spec_from_file_location(
        "hermes_hindsight_plus_dashboard_api_test", api_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    try:
        assert module.router.routes["/health"] is module.health
        assert module.health()["entry_count"] == 1

        history = module.history(session_id="dash-task", limit=200)
        assert history["session"]["entry_count"] == 1
        assert history["entries"][0]["content"] == _audit_details()["text"]
        assert history["entries"][0]["user_preview"] == "dashboard question"

        sessions = module.sessions(limit=10)
        assert sessions["sessions"][0]["key"] == "dash-task"
    finally:
        sys.modules.pop("hermes_hindsight_plus_dashboard_core", None)


# ==== project bank =========================================================

PROJECT_BANK = "coding-agent::demo-repo"


def _project_transport(
    calls,
    project_models=None,
    project_status=None,
    project_bank=PROJECT_BANK,
    fail=False,
):
    """Transport for the configured bank plus one project bank.

    Routes on the exact bank segment, so ``coding-agent::demo-repo`` and
    ``coding-agent::other`` cannot be confused. ``project_status`` simulates a
    failing project bank (an HTTP status code); ``fail`` makes every call fail.
    """

    models = (
        {"conventions": "prefer terse code", "decisions": "never PATCH a model"}
        if project_models is None
        else project_models
    )
    project_segment = f"/banks/{urllib.parse.quote(project_bank, safe='')}/"

    def transport(url, timeout_s, api_key):
        calls.append(url)
        if fail:
            raise urllib.error.HTTPError(url, 503, "offline", {}, None)
        if project_segment in url:
            if project_status is not None:
                raise urllib.error.HTTPError(url, project_status, "nope", {}, None)
            if url.rstrip("/").endswith("/mental-models"):
                return {"items": [{"id": name, "name": name} for name in models]}
            for model_id, content in models.items():
                encoded = urllib.parse.quote(model_id, safe="")
                if f"/mental-models/{encoded}?detail=content" in url:
                    return {
                        "content": content,
                        "name": model_id,
                        "last_refreshed_at": _iso(NOW),
                        "is_stale": False,
                    }
            raise AssertionError(f"unexpected project URL: {url}")
        if "/banks/bank-1/" in url:
            if url.rstrip("/").endswith("/mental-models"):
                return {
                    "items": [
                        {"id": "operator-context", "name": "Stable operating context"}
                    ]
                }
            if "/mental-models/operator-context" in url:
                return {
                    "content": "remember to be terse",
                    "name": "Stable operating context",
                    "last_refreshed_at": _iso(NOW),
                    "is_stale": False,
                }
        raise AssertionError(f"unexpected URL: {url}")

    return transport


def _project_config(**overrides):
    return _make_config(project_bank_template="coding-agent::{project}", **overrides)


def _repo_dir(tmp_path, name="demo-repo"):
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


def test_repo_name_repo_root_and_nested_dir(core, tmp_path):
    repo = _repo_dir(tmp_path)
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)

    assert core.repo_name(repo) == "demo-repo"
    assert core.repo_name(nested) == "demo-repo"
    assert core.repo_name(repo / ".git" / "HEAD") == "demo-repo"


def test_repo_name_from_worktree_git_file(core, tmp_path):
    repo = _repo_dir(tmp_path, "real-repo")
    worktree = tmp_path / "feature-worktree"
    gitdir = repo / ".git" / "worktrees" / "feature"
    gitdir.mkdir(parents=True)
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {gitdir.as_posix()}\n", "utf-8")

    assert core.repo_name(worktree) == "real-repo"


def test_repo_name_from_relative_worktree_gitdir(core, tmp_path):
    repo = _repo_dir(tmp_path, "real-repo")
    (repo / ".git" / "worktrees" / "feature").mkdir(parents=True)
    worktree = tmp_path / "feature"
    worktree.mkdir()
    (worktree / ".git").write_text(
        "gitdir: ../real-repo/.git/worktrees/feature\n", "utf-8"
    )

    assert core.repo_name(worktree) == "real-repo"


def test_repo_name_from_bare_gitdir(core, tmp_path):
    bare = tmp_path / "srv" / "project.git"
    bare.mkdir(parents=True)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").write_text(f"gitdir: {bare.as_posix()}\n", "utf-8")

    assert core.repo_name(checkout) == "project"


def test_repo_name_keeps_walking_past_an_unparseable_git_file(core, tmp_path):
    repo = _repo_dir(tmp_path, "outer-repo")
    inner = repo / "inner"
    inner.mkdir()
    (inner / ".git").write_text("this is not a gitdir line\n", "utf-8")

    assert core.repo_name(inner) == "outer-repo"


def test_repo_name_returns_empty_outside_a_repo(core, tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    assert core.repo_name(plain) == ""
    assert core.repo_name(tmp_path / "missing") == ""
    assert core.repo_name(None) == ""


def test_project_bank_id_substitutes_project_only(core):
    assert (
        core.project_bank_id("coding-agent::{project}", "demo-repo")
        == "coding-agent::demo-repo"
    )
    # Only {project} is known to this plugin; other placeholders stay literal.
    assert (
        core.project_bank_id("{profile}::{project}", "demo-repo")
        == "{profile}::demo-repo"
    )
    assert (
        core.project_bank_id("  coding-agent::{project}  ", "demo-repo")
        == "coding-agent::demo-repo"
    )


def test_project_bank_id_empty_inputs_return_empty(core):
    assert core.project_bank_id("", "demo-repo") == ""
    assert core.project_bank_id("   ", "demo-repo") == ""
    assert core.project_bank_id("coding-agent::{project}", "") == ""
    assert core.project_bank_id(None, "demo-repo") == ""


def test_project_section_appended_when_settings_are_set(
    plugin, core, monkeypatch, tmp_path
):
    _setup_env(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(core, "_http_get_json", _project_transport(calls))
    repo = _repo_dir(tmp_path)

    state = plugin._State(_project_config())
    result = plugin._hook(
        state, session_id="p1", is_first_turn=True, platform="chat", cwd=str(repo)
    )

    assert result is not None
    text = result["context"]
    assert "## Project memory (demo-repo)" in text
    assert text.index("## Stable operating context") < text.index("## Project memory")
    assert text.index("## Project memory") < text.index("### conventions")
    assert "These facts apply only when working in demo-repo." in text
    assert "prefer terse code" in text
    # The project bank was addressed by the templated id, not the configured one.
    assert any("/banks/coding-agent%3A%3Ademo-repo/" in url for url in calls)


def test_project_section_absent_when_template_is_empty(
    plugin, core, monkeypatch, tmp_path
):
    _setup_env(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(core, "_http_get_json", _project_transport(calls))
    repo = _repo_dir(tmp_path)

    today = core.build_injection(_make_config())
    result = plugin._hook(
        plugin._State(_make_config(project_bank_template="")),
        session_id="p2",
        is_first_turn=True,
        platform="chat",
        cwd=str(repo),
    )

    assert result is not None
    assert result["context"] == today
    assert "Project memory" not in today
    assert all("coding-agent" not in url for url in calls)


def test_project_bank_404_is_swallowed_and_never_creates(
    plugin, core, monkeypatch, tmp_path
):
    _setup_env(monkeypatch, tmp_path)
    calls = []
    posts = []
    monkeypatch.setattr(
        core, "_http_get_json", _project_transport(calls, project_status=404)
    )

    def post(url, timeout_s, api_key, payload):
        posts.append(url)
        raise AssertionError("the project bank must never be written to")

    monkeypatch.setattr(core, "_http_post_json", post)

    text = core.build_injection(
        _project_config(project_mental_model_ids=["conventions"]),
        project={"name": "demo-repo", "bank": PROJECT_BANK},
    )

    assert text is not None
    assert "remember to be terse" in text
    assert "Project memory" not in text
    assert posts == []
    # The project bank really was attempted, and refused without a write.
    assert any("/banks/coding-agent%3A%3Ademo-repo/" in url for url in calls)
    # A dead project bank leaves the injection exactly as it is today.
    assert text == core.build_injection(_make_config())


def test_project_uses_every_model_when_ids_empty(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    calls = []
    models = {"conventions": "prefer terse code", "decisions": "never PATCH a model"}
    monkeypatch.setattr(
        core, "_http_get_json", _project_transport(calls, project_models=models)
    )

    details = core.build_injection_details(
        _project_config(), project={"name": "demo-repo", "bank": PROJECT_BANK}
    )

    assert details is not None
    text = details["text"]
    assert text.count("## Project memory (demo-repo)") == 1
    assert "### conventions" in text
    assert "### decisions" in text
    assert [item["id"] for item in details["project"]["models"]] == [
        "conventions",
        "decisions",
    ]
    assert details["project"]["bank"] == PROJECT_BANK


def test_project_section_respects_max_chars(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        core,
        "_http_get_json",
        _project_transport(
            [],
            project_models={
                "conventions": "short project fact",
                "decisions": "d" * 1000,
            },
        ),
    )

    details = core.build_injection_details(
        _project_config(max_chars=600),
        project={"name": "demo-repo", "bank": PROJECT_BANK},
    )

    assert details is not None
    assert details["truncated"] is True
    assert len(details["text"]) <= 600
    # The main model body survives; the project block is cut at a section edge.
    assert "remember to be terse" in details["text"]
    assert "## Project memory (demo-repo)" in details["text"]
    assert "### conventions" in details["text"]
    assert "### decisions" not in details["text"]
    assert [item["id"] for item in details["project"]["models"]] == ["conventions"]
    # Never end on a bare heading: the last line is not a group or model heading.
    assert not details["text"].rstrip().endswith("demo-repo)")


def test_project_block_is_dropped_when_no_section_fits(
    plugin, core, monkeypatch, tmp_path
):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.setattr(
        core,
        "_http_get_json",
        _project_transport([], project_models={"conventions": "c" * 2000}),
    )

    details = core.build_injection_details(
        _project_config(max_chars=600),
        project={"name": "demo-repo", "bank": PROJECT_BANK},
    )

    assert details is not None
    assert len(details["text"]) <= 600
    assert "remember to be terse" in details["text"]
    assert "## Project memory" not in details["text"]
    assert "project" not in details


def test_project_ids_cached_and_used_offline(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(core, "_http_get_json", _project_transport(calls))
    # A zero TTL forces every read to refresh, so the second call exercises the
    # stale fallback instead of a fresh in-TTL hit.
    config = _project_config(cache_ttl_s=0)
    project = {"name": "demo-repo", "bank": PROJECT_BANK}

    online = core.build_injection(config, project=project)
    assert online is not None
    assert "## Project memory (demo-repo)" in online

    # The bank is now unreachable: the cached id list and model content carry it.
    monkeypatch.setattr(core, "_http_get_json", _project_transport(calls, fail=True))
    offline = core.build_injection(config, project=project)

    assert offline is not None
    assert "## Project memory (demo-repo)" in offline
    assert "prefer terse code" in offline
    # The id list was discovered before the outage.
    assert any(
        "/banks/coding-agent%3A%3Ademo-repo/mental-models" in url for url in calls
    )


def test_audit_carries_the_project_bank_and_repo(plugin, core, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.setattr(core, "_http_get_json", _project_transport([]))

    details = core.build_injection_details(
        _project_config(), project={"name": "demo-repo", "bank": PROJECT_BANK}
    )
    assert details is not None
    core.record_injection(
        details, _project_config(), session_id="proj-audit", task_id="proj-task"
    )

    entry = core.read_injection_history("proj-task")["entries"][0]
    assert entry["project"]["bank"] == PROJECT_BANK
    assert entry["project"]["repo"] == "demo-repo"
    assert [item["id"] for item in entry["project"]["models"]] == [
        "conventions",
        "decisions",
    ]
    # Project headings are '###', so the main model count is not inflated.
    assert entry["content"].count("## Project memory (demo-repo)") == 1
    assert "### conventions" in entry["content"]

    # An injection without a project keeps today's entry shape exactly.
    core.record_injection(
        _audit_details(), _make_config(), session_id="no-proj", task_id="no-proj-task"
    )
    plain = core.read_injection_history("no-proj-task")["entries"][0]
    assert "project" not in plain


@pytest.mark.parametrize("bank", ["a/b", "a b", "../../etc", "é::lin", "::x"])
def test_invalid_project_bank_ids_are_rejected(
    plugin, core, monkeypatch, tmp_path, bank
):
    _setup_env(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(core, "_http_get_json", _project_transport(calls))

    text = core.build_injection(
        _project_config(), project={"name": "demo-repo", "bank": bank}
    )

    assert text is not None
    assert "## Stable operating context" in text
    assert "Project memory" not in text
    encoded = urllib.parse.quote(bank, safe="")
    assert all(encoded not in url for url in calls)


def test_static_project_bank_id_needs_no_working_directory(
    plugin, core, monkeypatch, tmp_path
):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.delenv("HINDSIGHT_PROJECT", raising=False)
    calls = []
    static_bank = "coding-agent::static-bank"
    monkeypatch.setattr(
        core, "_http_get_json", _project_transport(calls, project_bank=static_bank)
    )
    config = _make_config(
        project_bank_id=static_bank,
        project_bank_template="coding-agent::{project}",
    )

    project = plugin._project_from_payload(config, {}, session_id="static-1")
    assert project == {"name": static_bank, "bank": static_bank}

    result = plugin._hook(
        plugin._State(config), session_id="static-1", is_first_turn=True, platform="chat"
    )
    assert result is not None
    assert f"## Project memory ({static_bank})" in result["context"]


def test_project_from_payload_reads_each_cwd_alias(plugin, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.delenv("HINDSIGHT_PROJECT", raising=False)
    repo = _repo_dir(tmp_path)
    config = _project_config()

    for index, key in enumerate(
        ("cwd", "workspace", "workspace_path", "working_directory")
    ):
        project = plugin._project_from_payload(
            config, {key: str(repo)}, session_id=f"alias-{index}"
        )
        assert project == {"name": "demo-repo", "bank": PROJECT_BANK}


def test_project_from_payload_reads_env_var(plugin, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    repo = _repo_dir(tmp_path)
    config = _project_config()

    monkeypatch.setenv("HINDSIGHT_PROJECT", str(repo))
    assert plugin._project_from_payload(config, {}, session_id="env-1") == {
        "name": "demo-repo",
        "bank": PROJECT_BANK,
    }

    # A bare repository name is accepted as-is.
    monkeypatch.setenv("HINDSIGHT_PROJECT", "my-checkout")
    assert plugin._project_from_payload(config, {}, session_id="env-2") == {
        "name": "my-checkout",
        "bank": "coding-agent::my-checkout",
    }


def test_project_from_payload_reads_state_db_cwd(plugin, monkeypatch, tmp_path):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.delenv("HINDSIGHT_PROJECT", raising=False)
    repo = _repo_dir(tmp_path)
    conn = sqlite3.connect(str(tmp_path / "state.db"))
    conn.execute("CREATE TABLE sessions (session_id TEXT, platform TEXT, cwd TEXT)")
    conn.execute(
        "INSERT INTO sessions (session_id, platform, cwd) VALUES (?,?,?)",
        ("desk-1", "desktop", str(repo)),
    )
    conn.execute(
        "INSERT INTO sessions (session_id, platform, cwd) VALUES (?,?,?)",
        ("desk-2", "desktop", None),
    )
    conn.commit()
    conn.close()

    assert plugin._project_from_payload(
        _project_config(), {}, session_id="desk-1"
    ) == {"name": "demo-repo", "bank": PROJECT_BANK}
    # A desktop session without a cwd resolves nothing: no project section.
    assert (
        plugin._project_from_payload(_project_config(), {}, session_id="desk-2")
        is None
    )


def test_project_from_payload_rejects_host_directories(
    plugin, core, monkeypatch, tmp_path
):
    _setup_env(monkeypatch, tmp_path)
    monkeypatch.delenv("HINDSIGHT_PROJECT", raising=False)
    config = _project_config()
    plugin_dir = os.path.dirname(os.path.abspath(plugin.__file__))
    # ``plugin_dir`` really is a git checkout; it must still not name a bank.
    assert core.repo_name(plugin_dir) == "hermes-hindsight-plus"

    # The daemon's own directory is the known trap.
    assert (
        plugin._project_from_payload(config, {"cwd": str(tmp_path)}, session_id="h1")
        is None
    )
    assert (
        plugin._project_from_payload(
            config, {"cwd": plugin_dir}, session_id="h2"
        )
        is None
    )
    # UNC paths are rejected outright.
    assert (
        plugin._project_from_payload(
            config, {"cwd": r"\\server\share\repo"}, session_id="h3"
        )
        is None
    )

