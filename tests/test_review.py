import importlib.util
import json
import pathlib
import socketserver
import sys
import threading
import types
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
SECRET = "sk-test-secret-key"


@pytest.fixture(scope="module")
def review():
    spec = importlib.util.spec_from_file_location("hh_review_under_test", ROOT / "review.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["hh_review_under_test"] = module
    spec.loader.exec_module(module)
    return module


def _iso(days):
    return (NOW - timedelta(days=days)).isoformat()


# --- stub Hindsight ------------------------------------------------------------


class _QuickHTTPServer(HTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind calls socket.getfqdn(), a slow reverse-DNS lookup on
        # some Windows setups; the stub only ever binds loopback.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class StubHindsight:
    """Records every request; serves a tiny in-memory fact store."""

    def __init__(self):
        self.requests = []
        self.memories = {
            "m1": {"id": "m1", "text": "Old fact", "state": "valid", "fact_type": "world",
                   "date": _iso(400), "invalidation_reason": None},
            "m2": {"id": "m2", "text": "Retired fact", "state": "invalidated",
                   "fact_type": "experience", "date": _iso(10),
                   "invalidation_reason": "outdated: gone"},
            "obs": {"id": "obs", "text": "Derived", "state": "valid",
                    "fact_type": "observation", "date": _iso(5), "invalidation_reason": None},
        }
        self.fail_patch = False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _record(self, body=None):
                outer.requests.append({
                    "method": self.command, "path": self.path, "body": body,
                    "auth": self.headers.get("Authorization"),
                })

            def do_GET(self):
                self._record()
                path = self.path.split("?")[0]
                if path.endswith("/memories/list"):
                    items = list(outer.memories.values())
                    return self._send(200, {"items": items, "total": len(items),
                                            "limit": 100, "offset": 0})
                mid = path.rsplit("/", 1)[-1]
                if mid in outer.memories:
                    return self._send(200, outer.memories[mid])
                self._send(404, {"detail": "not found"})

            def do_PATCH(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                self._record(body)
                if outer.fail_patch:
                    return self._send(500, {"detail": "boom " + SECRET})
                mid = self.path.rsplit("/", 1)[-1]
                memory = outer.memories.get(mid)
                if memory is None:
                    return self._send(404, {"detail": "not found"})
                if "text" in body:
                    memory["text"] = body["text"]
                if "state" in body:
                    memory["state"] = body["state"]
                    memory["invalidation_reason"] = body.get("reason")
                self._send(200, {})

            def do_DELETE(self):
                self._record()
                self._send(405, {})

            do_PUT = do_POST = do_DELETE

        self.server = _QuickHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def methods(self):
        return [r["method"] for r in self.requests]


@pytest.fixture()
def stub():
    s = StubHindsight()
    yield s
    s.close()


@pytest.fixture()
def client(review, stub):
    return review.ReviewClient(stub.url, SECRET, timeout_s=5)


# --- staleness scoring ---------------------------------------------------------


def test_age_days_and_score(review):
    assert review.age_days(_iso(400), NOW) == 400
    assert review.age_days("", NOW) is None
    assert review.age_days("not a date", NOW) is None
    assert review.age_days(_iso(-3), NOW) == 0  # future dates clamp to zero
    assert review.age_score(None) == 0.0
    assert review.age_score(0) == 0.0
    assert review.age_score(365) == 1.0
    assert review.age_score(5000) == 1.0
    assert review.age_score(73) == pytest.approx(0.2, abs=0.001)


def test_score_item_exposes_both_numbers(review):
    item = review.score_item(
        {"id": "x", "text": "Server at 10.0.0.5 runs v1.2.3", "date": _iso(200),
         "fact_type": "world", "state": "valid"},
        NOW,
    )
    assert item["age_days"] == 200
    assert item["age_score"] == pytest.approx(0.548, abs=0.001)
    assert item["volatile_score"] > 0
    assert set(item["volatile_hits"]) >= {"ip-address", "version"}
    assert item["staleness"] == pytest.approx((item["age_score"] + item["volatile_score"]) / 2, abs=0.001)
    assert item["curatable"] is True


def test_score_item_falls_back_to_mentioned_at_and_tolerates_junk(review):
    item = review.score_item({"id": "x", "text": "t", "mentioned_at": _iso(30)}, NOW)
    assert item["age_days"] == 30
    assert review.score_item(None, NOW)["age_days"] is None
    assert review.score_item({"fact_type": "observation"}, NOW)["curatable"] is False


def test_sort_by_age_or_volatile_differ(review):
    old_plain = review.score_item({"id": "a", "text": "plain", "date": _iso(300)}, NOW)
    new_volatile = review.score_item(
        {"id": "b", "text": "currently at 192.168.1.5 v2.0.1", "date": _iso(2)}, NOW
    )
    by_age = [i["id"] for i in review.sort_items([new_volatile, old_plain], "age")]
    by_vol = [i["id"] for i in review.sort_items([old_plain, new_volatile], "volatile")]
    assert by_age == ["a", "b"]
    assert by_vol == ["b", "a"]
    with pytest.raises(review.ReviewError):
        review.sort_items([], "bogus")


def test_risk_sorts_volatile_first_then_age_then_staleness(review):
    """``risk`` puts a recent risky fact above an old but textually stable one."""
    risky = review.score_item(
        {"id": "risky", "text": "migration is pending in commit a1b2c3d4e5", "date": _iso(10)},
        NOW,
    )
    old_plain = review.score_item({"id": "old", "text": "plain note", "date": _iso(300)}, NOW)
    assert risky["volatile_score"] == 0.5 and old_plain["volatile_score"] == 0.0

    assert [i["id"] for i in review.sort_items([old_plain, risky], "risk")] == ["risky", "old"]
    # the default ranking is the opposite one: that is the ordering ``risk`` adds
    assert [
        i["id"] for i in review.sort_items([old_plain, risky], "staleness")
    ] == ["old", "risky"]

    # equal volatile score: the older fact goes first
    newer = review.score_item({"id": "newer", "text": "plain note", "date": _iso(100)}, NOW)
    assert [i["id"] for i in review.sort_items([newer, old_plain], "risk")] == ["old", "newer"]

    # hand-built items pin the last key: equal volatile and equal age, staleness decides
    weaker = {"id": "weaker", "volatile_score": 0.5, "age_score": 0.2, "staleness": 0.3}
    stronger = {"id": "stronger", "volatile_score": 0.5, "age_score": 0.2, "staleness": 0.6}
    assert [
        i["id"] for i in review.sort_items([weaker, stronger], "risk")
    ] == ["stronger", "weaker"]


# --- volatile heuristic --------------------------------------------------------


@pytest.mark.parametrize(
    "text,label",
    [
        ("Host is 10.1.2.3", "ip-address"),
        ("Runs version v1.12.4", "version"),
        ("Pinned to 2.0.1-rc1", "version"),
        ("Deployed commit a1b2c3d4e5", "commit-hash"),
        ("Currently using Postgres", "time-relative"),
        ("The migration is pending", "time-relative"),
        ("As of today it works", "time-relative"),
        ("Actuellement en cours", "time-relative"),
        ("See http://192.168.1.10:8080/ui", "lan-url"),
        ("See http://nas.local/share", "lan-url"),
        ("See http://localhost:3000", "lan-url"),
        ("See https://box.tail1234.ts.net/x", "lan-url"),
    ],
)
def test_volatile_rules_fire(review, text, label):
    score, hits = review.volatile_score(text)
    assert label in hits
    assert 0 < score <= 1.0


@pytest.mark.parametrize(
    "text",
    [
        "The user prefers dark mode",
        "Alexandre lives in Lyon",
        "Version control is useful",
        "Public site https://example.com/docs",
        "The word deadbeef-free: just prose",
    ],
)
def test_stable_text_scores_zero(review, text):
    assert review.volatile_score(text) == (0.0, [])


def test_pure_letter_or_digit_words_are_not_commit_hashes(review):
    assert "commit-hash" not in review.volatile_score("decade defaced 12345678")[1]


def test_volatile_score_is_capped_at_one(review):
    score, hits = review.volatile_score(
        "currently http://10.0.0.1/x at 10.0.0.1 v1.2.3 commit abc1234def"
    )
    assert score == 1.0
    assert len(hits) >= 4


# --- validation ----------------------------------------------------------------


def test_reason_must_be_superseded_or_outdated(review):
    assert review.check_reason("superseded by m9") == "superseded by m9"
    assert review.check_reason(" Outdated: moved ") == "Outdated: moved"
    for bad in ("", "nope", "outdated:", "superseded by ", None, "x" * 600):
        with pytest.raises(review.ReviewError) as exc:
            review.check_reason(bad)
        assert exc.value.status == 400


def test_bank_and_id_allowlists(review):
    assert review.check_bank("atlas") == "atlas"
    with pytest.raises(review.ReviewError) as exc:
        review.check_bank("prod")
    assert exc.value.status == 404
    for bad in ("../x", "a/b", "", "a b", "x" * 200, 5):
        with pytest.raises(review.ReviewError):
            review.check_memory_id(bad)


# --- list ----------------------------------------------------------------------


def test_list_review_scores_sorts_and_sends_bearer(review, client, stub):
    result = review.list_review(client, "hermes", limit=50, sort="age", now=NOW)
    assert result["bank"] == "hermes" and result["total"] == 3
    assert result["items"][0]["id"] == "m1"  # oldest first
    req = stub.requests[0]
    assert req["method"] == "GET"
    assert "/v1/default/banks/hermes/memories/list" in req["path"]
    # The upstream page size is the scan window, not the display ``limit``: the
    # ranking has to see the whole bank before ``limit`` slices it.
    assert f"limit={review.MAX_PAGE}" in req["path"] and "state=valid" in req["path"]
    assert req["auth"] == "Bearer " + SECRET
    assert SECRET not in json.dumps(result)


def test_list_review_rejects_bad_params(review, client):
    for kwargs in ({"limit": 0}, {"limit": 201}, {"offset": -1}, {"state": "x"}):
        with pytest.raises(review.ReviewError):
            review.list_review(client, "hermes", **kwargs)
    with pytest.raises(review.ReviewError):
        review.list_review(client, "nope")


# --- revoke / restore / update -------------------------------------------------


def test_revoke_is_a_soft_patch_and_returns_previous(review, client, stub):
    out = review.revoke(client, "hermes", "m1", "outdated: replaced by new box")
    patches = [r for r in stub.requests if r["method"] == "PATCH"]
    assert len(patches) == 1
    assert patches[0]["body"] == {"state": "invalidated", "reason": "outdated: replaced by new box"}
    assert patches[0]["path"].endswith("/v1/default/banks/hermes/memories/m1")
    assert patches[0]["auth"] == "Bearer " + SECRET
    assert out["previous"]["state"] == "valid"
    assert out["current"]["state"] == "invalidated"
    assert out["current"]["invalidation_reason"] == "outdated: replaced by new box"
    assert "DELETE" not in stub.methods()


def test_revoke_undo_round_trip_restores_previous_state(review, client, stub):
    revoked = review.revoke(client, "hermes", "m1", "superseded by m2")
    restored = review.restore(client, "hermes", "m1")
    assert revoked["previous"]["state"] == "valid"
    assert restored["previous"]["state"] == "invalidated"
    assert restored["previous"]["invalidation_reason"] == "superseded by m2"
    assert restored["current"]["state"] == "valid"
    assert stub.memories["m1"]["state"] == "valid"
    assert {r["method"] for r in stub.requests} <= {"GET", "PATCH"}


def test_restore_sets_valid_state(review, client, stub):
    out = review.restore(client, "atlas", "m2")
    patch = next(r for r in stub.requests if r["method"] == "PATCH")
    assert patch["body"] == {"state": "valid"}
    assert "/banks/atlas/" in patch["path"]
    assert out["previous"]["state"] == "invalidated"
    assert out["current"]["state"] == "valid"


def test_update_rewrites_text_only_and_returns_previous_text(review, client, stub):
    out = review.update(client, "hermes", "m1", "  New fact text  ")
    patch = next(r for r in stub.requests if r["method"] == "PATCH")
    assert patch["body"] == {"text": "New fact text"}
    assert "state" not in patch["body"]
    assert out["previous"]["text"] == "Old fact"
    assert out["current"]["text"] == "New fact text"
    # undo = update back to previous text
    review.update(client, "hermes", "m1", out["previous"]["text"])
    assert stub.memories["m1"]["text"] == "Old fact"


def test_state_preconditions_and_observations_are_refused(review, client, stub):
    with pytest.raises(review.ReviewError) as exc:
        review.revoke(client, "hermes", "m2", "outdated: x")  # already invalidated
    assert exc.value.status == 409
    with pytest.raises(review.ReviewError) as exc:
        review.restore(client, "hermes", "m1")  # already valid
    assert exc.value.status == 409
    with pytest.raises(review.ReviewError) as exc:
        review.update(client, "hermes", "obs", "x")  # observations are derived
    assert exc.value.status == 400
    assert "PATCH" not in stub.methods()


def test_invalid_input_makes_no_http_call(review, client, stub):
    for call in (
        lambda: review.revoke(client, "hermes", "m1", "because"),
        lambda: review.update(client, "hermes", "m1", "   "),
        lambda: review.update(client, "hermes", "m1", "x" * 5000),
        lambda: review.revoke(client, "bad-bank", "m1", "outdated: x"),
        lambda: review.restore(client, "hermes", "../m1"),
    ):
        with pytest.raises(review.ReviewError):
            call()
    assert stub.requests == []


def test_upstream_error_is_mapped_and_never_leaks_key_or_body(review, client, stub):
    stub.fail_patch = True
    with pytest.raises(review.ReviewError) as exc:
        review.revoke(client, "hermes", "m1", "outdated: x")
    assert exc.value.status == 502
    assert SECRET not in exc.value.message
    assert "boom" not in exc.value.message
    assert stub.memories["m1"]["state"] == "valid"


def test_unknown_memory_is_404(review, client):
    with pytest.raises(review.ReviewError) as exc:
        review.revoke(client, "hermes", "nope", "outdated: x")
    assert exc.value.status == 404


def test_unreachable_hindsight_is_502_and_unconfigured_is_503(review):
    dead = review.ReviewClient("http://127.0.0.1:9", SECRET, timeout_s=1)
    with pytest.raises(review.ReviewError) as exc:
        dead.list_memories("hermes", 1, 0)
    assert exc.value.status == 502
    with pytest.raises(review.ReviewError) as exc:
        review.ReviewClient("", "").list_memories("hermes", 1, 0)
    assert exc.value.status == 503


def test_client_never_issues_delete_or_put(review):
    source = (ROOT / "review.py").read_text("utf-8")
    assert '"DELETE"' not in source
    assert '"PUT"' not in source
    assert source.count('"POST"') == 1
    assert "def create_memory" in source
    # the single POST lives inside create_memory
    post_index = source.index('"POST"')
    create_index = source.index("def create_memory")
    assert post_index > create_index
    assert "/memories" in source


def test_create_memory_body(review, monkeypatch):
    import json

    captured = {}

    class FakeClient:
        def _request(self, method, path, payload=None):
            captured["method"] = method
            captured["path"] = path
            captured["payload"] = payload
            return {"success": True, "bank_id": "hermes", "items_count": 1, "async": False}

        @staticmethod
        def _base(bank):
            return "/v1/default/banks/" + bank

    client = FakeClient()
    result = review.create_memory(client, "hermes", "hello", context="ctx", tags=["t1"])
    assert captured["method"] == "POST"
    assert "memories" in captured["path"]
    payload = captured["payload"]
    assert isinstance(payload["items"], list) and len(payload["items"]) == 1
    assert payload["async"] is False
    item = payload["items"][0]
    assert set(item.keys()) == {"content", "context", "tags"}
    assert item["content"] == "hello"
    assert item["context"] == "ctx"
    assert item["tags"] == ["t1"]

    # without context/tags, item carries only content
    captured2 = {}

    class FakeClient2:
        def _request(self, method, path, payload=None):
            captured2["payload"] = payload
            return {"success": True, "bank_id": "hermes", "items_count": 1, "async": False}

        @staticmethod
        def _base(bank):
            return "/v1/default/banks/" + bank

    review.create_memory(FakeClient2(), "hermes", "world")
    item2 = captured2["payload"]["items"][0]
    assert set(item2.keys()) == {"content"}


# --- check with Hermes ---------------------------------------------------------


def test_check_prompt_lists_facts_and_forbids_edits(review):
    prompt = review.build_check_prompt(
        [{"id": "m1", "text": "Server is at 10.0.0.5"}, {"id": "m2", "text": "Uses v2"}]
    )
    assert "[m1] Server is at 10.0.0.5" in prompt and "[m2] Uses v2" in prompt
    assert "still true / outdated / cannot tell" in prompt
    assert "Do not modify, delete or invalidate" in prompt


def test_check_prompt_enforces_one_to_ten(review):
    with pytest.raises(review.ReviewError):
        review.build_check_prompt([])
    with pytest.raises(review.ReviewError):
        review.build_check_prompt([{"id": f"m{i}", "text": "t"} for i in range(11)])
    review.build_check_prompt([{"id": f"m{i}", "text": "t"} for i in range(10)])
    with pytest.raises(review.ReviewError):
        review.build_check_prompt([{"id": "bad id", "text": "t"}])


def test_parse_verdicts_reads_last_json_block_and_filters(review):
    reply = (
        "m1 is outdated.\n```json\n[{\"id\": \"m1\", \"verdict\": \"Outdated\", "
        "\"evidence\": \"ping fails\"}, {\"id\": \"m2\", \"verdict\": \"maybe\"}, "
        "{\"id\": \"zzz\", \"verdict\": \"still true\"}]\n```"
    )
    out = review.parse_verdicts(reply, ["m1", "m2"])
    assert out == {"m1": {"verdict": "outdated", "evidence": "ping fails"}}
    assert review.parse_verdicts("no json here", ["m1"]) == {}
    assert review.parse_verdicts("```json\n{broken\n```", ["m1"]) == {}


# --- dashboard routes ----------------------------------------------------------


def _load_api(monkeypatch):
    class FakeRouter:
        def __init__(self):
            self.routes = {}

        def _reg(self, path):
            def decorator(fn):
                self.routes[path] = fn
                return fn

            return decorator

        get = post = _reg

    class FakeHTTPException(Exception):
        def __init__(self, status_code, detail):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fake = types.ModuleType("fastapi")
    fake.APIRouter = FakeRouter
    fake.Query = lambda default=None, **kwargs: default
    fake.Body = lambda default=None, **kwargs: default
    fake.HTTPException = FakeHTTPException
    monkeypatch.setitem(sys.modules, "fastapi", fake)
    for name in ("hermes_hindsight_plus_dashboard_core", "hermes_hindsight_plus_dashboard_review"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        "hh_dashboard_api_review_test", ROOT / "dashboard" / "plugin_api.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, FakeHTTPException


def test_dashboard_review_routes_end_to_end(stub, monkeypatch, tmp_path):
    hermes_home = tmp_path / "home"
    (hermes_home / "hindsight").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HINDSIGHT_API_URL", stub.url)
    monkeypatch.setenv("HINDSIGHT_API_KEY", SECRET)
    module, http_exc = _load_api(monkeypatch)
    try:
        listing = module.review_list("hermes", limit=100, offset=0, state="valid",
                                     sort="staleness", q="")
        assert listing["items"][0]["id"] == "m1"
        assert SECRET not in json.dumps(listing)

        revoked = module.review_revoke("hermes", "m1", {"reason": "outdated: gone"})
        assert revoked["previous"]["state"] == "valid"
        assert SECRET not in json.dumps(revoked)
        restored = module.review_restore("hermes", "m1")
        assert restored["current"]["state"] == "valid"
        updated = module.review_update("hermes", "m1", {"text": "Newer"})
        assert updated["previous"]["text"] == "Old fact"

        with pytest.raises(http_exc) as exc:
            module.review_revoke("hermes", "m1", {"reason": "nope"})
        assert exc.value.status_code == 400
        with pytest.raises(http_exc) as exc:
            module.review_list("prod", limit=10, offset=0, state="valid", sort="age", q="")
        assert exc.value.status_code == 404

        prompt = module.review_check_prompt({"facts": [{"id": "m1", "text": "t"}]})
        assert "[m1] t" in prompt["prompt"]
        verdicts = module.review_verdicts(
            {"ids": ["m1"], "reply": '```json\n[{"id":"m1","verdict":"cannot tell"}]\n```'}
        )
        assert verdicts["verdicts"]["m1"]["verdict"] == "cannot tell"

        assert "DELETE" not in stub.methods()
        assert all(r["auth"] == "Bearer " + SECRET for r in stub.requests)
        assert not any(SECRET in p.read_text("utf-8", errors="ignore")
                       for p in tmp_path.rglob("*") if p.is_file())
    finally:
        for name in ("hermes_hindsight_plus_dashboard_core", "hermes_hindsight_plus_dashboard_review"):
            sys.modules.pop(name, None)


# --- global ranking / work queue -----------------------------------------------


def test_dashboard_review_list_accepts_sort_risk_and_volatile_only(stub, monkeypatch, tmp_path):
    """The route forwards ``sort=risk`` and ``volatile_only``; an unknown sort is a 400."""
    hermes_home = tmp_path / "home"
    (hermes_home / "hindsight").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HINDSIGHT_API_URL", stub.url)
    monkeypatch.setenv("HINDSIGHT_API_KEY", SECRET)
    stub.memories["m1"]["text"] = "host is 10.0.0.5"  # volatile 0.35, unique in the stub
    module, http_exc = _load_api(monkeypatch)
    try:
        ranked = module.review_list("hermes", limit=100, offset=0, state="valid",
                                    sort="risk", q="")
        assert ranked["sort"] == "risk"
        assert [i["id"] for i in ranked["items"]] == ["m1", "m2", "obs"]
        assert SECRET not in json.dumps(ranked)

        risky_only = module.review_list(
            "hermes", limit=100, offset=0, state="valid", sort="risk", q="",
            volatile_only=True,
        )
        assert [i["id"] for i in risky_only["items"]] == ["m1"]
        assert risky_only["matched"] == 1
        assert risky_only["to_review"] == 3  # the queue size is not narrowed by the flag

        with pytest.raises(http_exc) as exc:
            module.review_list("hermes", limit=100, offset=0, state="valid",
                               sort="bogus", q="")
        assert exc.value.status_code == 400
        assert exc.value.detail == "sort must be one of: age, volatile, staleness, risk"
    finally:
        for name in ("hermes_hindsight_plus_dashboard_core",
                     "hermes_hindsight_plus_dashboard_review"):
            sys.modules.pop(name, None)


class QueueHindsight:
    """A paging-aware stub: honours ``limit``/``offset`` and records every request."""

    def __init__(self, facts, total=None, ignore_offset=False, report_total=True,
                 page_cap=None):
        self.facts = list(facts)
        self.total = total                  # override the reported total when not None
        self.ignore_offset = ignore_offset  # a server that never advances past page 1
        self.report_total = report_total    # a server that omits `total` entirely
        self.page_cap = page_cap            # a server that ignores `limit` downwards
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append({
                    "method": self.command, "path": self.path,
                    "auth": self.headers.get("Authorization"),
                })
                parsed = urllib.parse.urlparse(self.path)
                if not parsed.path.endswith("/memories/list"):
                    return self._send(404, {"detail": "not found"})
                query = urllib.parse.parse_qs(parsed.query)
                limit = int(query.get("limit", ["100"])[0])
                if outer.page_cap is not None:
                    limit = min(limit, outer.page_cap)
                offset = 0 if outer.ignore_offset else int(query.get("offset", ["0"])[0])
                page = outer.facts[offset:offset + limit]
                payload = {"items": page, "limit": limit, "offset": offset}
                if outer.report_total:
                    total = len(outer.facts) if outer.total is None else outer.total
                    payload["total"] = total
                self._send(200, payload)

        self.server = _QuickHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _fact(mid, days, text="plain note", fact_type="world"):
    return {"id": mid, "text": text, "state": "valid", "fact_type": fact_type,
            "date": _iso(days), "invalidation_reason": None}


@pytest.fixture()
def queue(review):
    """Build a paging stub and a client pointed at it; both are closed after the test."""
    servers = []

    def build(facts, **kwargs):
        stub = QueueHindsight(facts, **kwargs)
        servers.append(stub)
        return stub, review.ReviewClient(stub.url, SECRET, timeout_s=5)

    yield build
    for stub in servers:
        stub.close()


def test_global_sort_ranks_a_stale_fact_from_the_second_upstream_page_first(review, queue):
    facts = [_fact(f"f{i}", 1) for i in range(review.MAX_PAGE)]
    facts.append(_fact("old", 400))  # only reachable on the second upstream page
    stub, client = queue(facts)

    result = review.list_review(client, "hermes", now=NOW)

    assert result["items"][0]["id"] == "old"
    assert result["total"] == review.MAX_PAGE + 1
    assert result["scanned"] == review.MAX_PAGE + 1
    assert result["truncated"] is False
    assert len(stub.requests) == 2
    assert f"limit={review.MAX_PAGE}" in stub.requests[0]["path"]
    assert "offset=200" in stub.requests[1]["path"]


def test_review_only_filters_staleness_zero_and_counters_agree(review, queue):
    facts = [
        _fact("fresh", 0),
        _fact("old", 400),
        _fact("volatile", 0, "host is 10.0.0.5"),
    ]
    _stub, client = queue(facts)

    everything = review.list_review(client, "hermes", now=NOW)
    assert everything["matched"] == 3
    assert everything["to_review"] == 2
    assert {i["id"] for i in everything["items"]} == {"fresh", "old", "volatile"}

    queued = review.list_review(client, "hermes", now=NOW, review_only=True)
    assert [i["id"] for i in queued["items"]] == ["old", "volatile"]
    assert queued["matched"] == queued["to_review"] == len(queued["items"]) == 2
    assert queued["scanned"] == 3
    assert all(i["staleness"] > 0 for i in queued["items"])


def test_needs_review_requires_a_strictly_positive_staleness(review):
    """``staleness > 0`` is the rule; a negative score is not a stale fact.

    The item is built by hand (no HTTP): ``score_item`` never returns a negative
    staleness, so this pins the predicate itself.
    """
    assert review.needs_review({"id": "m1", "staleness": -5}) is False
    assert review.needs_review({"id": "m1", "staleness": -0.1}) is False
    assert review.needs_review({"id": "m1", "staleness": 0}) is False
    assert review.needs_review({"id": "m1"}) is False
    assert review.needs_review({"id": "m1", "staleness": 0.5}) is True
    # a negative score stays out of the queue even with an empty ledger
    assert review.needs_review({"id": "m1", "staleness": -5}, {}, NOW) is False


def test_truncated_when_the_bank_exceeds_max_scan(review, queue, monkeypatch):
    monkeypatch.setattr(review, "MAX_SCAN", 5)
    facts = [_fact(f"f{i}", 10 + i) for i in range(8)]
    stub, client = queue(facts)

    result = review.list_review(client, "hermes", now=NOW)

    assert result["scanned"] == 5
    assert result["truncated"] is True
    assert result["total"] == 8
    assert result["matched"] == 5
    assert len(stub.requests) == 1  # one bounded page, never a request past the window
    assert "limit=5" in stub.requests[0]["path"]


def test_offset_slices_the_ranked_set_not_the_upstream_page(review, queue):
    facts = [
        _fact("fresh", 0),  # first upstream, filtered out by review_only
        _fact("a", 400),
        _fact("b", 300),
        _fact("c", 200),
        _fact("d", 100),
    ]
    stub, client = queue(facts)

    page = review.list_review(client, "hermes", limit=2, offset=1, now=NOW, review_only=True)

    assert page["matched"] == 4
    assert [i["id"] for i in page["items"]] == ["b", "c"]  # raw slice would be a,b
    assert page["offset"] == 1 and page["limit"] == 2
    assert "offset=0" in stub.requests[0]["path"]  # the scan starts at the top of the bank


def test_curatable_only_keeps_world_and_experience_over_observations(review, queue):
    """A derived observation cannot be soft-invalidated, so it is not curatable."""
    facts = [
        _fact("obs", 400, fact_type="observation"),
        _fact("world", 300),
        _fact("exp", 200, fact_type="experience"),
    ]
    _stub, client = queue(facts)

    everything = review.list_review(client, "hermes", now=NOW)
    queued = review.list_review(client, "hermes", now=NOW, review_only=True)
    curatable = review.list_review(client, "hermes", now=NOW, curatable_only=True)

    assert everything["matched"] == queued["matched"] == 3
    assert len(everything["items"]) == len(queued["items"]) == 3
    assert curatable["matched"] == 2
    assert [i["id"] for i in curatable["items"]] == ["world", "exp"]
    assert all(i["curatable"] for i in curatable["items"])
    # the queue size is the ledger-filtered scan, never narrowed by this flag
    assert curatable["to_review"] == queued["to_review"] == 3
    assert curatable["scanned"] == 3
    assert {i["id"]: i["curatable"] for i in everything["items"]} == {
        "obs": False, "world": True, "exp": True,
    }


def test_curatable_only_combines_with_review_only(review, queue):
    """Both filters apply: a stale observation is in the queue but not curatable."""
    facts = [
        _fact("obs", 400, fact_type="observation"),
        _fact("world", 300),
        _fact("fresh", 0),
    ]
    _stub, client = queue(facts)

    assert review.list_review(
        client, "hermes", now=NOW, review_only=True
    )["matched"] == 2
    assert review.list_review(
        client, "hermes", now=NOW, curatable_only=True
    )["matched"] == 2  # obs dropped by type, fresh kept without the queue filter

    both = review.list_review(
        client, "hermes", now=NOW, review_only=True, curatable_only=True
    )
    assert both["matched"] == 1
    assert [i["id"] for i in both["items"]] == ["world"]
    assert both["to_review"] == 2


def test_offset_slices_the_curatable_filtered_set(review, queue):
    facts = [
        _fact("obs", 500, fact_type="observation"),  # ranks first, not curatable
        _fact("a", 400),
        _fact("b", 300),
        _fact("c", 200),
    ]
    _stub, client = queue(facts)

    page = review.list_review(
        client, "hermes", limit=2, offset=1, now=NOW, curatable_only=True
    )

    assert page["matched"] == 3
    assert [i["id"] for i in page["items"]] == ["b", "c"]  # raw slice would be a,b
    assert page["offset"] == 1 and page["limit"] == 2
    assert page["to_review"] == 4


def test_curatable_only_default_is_the_unchanged_output(review, queue):
    facts = [
        _fact("obs", 400, fact_type="observation"),
        _fact("world", 300),
        _fact("fresh", 0),
    ]
    _stub, client = queue(facts)

    default = review.list_review(client, "hermes", now=NOW)
    explicit = review.list_review(client, "hermes", now=NOW, curatable_only=False)

    assert explicit == default
    assert default["matched"] == 3
    assert len(default["items"]) == 3
    assert default["to_review"] == 2


def test_volatile_only_keeps_only_facts_with_a_volatile_score(review, queue):
    facts = [
        _fact("risky", 10, "host is 10.0.0.5"),  # volatile 0.35
        _fact("old", 400),                       # volatile 0
        _fact("versioned", 0, "runs v1.2.3"),    # volatile 0.25
    ]
    _stub, client = queue(facts)

    everything = review.list_review(client, "hermes", now=NOW)
    volatile = review.list_review(client, "hermes", now=NOW, volatile_only=True)

    assert everything["matched"] == 3
    assert volatile["matched"] == 2
    assert [i["id"] for i in volatile["items"]] == ["risky", "versioned"]
    assert all(i["volatile_score"] > 0 for i in volatile["items"])
    assert volatile["scanned"] == 3
    # the queue size is the ledger-filtered scan, never narrowed by this flag
    assert volatile["to_review"] == everything["to_review"] == 3


def test_volatile_only_combines_with_review_only_and_curatable_only(review, queue):
    """Three filters, one set: each drops what the others keep."""
    facts = [
        _fact("plain", 400),                                         # stale, curatable, quiet
        _fact("obs-plain", 400, fact_type="observation"),            # stale, not curatable
        _fact("volatile-obs", 300, "ip 10.0.0.7", fact_type="observation"),
        _fact("volatile-world", 300, "currently v2.0.1"),
    ]
    _stub, client = queue(facts)

    def ids(**flags):
        result = review.list_review(client, "hermes", now=NOW, **flags)
        assert result["matched"] == len(result["items"])
        return {i["id"] for i in result["items"]}

    assert ids() == {"plain", "obs-plain", "volatile-obs", "volatile-world"}
    assert ids(volatile_only=True) == {"volatile-obs", "volatile-world"}
    assert ids(curatable_only=True) == {"plain", "volatile-world"}
    assert ids(review_only=True, curatable_only=True) == {"plain", "volatile-world"}
    assert ids(review_only=True, volatile_only=True) == {"volatile-obs", "volatile-world"}
    assert ids(curatable_only=True, volatile_only=True) == {"volatile-world"}
    assert ids(review_only=True, curatable_only=True, volatile_only=True) == {"volatile-world"}


def test_offset_slices_the_volatile_filtered_set(review, queue):
    facts = [
        _fact("plain-old", 300),                        # volatile 0: would sit inside the window
        _fact("volatile-a", 200, "pending"),
        _fact("volatile-b", 100, "see http://nas.local/share"),
        _fact("volatile-c", 50, "v1.2.3"),
    ]
    _stub, client = queue(facts)

    page = review.list_review(client, "hermes", limit=2, offset=1, now=NOW, volatile_only=True)

    assert page["matched"] == 3
    # unfiltered, offset 1..3 would be volatile-a, volatile-b
    assert [i["id"] for i in page["items"]] == ["volatile-b", "volatile-c"]
    assert page["offset"] == 1 and page["limit"] == 2
    assert page["to_review"] == 4


def test_volatile_only_default_is_the_unchanged_output(review, queue):
    facts = [
        _fact("volatile", 200, "currently on 10.0.0.5"),
        _fact("plain", 400),
        _fact("fresh", 0),
    ]
    _stub, client = queue(facts)

    default = review.list_review(client, "hermes", now=NOW)
    explicit = review.list_review(client, "hermes", now=NOW, volatile_only=False)

    assert explicit == default
    assert [i["id"] for i in default["items"]] == ["volatile", "plain", "fresh"]
    assert default["matched"] == 3
    assert default["to_review"] == 2


def test_q_is_forwarded_upstream_and_never_filtered_here(review, queue):
    facts = [_fact("a", 10, "postgres on the nas"), _fact("b", 20, "unrelated")]
    stub, client = queue(facts)

    result = review.list_review(client, "hermes", q="  postgres  ", now=NOW)

    assert "q=postgres" in stub.requests[0]["path"]
    assert "state=valid" in stub.requests[0]["path"]
    assert result["scanned"] == 2  # the stub ignores q: filtering is Hindsight's job
    assert len(result["items"]) == 2


def test_list_review_response_keys_match_the_contract(review, queue):
    _stub, client = queue([_fact("a", 10)])
    result = review.list_review(client, "hermes", now=NOW)
    assert list(result) == [
        "bank", "total", "scanned", "truncated", "matched", "to_review",
        "offset", "limit", "sort", "items",
    ]
    assert result["scanned"] == 1 and result["truncated"] is False
    assert result["matched"] == 1 and result["to_review"] == 1


def test_scan_terminates_on_a_server_that_ignores_offset(review, queue):
    facts = [_fact(f"f{i}", i) for i in range(review.MAX_PAGE + 5)]
    stub, client = queue(facts, ignore_offset=True)

    result = review.list_review(client, "hermes", now=NOW)

    assert result["scanned"] == review.MAX_PAGE
    assert len(stub.requests) == 2  # the repeated page brought no new id: stop
    assert len(result["items"]) == 100
    # 205 facts exist, only 200 were read: the early stop is reported, not hidden.
    assert result["total"] == 205
    assert result["truncated"] is True


@pytest.mark.parametrize(
    "facts,total,report_total,expected",
    [
        (0, 1999, True, False),   # the bank fits the window exactly
        (0, 2000, True, False),   # exactly MAX_SCAN: nothing was cut
        (0, 2500, True, True),    # the window cut a larger bank
        (0, None, False, True),   # no total and a full window: assume it was cut
    ],
    ids=["fits", "exact-window", "window-cut", "no-total-full-window"],
)
def test_truncated_boundaries(review, queue, facts, total, report_total, expected):
    count = total if report_total else review.MAX_SCAN
    rows = [_fact(f"f{i}", 10) for i in range(count)]
    _stub, client = queue(rows, total=total, report_total=report_total)

    result = review.list_review(client, "hermes", now=NOW)

    assert result["scanned"] == min(count, review.MAX_SCAN)
    assert result["truncated"] is expected
    if report_total:
        assert result["total"] == total


def test_truncated_when_the_server_ignores_offset_and_ignores_the_size_it_reports(review, queue):
    # The upstream total says 205, offset is ignored: 200 rows read, 205 claimed.
    facts = [_fact(f"f{i}", 10) for i in range(205)]
    stub, client = queue(facts, ignore_offset=True)

    result = review.list_review(client, "hermes", now=NOW)

    assert result["scanned"] == 200 and result["total"] == 205
    assert result["truncated"] is True
    assert len(stub.requests) == 2


def test_non_numeric_total_does_not_mark_a_full_window_as_truncated(review, queue, monkeypatch):
    monkeypatch.setattr(review, "MAX_SCAN", 5)
    facts = [_fact(f"f{i}", 10) for i in range(5)]
    _stub, client = queue(facts, total="5")

    result = review.list_review(client, "hermes", now=NOW)

    assert result["scanned"] == 5
    assert result["total"] == "5"  # the raw value is what the response carries
    assert result["truncated"] is False  # the string total was coerced, not dropped


def test_a_total_that_cannot_be_coerced_is_treated_as_absent(review, queue, monkeypatch):
    monkeypatch.setattr(review, "MAX_SCAN", 5)
    facts = [_fact(f"f{i}", 10) for i in range(5)]
    _stub, client = queue(facts, total="many")

    result = review.list_review(client, "hermes", now=NOW)

    assert result["scanned"] == 5 and result["total"] == "many"
    assert result["truncated"] is True  # no usable total + full window: assume cut


def test_scan_stops_at_max_calls_against_a_one_row_per_page_server(review, queue):
    facts = [_fact(f"f{i}", 10) for i in range(400)]
    stub, client = queue(facts, page_cap=1)

    result = review.list_review(client, "hermes", now=NOW)

    assert review.MAX_CALLS == 4 * -(-review.MAX_SCAN // review.MAX_PAGE)
    assert len(stub.requests) <= review.MAX_CALLS
    assert len(stub.requests) == review.MAX_CALLS
    assert result["scanned"] == review.MAX_CALLS
    assert result["truncated"] is True  # 400 facts exist, MAX_CALLS were read


def test_facts_without_an_id_are_skipped_instead_of_collapsed_into_one(review, queue):
    facts = [
        {"id": "", "text": "no id one", "date": _iso(400), "fact_type": "world"},
        {"id": "", "text": "no id two", "date": _iso(400), "fact_type": "world"},
        {"id": "", "text": "no id three", "date": _iso(400), "fact_type": "world"},
        _fact("kept", 400),
    ]
    stub, client = queue(facts)

    result = review.list_review(client, "hermes", now=NOW)

    # An id-less fact can be neither curated nor deduplicated: it is not scored at
    # all, and it must not make its siblings vanish either.
    assert result["scanned"] == 1
    assert [item["id"] for item in result["items"]] == ["kept"]
    assert result["total"] == 4
    assert len(stub.requests) == 2  # the page was not mistaken for a repeat

