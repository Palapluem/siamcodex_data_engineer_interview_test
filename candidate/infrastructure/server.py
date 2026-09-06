"""Fixed interview infrastructure, not a candidate pipeline implementation."""
import hmac
import http.client
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODE = os.environ.get("MODE", "source")
SOURCE = os.environ.get("SOURCE", "aster")
RUNTIME = Path(os.environ.get("RUNTIME", "/runtime"))
DATA = Path(os.environ.get("DATA", "/data"))
LOCK = threading.Lock()
REQUEST_TIMES = deque()
FAILED_CURSORS = set()
EVENTS = []


def state():
    # Docker Desktop file sharing can briefly expose stale length/content metadata
    # even after an atomic host rename. Bound the propagation wait explicitly.
    for _ in range(20):
        try:
            return json.loads((RUNTIME / "control/control.json").read_text())
        except (json.JSONDecodeError, OSError):
            time.sleep(.1)
    raise RuntimeError("control_unavailable")


class Handler(BaseHTTPRequestHandler):
    server_version = "InterviewFixture/1.0"

    def log_message(self, *_):
        pass  # Never log credentials, tokens, or payloads.

    def reply(self, code, value, headers=None):
        body = json.dumps(value).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, val in (headers or {}).items():
            self.send_header(key, str(val))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        if MODE != "identity" or self.path != "/introspect":
            return self.reply(404, {"error": "not_found"})
        expected = (RUNTIME / "identity_service.key").read_text().strip()
        if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {expected}"):
            return self.reply(401, {"error": "unauthenticated"})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError()
            token = json.loads(self.rfile.read(size))["token"]
            principals = json.loads((RUNTIME / "principals.json").read_text())
            principal = principals.get(token)
            if not principal or token in state().get("revoked_tokens", []):
                return self.reply(200, {"active": False})
            return self.reply(200, dict(principal, active=True))
        except (ValueError, KeyError, TypeError):
            return self.reply(400, {"error": "invalid_request"})
        except RuntimeError:
            return self.reply(503, {"error": "control_unavailable"}, {"Retry-After": 1})

    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"status": "ok", "mode": MODE})
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path != "/v1/changes" or MODE == "identity":
            return self.reply(404, {"error": "not_found"})
        if MODE == "gateway":
            # Fixed reverse proxy: caller cannot supply a destination or arbitrary path.
            try:
                req = urllib.request.Request("http://source.internal:8080" + self.path,
                                             headers={"Authorization": self.headers.get("Authorization", "")})
                with urllib.request.urlopen(req, timeout=5) as response:
                    return self.reply(response.status, json.load(response))
            except urllib.error.HTTPError as err:
                return self.reply(err.code, json.load(err), {"Retry-After": err.headers.get("Retry-After", "1")})
            except (urllib.error.URLError, TimeoutError, http.client.HTTPException, ConnectionError):
                return self.reply(503, {"error": "upstream_unavailable"}, {"Retry-After": 1})
        expected = (RUNTIME / f"{SOURCE}.key").read_text().strip()
        if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {expected}"):
            return self.reply(401, {"error": "unauthenticated"})
        try:
            current = state()
        except RuntimeError:
            return self.reply(503, {"error": "control_unavailable"}, {"Retry-After": 1})
        if current.get("outages", {}).get(SOURCE, False):
            return self.reply(503, {"error": "link_unavailable"}, {"Retry-After": 2})
        try:
            q = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
            if set(q) - {"cursor", "limit"} or any(len(v) != 1 for v in q.values()):
                raise ValueError()
            cursor = int(q.get("cursor", ["0"])[0])
            limit = int(q.get("limit", ["100"])[0])
            if cursor < 0 or not 1 <= limit <= 200:
                raise ValueError()
        except ValueError:
            return self.reply(400, {"error": "invalid_cursor_or_limit"})
        rate = {"aster": 8, "birch": 4, "cobalt": 2}[SOURCE]
        now = time.monotonic()
        with LOCK:
            while REQUEST_TIMES and REQUEST_TIMES[0] <= now - 1:
                REQUEST_TIMES.popleft()
            if len(REQUEST_TIMES) >= rate:
                return self.reply(429, {"error": "rate_limited"}, {"Retry-After": 1})
            REQUEST_TIMES.append(now)
            if current.get("transient_errors", True) and cursor > 0 and cursor % 600 == 0 and cursor not in FAILED_CURSORS:
                FAILED_CURSORS.add(cursor)
                return self.reply(503, {"error": "temporary_failure"}, {"Retry-After": 1})
        time.sleep({"aster": .05, "birch": .10, "cobalt": .15}[SOURCE])
        visible = [e for e in EVENTS if e["phase"] <= current["phase"]]
        page = [e for e in visible if e["seq"] > cursor][:limit]
        last = page[-1]["seq"] if page else cursor
        return self.reply(200, {"source": SOURCE,
                               "items": [{k: v for k, v in e.items() if k != "phase"} for e in page],
                               "next_cursor": str(last), "has_more": any(e["seq"] > last for e in visible)})


def main():
    global EVENTS
    if MODE == "source":
        EVENTS = [json.loads(line) for line in (DATA / f"{SOURCE}.jsonl").read_text().splitlines()]
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler)
    if MODE in ("gateway", "identity"):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(RUNTIME / "tls/server.crt", RUNTIME / "tls/server.key")
        server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
