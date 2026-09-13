"""Loopback doctor-experience BFF for the synthetic DO NO HARM workspace."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from secrets import token_urlsafe
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from backend.contracts import validate_event
PARTICIPANT_TYPES = {"doctor_action", "clinical_update", "session_state"}
MAX_BODY = 65536


class BFFError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def exact(body, fields):
    if not isinstance(body, dict) or body.keys() != set(fields):
        raise BFFError(422, "missing_or_unknown_fields")


def bounded_text(value, *, maximum=4000):
    return isinstance(value, str) and 0 < len(value) <= maximum


def participant_event(event, *, run_id=None):
    if not (isinstance(event, dict) and event.get("visibility") == "participant" and event.get("type") in PARTICIPANT_TYPES):
        return False
    if run_id is not None and event.get("run_id") != run_id:
        return False
    producer = event.get("payload", {}).get("source") if event.get("type") == "doctor_action" else "simulation_service"
    try:
        validate_event(event, producer=producer)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


class JSONClient:
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, _request, _file_pointer, _code, _message, _headers, _new_url):
            return None

    def request(self, url, *, method="GET", token=None, body=None, timeout=10):
        headers = {"Accept": "application/json"}
        data = None
        if token:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, separators=(",", ":")).encode()
        try:
            with build_opener(self.NoRedirect).open(Request(url, data=data, headers=headers, method=method), timeout=timeout) as response:
                raw = response.read()
                try:
                    return response.status, json.loads(raw) if raw else {}
                except (ValueError, UnicodeError) as error:
                    raise BFFError(502, "invalid_upstream_response") from error
        except HTTPError as error:
            try:
                payload = json.loads(error.read())
            except (ValueError, UnicodeError):
                payload = {"error": "upstream_error"}
            return error.code, payload
        except (URLError, TimeoutError, OSError) as error:
            raise BFFError(503, "upstream_unavailable") from error


class SessionGateway:
    """Maps one local browser to one run; capabilities never leave this object."""
    def __init__(self, base_url, run_id, doctor_token, audio_token, *, client=None):
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Session API must be an explicit loopback HTTP URL")
        if not all(bounded_text(value, maximum=256) for value in (run_id, doctor_token, audio_token)):
            raise ValueError("Run and capability configuration is required")
        self.base = base_url.rstrip("/")
        self.run_id, self.doctor_token, self.audio_token = run_id, doctor_token, audio_token
        self.client = client or JSONClient()

    def _url(self, suffix=""):
        return f"{self.base}/api/runs/{self.run_id}{suffix}"

    def feed(self, after):
        status, result = self.client.request(self._url(f"/events?after={after}"), token=self.doctor_token)
        if status != 200:
            raise BFFError(status, result.get("error", "session_api_error") if isinstance(result, dict) else "session_api_error")
        if (not isinstance(result, dict) or result.get("run_id") != self.run_id or
                not isinstance(result.get("events"), list) or type(result.get("next_cursor")) is not int or result["next_cursor"] < after):
            raise BFFError(502, "invalid_session_feed")
        events = [event for event in result["events"] if participant_event(event, run_id=self.run_id)]
        return {key: result[key] for key in ("run_id", "instance_id", "state", "execution_version", "simulation_time_ms", "assisted", "review_allowed", "next_cursor") if key in result} | {"events": events}

    def action(self, body):
        exact(body, {"event_id", "execution_version", "action", "kind"})
        if not bounded_text(body["event_id"], maximum=128) or not bounded_text(body["action"]):
            raise BFFError(422, "invalid_action")
        if type(body["execution_version"]) is not int or body["execution_version"] < 1:
            raise BFFError(422, "invalid_execution_version")
        source_phase = {"browser": ("browser", "observed"), "speech": ("speech", "intent")}
        if body["kind"] not in source_phase:
            raise BFFError(422, "invalid_action_kind")
        source, phase = source_phase[body["kind"]]
        upstream = {"event_id": body["event_id"], "execution_version": body["execution_version"],
                    "payload": {"action": body["action"], "source": source, "phase": phase}}
        status, result = self.client.request(self._url("/actions"), method="POST", token=self.doctor_token, body=upstream)
        if status != 200:
            raise BFFError(status, result.get("error", "session_api_error") if isinstance(result, dict) else "session_api_error")
        if not participant_event(result, run_id=self.run_id):
            raise BFFError(502, "invalid_upstream_event")
        return result

    def audio_ack(self, body):
        exact(body, {"request_id", "execution_version", "transition"})
        if body["transition"] not in {"pause", "resume"}:
            raise BFFError(422, "invalid_transition")
        status, result = self.client.request(self._url("/acks"), method="POST", token=self.audio_token, body=body)
        if status != 200:
            raise BFFError(status, result.get("error", "session_api_error") if isinstance(result, dict) else "session_api_error")
        return result


class OpenAILive:
    def __init__(self, api_key, *, client=None, endpoint="https://api.openai.com/v1/live/sessions"):
        if not bounded_text(api_key, maximum=512):
            raise ValueError("OPENAI_API_KEY is required")
        if endpoint != "https://api.openai.com/v1/live/sessions":
            raise ValueError("Unexpected Live API endpoint")
        self.api_key, self.client, self.endpoint = api_key, client or JSONClient(), endpoint

    def create(self, sdp):
        if not bounded_text(sdp, maximum=60000):
            raise BFFError(422, "invalid_sdp")
        body = {
            "session": {
                "model": "gpt-live-1",
                "instructions": (
                    "You are the voice interface for a synthetic emergency-medicine simulation. "
                    "Treat speech as intent, never as confirmed clinical execution. Delegate requests needing "
                    "case reasoning or authoritative state. Announce only application-confirmed participant-visible "
                    "updates. During a pause, stop clinical speech and wait for an explicit running update. "
                    "Never request or reveal rubrics, examiner findings, or unreleased events."
                ),
                "delegation": {"type": "client"},
            },
            "transport": {"type": "webrtc", "sdp": sdp},
        }
        status, result = self.client.request(self.endpoint, method="POST", token=self.api_key, body=body, timeout=20)
        if status not in (200, 201):
            raise BFFError(status if 400 <= status < 600 else 502, "live_session_failed")
        session_id = result.get("session", {}).get("id")
        transport = result.get("transport", {})
        if not bounded_text(session_id, maximum=256) or transport.get("type") != "webrtc" or not bounded_text(transport.get("sdp"), maximum=60000):
            raise BFFError(502, "invalid_live_response")
        return {"session": {"id": session_id}, "transport": {"type": "webrtc", "sdp": transport["sdp"]}}

    def hangup(self, session_id):
        if not bounded_text(session_id, maximum=256):
            raise BFFError(422, "invalid_live_session")
        status, _result = self.client.request(
            f"https://api.openai.com/v1/live/sessions/{quote(session_id, safe='')}/hangup",
            method="POST", token=self.api_key, timeout=10,
        )
        if status not in (200, 204):
            raise BFFError(status if 400 <= status < 600 else 502, "live_hangup_failed")
        return {"ended": True}


class ExaminerBridge:
    """Optional dependency-owned adapter. Only a small participant-safe result crosses back."""
    def __init__(self, url=None, token=None, *, client=None):
        self.url, self.token, self.client = url, token, client or JSONClient()
        if bool(url) != bool(token):
            raise ValueError("Examiner bridge URL and token must be configured together")
        if url:
            parsed = urlsplit(url)
            secure = parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"})
            if not secure or parsed.username or parsed.password or parsed.fragment:
                raise ValueError("Invalid examiner bridge URL")

    @property
    def available(self):
        return bool(self.url)

    def delegate(self, run_id, body):
        if not self.available:
            raise BFFError(503, "examiner_bridge_unavailable")
        status, result = self.client.request(self.url, method="POST", token=self.token,
                                             body={"type": "live_delegation", "run_id": run_id, **body}, timeout=20)
        if status != 200 or not isinstance(result, dict):
            raise BFFError(status if 400 <= status < 600 else 502, "examiner_bridge_failed")
        safe = {"status": result.get("status"), "spoken_update": result.get("spoken_update"),
                "evidence_ids": result.get("evidence_ids", [])}
        if safe["status"] not in {"complete", "needs_clarification", "unavailable"} or not bounded_text(safe["spoken_update"], maximum=2000):
            raise BFFError(502, "invalid_examiner_result")
        if not isinstance(safe["evidence_ids"], list) or not all(bounded_text(item, maximum=256) for item in safe["evidence_ids"]):
            raise BFFError(502, "invalid_examiner_result")
        allowed = set(body.get("participant_event_ids", []))
        if not set(safe["evidence_ids"]) <= allowed:
            raise BFFError(502, "examiner_referenced_hidden_evidence")
        return safe

    def delivery(self, run_id, body):
        if not self.available:
            raise BFFError(503, "examiner_bridge_unavailable")
        status, result = self.client.request(self.url, method="POST", token=self.token,
                                             body={"type": "delivery_ack", "run_id": run_id, **body}, timeout=10)
        if status != 200:
            raise BFFError(status if 400 <= status < 600 else 502, "delivery_ack_failed")
        return {"accepted": result.get("accepted") is True}


class DoctorHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def reply(self, status, payload, *, content_type="application/json; charset=utf-8"):
        encoded = payload if isinstance(payload, bytes) else json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; media-src 'self' blob:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(encoded)

    def _trusted_host(self):
        hosts = self.headers.get_all("Host", [])
        return len(hosts) == 1 and hosts[0] in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

    def _read_json(self):
        if self.headers.get_all("Transfer-Encoding") or self.headers.get_content_type() != "application/json":
            raise BFFError(415, "expected_json")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
            raise BFFError(400, "invalid_content_length")
        length = int(lengths[0])
        if length > MAX_BODY:
            raise BFFError(413, "body_too_large")
        try:
            return json.loads(self.rfile.read(length), object_pairs_hook=unique_object,
                              parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError) as error:
            raise BFFError(400, "invalid_json") from error

    def _guard_post(self):
        origins = self.headers.get_all("Origin", [])
        allowed = {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}
        if len(origins) != 1 or origins[0] not in allowed:
            raise BFFError(403, "untrusted_origin")
        csrf = self.headers.get_all("X-DNH-CSRF", [])
        if len(csrf) != 1 or csrf[0] != self.server.csrf:
            raise BFFError(403, "invalid_csrf")

    def do_GET(self):
        try:
            if not self._trusted_host():
                raise BFFError(403, "untrusted_host")
            parsed = urlsplit(self.path)
            if parsed.path == "/":
                self.reply(200, (ROOT / "index.html").read_bytes(), content_type="text/html; charset=utf-8")
            elif parsed.path in {"/app.js", "/voice_controller.mjs"}:
                self.reply(200, (ROOT / parsed.path[1:]).read_bytes(), content_type="text/javascript; charset=utf-8")
            elif parsed.path == "/styles.css":
                self.reply(200, (ROOT / "styles.css").read_bytes(), content_type="text/css; charset=utf-8")
            elif parsed.path == "/api/bootstrap" and not parsed.query:
                self.reply(200, {"csrf": self.server.csrf, "run_id": self.server.gateway.run_id,
                                 "live_available": self.server.live is not None,
                                 "examiner_available": self.server.bridge.available,
                                 "retention": "session_only"})
            elif parsed.path == "/api/events":
                query = parsed.query
                if not query.startswith("after=") or not query[6:].isascii() or not query[6:].isdecimal():
                    raise BFFError(422, "invalid_cursor")
                feed = self.server.gateway.feed(int(query[6:]))
                self.server.execution_version = feed.get("execution_version")
                for event in feed["events"]:
                    self.server.participant_event_ids.add(event["event_id"])
                    if event["type"] == "clinical_update" and event["payload"]["delivery_stage"] == "published":
                        self.server.publications[event["event_id"]] = event["execution_version"]
                self.reply(200, feed)
            else:
                raise BFFError(404, "not_found")
        except BFFError as error:
            self.reply(error.status, {"error": error.code})

    def do_POST(self):
        try:
            if not self._trusted_host():
                raise BFFError(403, "untrusted_host")
            self._guard_post()
            parsed = urlsplit(self.path)
            if parsed.query:
                raise BFFError(422, "unexpected_query")
            body = self._read_json()
            if parsed.path == "/api/consent":
                exact(body, {"recording", "retention"})
                if type(body["recording"]) is not bool or body["retention"] != "session_only":
                    raise BFFError(422, "invalid_consent")
                self.server.recording_consent = body["recording"]
                self.reply(200, {"recording": body["recording"], "retention": "session_only"})
            elif parsed.path == "/api/live/session":
                exact(body, {"sdp"})
                if not self.server.recording_consent:
                    raise BFFError(409, "recording_consent_required")
                if self.server.live is None:
                    raise BFFError(503, "live_provider_unavailable")
                result = self.server.live.create(body["sdp"])
                self.server.live_sessions.add(result["session"]["id"])
                self.reply(201, result)
            elif parsed.path == "/api/live/hangup":
                exact(body, {"session_id"})
                if self.server.live is None or body["session_id"] not in self.server.live_sessions:
                    raise BFFError(404, "unknown_live_session")
                result = self.server.live.hangup(body["session_id"])
                self.server.live_sessions.discard(body["session_id"])
                self.reply(200, result)
            elif parsed.path == "/api/actions":
                exact(body, {"event_id", "execution_version", "action", "kind"})
                self.reply(200, self.server.gateway.action(body))
            elif parsed.path == "/api/audio/ack":
                self.reply(200, self.server.gateway.audio_ack(body))
            elif parsed.path == "/api/delegations":
                exact(body, {"delegation_id", "offset_ms", "execution_version", "transcript"})
                if not bounded_text(body["delegation_id"], maximum=256) or type(body["offset_ms"]) is not int or body["offset_ms"] < 0:
                    raise BFFError(422, "invalid_delegation")
                if type(body["execution_version"]) is not int or not isinstance(body["transcript"], list) or len(body["transcript"]) > 300:
                    raise BFFError(422, "invalid_delegation")
                if self.server.execution_version is not None and body["execution_version"] != self.server.execution_version:
                    raise BFFError(409, "stale_execution_version")
                allowed_transcript_fields = {"speaker", "text", "partial", "corrected", "source", "start_ms", "end_ms"}
                for item in body["transcript"]:
                    if (not isinstance(item, dict) or not set(item) <= allowed_transcript_fields or
                            item.get("speaker") not in {"doctor", "assistant"} or not bounded_text(item.get("text"))):
                        raise BFFError(422, "invalid_transcript")
                    for flag in ("partial", "corrected"):
                        if flag in item and type(item[flag]) is not bool:
                            raise BFFError(422, "invalid_transcript")
                    for timestamp in ("start_ms", "end_ms"):
                        if (timestamp in item and item[timestamp] is not None and
                                (type(item[timestamp]) not in (int, float) or item[timestamp] < 0 or item[timestamp] != item[timestamp])):
                            raise BFFError(422, "invalid_transcript")
                    if "source" in item and item["source"] != "typed":
                        raise BFFError(422, "invalid_transcript")
                request = {**body, "participant_event_ids": sorted(self.server.participant_event_ids)[-1000:]}
                self.reply(200, self.server.bridge.delegate(self.server.gateway.run_id, request))
            elif parsed.path == "/api/delivery":
                exact(body, {"event_id", "execution_version", "stage"})
                if not bounded_text(body["event_id"], maximum=256) or body["stage"] not in {"displayed", "spoken"} or type(body["execution_version"]) is not int:
                    raise BFFError(422, "invalid_delivery")
                if self.server.publications.get(body["event_id"]) != body["execution_version"]:
                    raise BFFError(409, "unknown_publication")
                self.reply(200, self.server.bridge.delivery(self.server.gateway.run_id, body))
            else:
                raise BFFError(404, "not_found")
        except BFFError as error:
            self.reply(error.status, {"error": error.code})

    def log_message(self, _format, *_args):
        pass


def create_server(port, *, gateway, live=None, bridge=None):
    server = ThreadingHTTPServer(("127.0.0.1", port), DoctorHandler)
    server.gateway, server.live, server.bridge = gateway, live, bridge or ExaminerBridge()
    server.csrf, server.recording_consent = token_urlsafe(32), False
    server.execution_version, server.participant_event_ids, server.publications = None, set(), {}
    server.live_sessions = set()
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()
    gateway = SessionGateway(os.environ.get("DNH_SESSION_API", "http://127.0.0.1:8000"),
                             os.environ.get("DNH_RUN_ID", ""), os.environ.get("DNH_DOCTOR_CAPABILITY", ""),
                             os.environ.get("DNH_AUDIO_CAPABILITY", ""))
    live = OpenAILive(os.environ["OPENAI_API_KEY"]) if os.environ.get("OPENAI_API_KEY") else None
    bridge = ExaminerBridge(os.environ.get("DNH_EXAMINER_BRIDGE_URL"), os.environ.get("DNH_EXAMINER_BRIDGE_TOKEN"))
    with create_server(args.port, gateway=gateway, live=live, bridge=bridge) as server:
        print(f"Doctor interface: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
