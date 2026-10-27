import json
import urllib.error
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
    assert "repli cache" in text


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
