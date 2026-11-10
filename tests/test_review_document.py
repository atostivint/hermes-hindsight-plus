import importlib.util
import json
import pathlib
import socketserver
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOW_ISO = "2026-01-01T00:00:00+00:00"


def _load_review():
    spec = importlib.util.spec_from_file_location("hh_review_document_test", ROOT / "review.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["hh_review_document_test"] = module
    spec.loader.exec_module(module)
    return module


import pytest

REVIEW = _load_review()


class _QuickHTTPServer(HTTPServer):
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class StubHindsight:
    def __init__(self):
        self.requests = []
        self.memories = {
            "m1": {"id": "m1", "text": "Old fact", "state": "valid", "fact_type": "world", "date": NOW_ISO},
            "m2": {"id": "m2", "text": "Retired fact", "state": "invalidated", "fact_type": "experience", "date": NOW_ISO, "invalidation_reason": "outdated: gone"},
        }

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
                self._record()
                mid = self.path.rsplit("/", 1)[-1].split("?")[0]
                if mid in self.server.memories:
                    return self._send(200, self.server.memories[mid])
                self._send(404, {"detail": "not found"})

            def do_PATCH(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                self._record(body)
                mid = self.path.rsplit("/", 1)[-1]
                memory = self.server.memories.get(mid)
                if memory is None:
                    return self._send(404, {"detail": "not found"})
                if "text" in body:
                    memory["text"] = body["text"]
                if "state" in body:
                    memory["state"] = body["state"]
                    memory["invalidation_reason"] = body.get("reason")
                self._send(200, {})

            def _record(self, body=None):
                self.server.requests.append({"method": self.command, "path": self.path, "body": body})

        self.server = _QuickHTTPServer(("127.0.0.1", 0), Handler)
        self.server.memories = self.memories
        self.server.requests = self.requests
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def stub():
    s = StubHindsight()
    yield s
    s.close()


@pytest.fixture()
def client(stub):
    return REVIEW.ReviewClient(stub.url, "sk-test", timeout_s=5)


def test_lcs_alignment_one_removal_zero_updates(client, stub):
    # Mimics an LCS-aligned document edit: base [A, B, C] -> [A, C].
    out = REVIEW.apply_document(client, "hermes", [
        {"id": "m1", "state": "invalidated", "reason": "removed in document editor"}
    ])
    assert out["removed"] == 1
    assert out["updated"] == 0
    assert out["results"][0]["ok"] is True


def test_apply_document_update_ok_and_unknown_id_does_not_abort(client, stub):
    out = REVIEW.apply_document(client, "hermes", [
        {"id": "m1", "text": "New fact"},
        {"id": "nope", "text": "x"},
    ])
    assert out["results"][0]["ok"] is True
    assert out["results"][1]["ok"] is False
    assert out["updated"] == 1
    assert out["failed"] == ["nope"]
    assert stub.memories["m1"]["text"] == "New fact"


def test_apply_document_invalid_id_creates_nothing_and_no_http_call(client, stub):
    before = len(stub.requests)
    out = REVIEW.apply_document(client, "hermes", [{"id": "", "text": "new fact"}])
    assert out["results"][0]["ok"] is False
    assert len(stub.requests) == before
