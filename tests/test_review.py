import importlib.util
import json
import pathlib
import socketserver
import sys
import threading
import types
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
    assert "limit=50" in req["path"] and "state=valid" in req["path"]
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
    assert '"DELETE"' not in source.replace('method not in ("GET", "PATCH")', "")
    assert '"PUT"' not in source
    assert '"POST"' not in source


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
