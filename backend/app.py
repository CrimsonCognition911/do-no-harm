"""Loopback-only fixture API: no clinical execution or provider connections."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
from time import monotonic
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))
from backend.session_service import APIError, SessionService


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("Non-finite JSON number")


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def send_json(self, status, payload, *, allow=None):
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if allow:
            self.send_header("Allow", allow)
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        path = urlsplit(self.path).path
        if self.server.sessions is not None and (path == "/api/runs" or path.startswith("/api/runs/")):
            self.session_request()
            return
        if path == "/health":
            self.send_json(200, {
                "service": "do-no-harm",
                "status": "scaffold",
                "contract_version": "0.1",
                "integrations": {
                    "agents_api": "not_connected",
                    "gpt_live": "not_connected",
                    "openmrs": "not_connected",
                },
            })
        elif path == "/ready":
            self.send_json(503, {
                "ready": False,
                "reason": "Clinical runtime and provider integrations are not implemented yet.",
            })
        elif path == "/api/contracts/events":
            schema = json.loads((ROOT / "contracts" / "events.schema.json").read_text())
            self.send_json(200, schema)
        elif path == "/api/contracts/handshake":
            handshake = json.loads((ROOT / "contracts" / "samples" / "handshake.json").read_text())
            self.send_json(200, handshake)
        elif path == "/api/contracts/voice-update":
            schema = json.loads((ROOT / "contracts" / "voice-update.schema.json").read_text())
            self.send_json(200, schema)
        else:
            self.send_json(404, {"error": "not_found"})

    def do_POST(self):
        if self.server.sessions is None:
            self.send_json(405, {"error": "method_not_allowed"}, allow="GET")
        else:
            self.session_request()

    def session_request(self):
        try:
            expected_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if len(self.headers.get_all("Host", [])) != 1 or self.headers.get("Host") not in expected_hosts:
                raise APIError(403, "untrusted_host")
            # Server-to-server/BFF transport only. Browser origins are not trusted.
            if self.headers.get_all("Origin"):
                raise APIError(403, "browser_origin_not_supported")
            auth = self.headers.get_all("Authorization", [])
            if len(auth) != 1 or not auth[0].startswith("Bearer "):
                raise APIError(401, "unauthorized")
            token = auth[0][7:]
            self.server.sessions.authorize(token)
            parsed = urlsplit(self.path)
            if not (parsed.path == "/api/runs" or parsed.path.startswith("/api/runs/")):
                raise APIError(404, "not_found")
            body = None
            if self.command == "POST":
                if self.headers.get_all("Transfer-Encoding"):
                    raise APIError(400, "unsupported_transfer_encoding")
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal() or len(lengths[0]) > 10:
                    raise APIError(400, "invalid_content_length")
                length = int(lengths[0])
                if length > 65536:
                    raise APIError(413, "body_too_large")
                if self.headers.get_content_type() != "application/json":
                    raise APIError(415, "expected_json")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise APIError(400, "incomplete_body")
                try:
                    body = json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)
                except (ValueError, UnicodeError, RecursionError) as error:
                    raise APIError(400, "invalid_json") from error
            suffix = parsed.path.removeprefix("/api/runs")
            parts = suffix[1:].split("/") if suffix else []
            status, result = self.server.sessions.handle(self.command, parts,
                parse_qs(parsed.query, keep_blank_values=True, max_num_fields=10), token, body)
            self.send_json(status, result)
        except APIError as error:
            self.close_connection = True
            self.send_json(error.status, {"error": error.code})
        except TimeoutError:
            self.close_connection = True
            self.send_json(408, {"error": "request_timeout"})
        except ValueError:
            self.close_connection = True
            self.send_json(400, {"error": "invalid_request"})

    def log_message(self, format, *args):
        # Do not log request URLs: they could accidentally contain sensitive input.
        pass


def create_server(port=8000, *, operator_token=None, clock=monotonic):
    sessions = SessionService(operator_token, clock=clock) if operator_token is not None else None
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.sessions = sessions
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    with create_server(args.port, operator_token=os.environ.get("DNH_OPERATOR_TOKEN")) as server:
        print(f"DO NO HARM local API: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
