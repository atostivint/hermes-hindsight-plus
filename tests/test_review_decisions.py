"""Local review ledger: decisions that stop a confirmed fact from coming back.

The ledger is a plain JSON file in the plugin data dir (``plugin_api.py`` owns the
file, ``review.py`` owns the pure helpers). Nothing here talks to Hindsight: the
decision routes are local by contract. ``FakeClient`` replaces only the transport
already covered by ``tests/test_review.py``; ``_load_api`` replaces ``fastapi`` the
same way the other dashboard tests do.
"""

import importlib.util
import json
import pathlib
import re
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def review():
    spec = importlib.util.spec_from_file_location("hh_review_decisions_under_test", ROOT / "review.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["hh_review_decisions_under_test"] = module
    spec.loader.exec_module(module)
    return module


def _iso(days):
    return (NOW - timedelta(days=days)).isoformat()


def _fact(mid, days, text):
    return {"id": mid, "text": text, "state": "valid", "fact_type": "world",
            "date": _iso(days), "invalidation_reason": None}


class FakeClient:
    """Serves one page of facts; the HTTP transport is not what these tests cover."""

    def __init__(self, facts):
        self.facts = list(facts)

    def list_memories(self, bank, limit, offset, state=None, q=None):
        return {"items": self.facts[offset:offset + limit], "total": len(self.facts)}


# --- decision_hides / list_review ----------------------------------------------


def test_still_true_with_matching_hash_hides_the_fact_from_review_only(review):
    facts = [_fact("stale", 400, "old note"), _fact("other", 300, "other note")]
    client = FakeClient(facts)
    before = review.list_review(client, "hermes", now=NOW, review_only=True)
    assert before["matched"] == 2

    decisions = review.record_decision(
        {}, "stale", "still true", review.text_hash("old note"), now=NOW
    )
    after = review.list_review(
        client, "hermes", now=NOW, review_only=True, decisions=decisions
    )

    assert [i["id"] for i in after["items"]] == ["other"]
    assert after["matched"] == before["matched"] - 1
    assert after["to_review"] == 1  # the queue is what the user will actually see
    items = {i["id"]: i for i in before["items"]}
    assert review.decision_hides(decisions["stale"], items["stale"], NOW) is True

    # review_only=False is the full bank view: the ledger never removes a fact there.
    everything = review.list_review(client, "hermes", now=NOW, decisions=decisions)
    assert {i["id"] for i in everything["items"]} == {"stale", "other"}
    assert everything["to_review"] == 1  # the badge still excludes the settled fact


def test_to_review_is_the_queue_size_after_the_ledger(review):
    """3 stale facts, one settled by the ledger: queue and page agree at 2."""
    facts = [
        _fact("settled", 400, "checked note"),
        _fact("stale-a", 300, "other note"),
        _fact("stale-b", 200, "third note"),
        _fact("fresh", 0, "brand new"),
    ]
    client = FakeClient(facts)
    decisions = review.record_decision(
        {}, "settled", "still true", review.text_hash("checked note"), now=NOW
    )

    queued = review.list_review(
        client, "hermes", now=NOW, review_only=True, decisions=decisions
    )
    full = review.list_review(client, "hermes", now=NOW, decisions=decisions)

    assert queued["to_review"] == 2
    assert queued["matched"] == 2
    assert [i["id"] for i in queued["items"]] == ["stale-a", "stale-b"]
    assert full["to_review"] == 2  # same predicate, whatever the page shows
    assert full["matched"] == 4    # matched is the post-filter count, not the queue
    assert review.needs_review(
        review.score_item(facts[1], NOW), decisions, NOW
    ) is True
    assert review.needs_review(
        review.score_item(facts[0], NOW), decisions, NOW
    ) is False
    assert review.needs_review(review.score_item(facts[3], NOW), decisions, NOW) is False


def test_changed_text_makes_the_fact_eligible_again(review):
    decisions = review.record_decision(
        {}, "m1", "still true", review.text_hash("old note"), now=NOW
    )
    client = FakeClient([_fact("m1", 400, "old note")])
    assert review.list_review(
        client, "hermes", now=NOW, review_only=True, decisions=decisions
    )["matched"] == 0

    client.facts = [_fact("m1", 400, "new note")]
    again = review.list_review(
        client, "hermes", now=NOW, review_only=True, decisions=decisions
    )
    assert [i["id"] for i in again["items"]] == ["m1"]
    assert again["matched"] == 1
    assert review.score_item(client.facts[0], NOW)["text_hash"] != decisions["m1"]["hash"]


def test_decision_older_than_ttl_makes_the_fact_eligible_again(review):
    assert review.OK_TTL_DAYS == 90
    digest = review.text_hash("old note")
    client = FakeClient([_fact("m1", 400, "old note")])

    fresh = review.record_decision(
        {}, "m1", "still true", digest, now=NOW - timedelta(days=review.OK_TTL_DAYS - 1)
    )
    assert review.list_review(
        client, "hermes", now=NOW, review_only=True, decisions=fresh
    )["matched"] == 0

    boundary = review.record_decision(
        {}, "m1", "still true", digest, now=NOW - timedelta(days=review.OK_TTL_DAYS)
    )
    assert review.list_review(
        client, "hermes", now=NOW, review_only=True, decisions=boundary
    )["matched"] == 1  # exactly 90 days old is no longer "younger than"

    stale = review.record_decision(
        {}, "m1", "still true", digest, now=NOW - timedelta(days=review.OK_TTL_DAYS + 1)
    )
    result = review.list_review(
        client, "hermes", now=NOW, review_only=True, decisions=stale
    )
    assert result["matched"] == 1 and result["items"][0]["id"] == "m1"
    assert review.decision_hides(stale["m1"], result["items"][0], NOW) is False


def test_cannot_tell_and_outdated_never_hide_a_fact(review):
    digest = review.text_hash("old note")
    client = FakeClient([_fact("m1", 400, "old note")])
    for verdict in ("outdated", "cannot tell"):
        decisions = review.record_decision({}, "m1", verdict, digest, now=NOW)
        result = review.list_review(
            client, "hermes", now=NOW, review_only=True, decisions=decisions
        )
        assert result["matched"] == 1, verdict
        assert review.decision_hides(decisions["m1"], result["items"][0], NOW) is False
        assert review.decision_counts(decisions)[verdict] == 1


def test_text_hash_is_stable_and_sensitive_to_the_text(review):
    digest = review.text_hash("a fact")
    assert re.fullmatch(r"[0-9a-f]{16}", digest)
    assert digest == review.text_hash("a fact")
    assert digest != review.text_hash("a fact ")  # no trimming: the text as stored
    assert review.text_hash(None) == review.text_hash("")
    item = review.score_item({"id": "x", "text": "a fact"}, NOW)
    assert item["text_hash"] == digest
    assert len([key for key in item if key == "text_hash"]) == 1
    assert "text_hash" in review.list_review(
        FakeClient([{"id": "x", "text": "a fact", "date": _iso(1)}]), "hermes", now=NOW
    )["items"][0]


# --- recording -----------------------------------------------------------------


def test_record_decision_returns_a_new_mapping_and_validates_input(review):
    original = {}
    decisions = review.record_decision(
        original, "m1", "STILL TRUE", review.text_hash("t"), now=NOW
    )
    assert original == {}  # the input is never mutated
    assert decisions["m1"] == {
        "verdict": "still true", "at": NOW.isoformat(), "hash": review.text_hash("t")
    }
    assert review.decision_counts(decisions) == {
        "still true": 1, "outdated": 0, "cannot tell": 0
    }
    for bad in ("maybe", "", None, True):
        with pytest.raises(review.ReviewError) as exc:
            review.record_decision({}, "m1", bad, review.text_hash("t"), now=NOW)
        assert exc.value.status == 400
    for bad in ("../m1", "", None, 5):
        with pytest.raises(review.ReviewError) as exc:
            review.record_decision({}, bad, "still true", review.text_hash("t"), now=NOW)
        assert exc.value.status == 400
    for bad in ("", None, "zzzz", review.text_hash("t").upper()[:15]):
        with pytest.raises(review.ReviewError) as exc:
            review.record_decision({}, "m1", "still true", bad, now=NOW)
        assert exc.value.status == 400
    # an uppercase digest is normalised, not rejected: the page may send either case
    assert review.record_decision(
        {}, "m1", "still true", review.text_hash("t").upper(), now=NOW
    )["m1"]["hash"] == review.text_hash("t")


def test_prune_decisions_keeps_the_newest_entries(review):
    decisions = {}
    for index in range(4):
        decisions = review.record_decision(
            decisions, f"m{index}", "cannot tell", review.text_hash("t"),
            now=NOW - timedelta(days=index),
        )
    pruned = review.prune_decisions(decisions, 2)
    assert set(pruned) == {"m0", "m1"}  # newest by `at`, oldest pruned first
    assert review.prune_decisions(decisions, 10) == decisions
    assert review.prune_decisions({}, 0) == {}
    # a junk entry (no parseable `at`) is pruned before a dated one
    junk = dict(decisions)
    junk["junk"] = {"verdict": "still true", "hash": review.text_hash("t")}
    assert "junk" not in review.prune_decisions(junk, 2)


# --- dashboard routes ----------------------------------------------------------


def _load_api(monkeypatch, api_path=None):
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
        "hh_dashboard_api_decisions_test",
        api_path or (ROOT / "dashboard" / "plugin_api.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, FakeHTTPException


@pytest.fixture()
def api(monkeypatch, tmp_path):
    module, http_exc = _load_api(monkeypatch)
    # ``_decisions_path`` is the only data-dir seam the routes use: keep the ledger
    # inside the test's tmp dir instead of the real plugin data dir.
    monkeypatch.setattr(
        module, "_decisions_path", lambda: tmp_path / "review-decisions.json"
    )
    try:
        yield module, http_exc, tmp_path
    finally:
        for name in ("hermes_hindsight_plus_dashboard_core", "hermes_hindsight_plus_dashboard_review"):
            sys.modules.pop(name, None)


def test_decisions_path_is_the_core_plugin_data_dir(tmp_path, monkeypatch):
    """The ledger is resolved by ``core._plugin_data_dir``, with no alias to guess."""
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    module, _http_exc = _load_api(monkeypatch)
    try:
        assert module._decisions_path() == (
            pathlib.Path(module._core()._plugin_data_dir()) / module.DECISIONS_FILENAME
        )
        assert module._decisions_path().parent.parts[-2:] == (
            "plugin-data", "hermes-hindsight-plus"
        )
        # The route resolves the real thing too, not a module-level snapshot.
        assert module.review_decisions("hermes")["total"] == 0
    finally:
        for name in ("hermes_hindsight_plus_dashboard_core", "hermes_hindsight_plus_dashboard_review"):
            sys.modules.pop(name, None)


def test_decisions_routes_round_trip_writes_atomic_json(api):
    module, _http_exc, tmp_path = api
    ledger_path = tmp_path / "review-decisions.json"
    digest = "0123456789abcdef"

    assert module.DECISIONS_FILENAME == "review-decisions.json"
    assert module._decisions_path() == ledger_path
    assert module.review_decisions("hermes") == {
        "bank": "hermes",
        "counts": {"still true": 0, "outdated": 0, "cannot tell": 0},
        "total": 0,
    }

    posted = module.review_record_decision(
        "hermes", {"id": "m1", "verdict": "still true", "hash": digest}
    )
    assert posted["decision"]["verdict"] == "still true"
    assert posted["decision"]["hash"] == digest
    assert posted["counts"] == {"still true": 1, "outdated": 0, "cannot tell": 0}

    got = module.review_decisions("hermes")
    assert got["total"] == 1 and got["counts"]["still true"] == 1

    raw = json.loads(ledger_path.read_text("utf-8"))  # valid JSON on disk
    assert list(raw) == ["hermes"]
    assert set(raw["hermes"]["m1"]) == {"verdict", "at", "hash"}  # no fact text
    assert raw["hermes"]["m1"]["verdict"] == "still true"
    assert "old note" not in ledger_path.read_text("utf-8")
    # the atomic write leaves no temp file behind
    assert [p.name for p in tmp_path.iterdir()] == ["review-decisions.json"]

    module.review_record_decision("atlas", {"id": "a1", "verdict": "outdated", "hash": digest})
    assert module.review_decisions("hermes")["total"] == 1  # banks stay separate
    assert module.review_decisions("atlas")["counts"]["outdated"] == 1


def test_decisions_routes_reject_unknown_bank_and_verdict(api):
    module, http_exc, tmp_path = api
    with pytest.raises(http_exc) as exc:
        module.review_decisions("prod")
    assert exc.value.status_code == 404  # check_bank's status
    with pytest.raises(http_exc) as exc:
        module.review_record_decision(
            "prod", {"id": "m1", "verdict": "still true", "hash": "0123456789abcdef"}
        )
    assert exc.value.status_code == 404
    with pytest.raises(http_exc) as exc:
        module.review_record_decision(
            "hermes", {"id": "m1", "verdict": "maybe", "hash": "0123456789abcdef"}
        )
    assert exc.value.status_code == 400
    with pytest.raises(http_exc) as exc:
        module.review_record_decision("hermes", {"id": "m1", "verdict": "still true"})
    assert exc.value.status_code == 400
    assert not (tmp_path / "review-decisions.json").exists()


def test_decisions_route_rejects_a_malformed_id_or_hash(api):
    """Syntactic validation only: the pair must look right before it is journalled.

    The route never asks Hindsight whether the fact exists (documented in its
    docstring), so this is the only gate an entry gets.
    """
    module, http_exc, tmp_path = api
    for bad_id in ("", "../m1", None, 5, "a/b"):
        with pytest.raises(http_exc) as exc:
            module.review_record_decision(
                "hermes", {"id": bad_id, "verdict": "still true", "hash": "0123456789abcdef"}
            )
        assert exc.value.status_code == 400, bad_id
    for bad_hash in ("", None, "0123456789abcde", "0123456789abcdeg", 17):
        with pytest.raises(http_exc) as exc:
            module.review_record_decision(
                "hermes", {"id": "m1", "verdict": "still true", "hash": bad_hash}
            )
        assert exc.value.status_code == 400, bad_hash
    assert not (tmp_path / "review-decisions.json").exists()  # nothing journalled


def test_corrupt_ledger_reads_as_empty(api):
    module, _http_exc, tmp_path = api
    ledger_path = tmp_path / "review-decisions.json"
    ledger_path.write_text("{not json", "utf-8")
    assert module.review_decisions("hermes") == {
        "bank": "hermes",
        "counts": {"still true": 0, "outdated": 0, "cannot tell": 0},
        "total": 0,
    }
    ledger_path.write_text('["not", "an", "object"]', "utf-8")
    assert module.review_decisions("hermes")["total"] == 0

    module.review_record_decision(
        "hermes", {"id": "m1", "verdict": "cannot tell", "hash": "0123456789abcdef"}
    )
    assert json.loads(ledger_path.read_text("utf-8"))["hermes"]["m1"]["verdict"] == "cannot tell"


def test_decisions_route_prunes_at_max_decisions(api, monkeypatch):
    module, _http_exc, tmp_path = api
    monkeypatch.setattr(module._review(), "MAX_DECISIONS", 2)
    for index in range(3):
        module.review_record_decision(
            "hermes",
            {"id": f"m{index}", "verdict": "cannot tell", "hash": "0123456789abcdef"},
        )
    got = module.review_decisions("hermes")
    assert got["total"] == 2
    raw = json.loads((tmp_path / "review-decisions.json").read_text("utf-8"))
    assert set(raw["hermes"]) == {"m1", "m2"}  # the oldest decision went first


def test_review_list_limit_ceiling_is_read_lazily_from_review_max_page(tmp_path, monkeypatch):
    """The route's ceiling must follow ``review.MAX_PAGE`` at call time, never at import.

    Proven on a copy of the pair whose backend says ``MAX_PAGE = 7``: loading the API
    must not import ``review.py`` at all (``_review_cache`` stays ``None``), and a
    limit above the backend ceiling must be the usual 400 carrying that 7.
    """
    source = (ROOT / "review.py").read_text("utf-8")
    static_source = (ROOT / "dashboard" / "plugin_api.py").read_text("utf-8")
    assert "MAX_PAGE = 200" in source
    patched = source.replace("MAX_PAGE = 200", "MAX_PAGE = 7", 1)
    (tmp_path / "review.py").write_text(patched, "utf-8")
    (tmp_path / "dashboard").mkdir()
    (tmp_path / "dashboard" / "plugin_api.py").write_text(static_source, "utf-8")

    module, http_exc = _load_api(
        monkeypatch, tmp_path / "dashboard" / "plugin_api.py"
    )
    try:
        assert module._review_cache is None  # nothing loaded at import
        assert not hasattr(module, "_REVIEW_MAX_PAGE")  # no import-time snapshot
        with pytest.raises(http_exc) as exc:
            module.review_list(
                "hermes", limit=8, offset=0, state="valid",
                sort="staleness", q="", review_only=False,
            )
        assert exc.value.status_code == 400
        assert "1..7" in exc.value.detail
        assert module._review().MAX_PAGE == 7
        assert source.count("MAX_PAGE = 200") == 1  # the backend owns the number
    finally:
        for name in (
            "hermes_hindsight_plus_dashboard_core",
            "hermes_hindsight_plus_dashboard_review",
        ):
            sys.modules.pop(name, None)


def test_a_broken_review_module_does_not_break_the_dashboard_api(tmp_path, monkeypatch):
    """A ``review.py`` that cannot load must cost the review routes, not the API."""
    (tmp_path / "review.py").write_text(
        "raise RuntimeError('review unavailable')\n", "utf-8"
    )
    (tmp_path / "dashboard").mkdir()
    (tmp_path / "dashboard" / "plugin_api.py").write_text(
        (ROOT / "dashboard" / "plugin_api.py").read_text("utf-8"), "utf-8"
    )

    module, _http_exc = _load_api(
        monkeypatch, tmp_path / "dashboard" / "plugin_api.py"
    )
    try:
        assert module._review_cache is None  # the module imported cleanly
        with pytest.raises(RuntimeError):
            module.review_decisions("hermes")  # only the review surface fails
    finally:
        for name in (
            "hermes_hindsight_plus_dashboard_core",
            "hermes_hindsight_plus_dashboard_review",
        ):
            sys.modules.pop(name, None)
