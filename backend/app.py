"""Local scaffold only: liveness and shared contracts, no clinical/API execution."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]


class Handler(BaseHTTPRequestHandler):
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
        else:
            self.send_json(404, {"error": "not_found"})

    def do_POST(self):
        self.send_json(405, {"error": "method_not_allowed"}, allow="GET")

    def log_message(self, format, *args):
        # Do not log request URLs: they could accidentally contain sensitive input.
        pass


def create_server(port=8000):
    # Deliberately local-only. Production serving/authentication are separate work.
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    with create_server(args.port) as server:
        print(f"DO NO HARM scaffold: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
