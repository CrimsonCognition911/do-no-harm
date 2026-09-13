"""Local synthetic-session transport. Capabilities are not a production login system."""
from copy import deepcopy
from hashlib import sha256
from secrets import token_urlsafe
from threading import RLock
from time import monotonic
from uuid import uuid4

from backend.contracts import project_permitted_voice_update
from backend.runtime import Run


class APIError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


def fields(body, required):
    if not isinstance(body, dict) or body.keys() != set(required):
        raise APIError(422, "missing_or_unknown_fields")


def request_key(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise APIError(422, "invalid_request_id")
    return value


def digest(token):
    return sha256(token.encode("utf-8")).hexdigest()


class SessionService:
    """All run access is serialized here; no external I/O occurs under the lock.

    Only offline fixtures can be created. Real patient/visit binding, provider
    sessions and clinical writes are intentionally unavailable through this API.
    """
    def __init__(self, operator_token, *, clock=monotonic):
        if not isinstance(operator_token, str) or not 32 <= len(operator_token) <= 256:
            raise ValueError("Operator token must have 32 to 256 characters")
        self._operator = digest(operator_token)
        self._clock, self._lock = clock, RLock()
        self._runs, self._capabilities, self._creations = {}, {}, {}
        self.instance_id = str(uuid4())

    def _identity(self, token):
        if not isinstance(token, str) or not 1 <= len(token) <= 256:
            raise APIError(401, "unauthorized")
        hashed = digest(token)
        if hashed == self._operator:
            return "operator", None
        entry = self._capabilities.get(hashed)
        if entry is None or self._clock() >= entry[2]:
            raise APIError(401, "unauthorized")
        return entry[:2]

    def authorize(self, token):
        with self._lock:
            self._identity(token)

    def handle(self, method, parts, query, token, body):
        with self._lock:
            role, bound_run = self._identity(token)
            if not parts and method == "POST":
                if role != "operator":
                    raise APIError(403, "forbidden")
                if query:
                    raise APIError(422, "unexpected_query")
                return self._create(body)
            if not parts:
                raise APIError(404, "not_found")
            run_id = parts[0]
            if bound_run != run_id:
                raise APIError(403, "forbidden")
            entry = self._runs[run_id]
            run = entry["run"]
            if method == "GET":
                if len(parts) == 1 and not query:
                    return 200, self._snapshot(run)
                if parts[1:] == ["events"]:
                    if set(query) - {"after"} or len(query.get("after", [])) > 1:
                        raise APIError(422, "invalid_cursor")
                    cursor = query.get("after", ["0"])[0]
                    if not cursor.isascii() or not cursor.isdecimal() or len(cursor) > 10:
                        raise APIError(422, "invalid_cursor")
                    after = int(cursor)
                    events = run.events(audience="examiner" if role == "examiner" else "participant")
                    if after > len(events):
                        raise APIError(409, "cursor_out_of_range")
                    page = events[after:after + 200]
                    return 200, {**self._snapshot(run), "events": page, "next_cursor": after + len(page)}
                if parts[1:] == ["voice"]:
                    if role != "audio":
                        raise APIError(403, "forbidden")
                    if query:
                        raise APIError(422, "unexpected_query")
                    updates = []
                    for event in run.events(audience="participant"):
                        try:
                            updates.append(project_permitted_voice_update(event))
                        except ValueError:
                            continue
                    return 200, {**self._snapshot(run), "updates": updates}
                raise APIError(422 if query else 404, "invalid_request")
            if method != "POST" or len(parts) != 2:
                raise APIError(404, "not_found")
            if query:
                raise APIError(422, "unexpected_query")
            route = parts[1]
            if route == "actions":
                if role != "doctor":
                    raise APIError(403, "forbidden")
                fields(body, {"event_id", "execution_version", "payload"})
                payload = body["payload"]
                if not isinstance(payload, dict):
                    raise APIError(422, "invalid_payload")
                source, phase = payload.get("source"), payload.get("phase")
                if (source, phase) not in (("browser", "observed"), ("speech", "intent")):
                    raise APIError(403, "untrusted_action_source")
                return self._record(run, body, "doctor_action", source, "participant", [])
            if route == "findings":
                if role != "examiner":
                    raise APIError(403, "forbidden")
                fields(body, {"event_id", "execution_version", "payload", "evidence_ids"})
                return self._record(run, body, "evaluation_finding", "examiner", "examiner", body["evidence_ids"])
            if route == "commands":
                if role != "examiner":
                    raise APIError(403, "forbidden")
                fields(body, {"request_id", "command", "execution_version"})
                commands = {"start": run.start, "pause": run.request_pause, "coach": run.begin_coaching,
                            "resume": run.request_resume, "end": run.end}
                command = body["command"]
                if not isinstance(command, str) or command not in commands:
                    raise APIError(422, "unsupported_command")
                return self._command(entry, role, route, body, commands[command])
            if route == "acks":
                if role not in ("execution", "audio"):
                    raise APIError(403, "forbidden")
                fields(body, {"request_id", "transition", "execution_version"})
                transition = body["transition"]
                if transition not in ("pause", "resume"):
                    raise APIError(422, "unsupported_transition")
                method = run.acknowledge_pause if transition == "pause" else run.acknowledge_resume
                return self._command(entry, role, route, body, lambda **args: method(role, **args))
            raise APIError(404, "not_found")

    def _create(self, body):
        fields(body, {"request_id", "mode"})
        key = request_key(body["request_id"])
        if body["mode"] not in ("coached", "assessment"):
            raise APIError(422, "invalid_mode")
        if key in self._creations:
            previous, result, expires = self._creations[key]
            if previous != body or self._clock() >= expires:
                raise APIError(409, "creation_conflict_or_expired")
            return 200, deepcopy(result)
        if len(self._runs) >= 100:
            raise APIError(503, "local_session_capacity")
        run_id = str(uuid4())
        # No arbitrary OpenMRS resource identifiers may be supplied by HTTP clients.
        run = Run(run_id, resource_refs=(), mode=body["mode"], clock=self._clock)
        self._runs[run_id] = {"run": run, "commands": {}}
        expires = self._clock() + 3600
        tokens = {}
        for role in ("doctor", "examiner", "execution", "audio"):
            token = token_urlsafe(32)
            tokens[role] = token
            self._capabilities[digest(token)] = (role, run_id, expires)
        result = {**self._snapshot(run), "tokens": tokens, "expires_in_seconds": 3600}
        self._creations[key] = (deepcopy(body), deepcopy(result), expires)
        return 201, result

    def _snapshot(self, run):
        return {"run_id": run.events(audience="participant")[0]["run_id"],
                "instance_id": self.instance_id, "environment": "offline_fixture",
                "state": run.state, "execution_version": run.execution_version,
                "simulation_time_ms": run.simulation_time_ms,
                "assisted": run.assisted, "review_allowed": run.review_allowed}

    def _record(self, run, body, kind, producer, visibility, evidence_ids):
        if len(run.events()) >= 10000:
            raise APIError(503, "local_evidence_capacity")
        try:
            event = run.record(event_id=body["event_id"], expected_version=body["execution_version"],
                               kind=kind, producer=producer, payload=body["payload"],
                               evidence_ids=evidence_ids, visibility=visibility)
        except ValueError as error:
            raise APIError(409, "event_rejected") from error
        return 200, event

    def _command(self, entry, role, route, body, callback):
        if type(body["execution_version"]) is not int or body["execution_version"] < 1:
            raise APIError(422, "invalid_execution_version")
        key = (role, request_key(body["request_id"]))
        request = (route, body)
        if key in entry["commands"]:
            previous, result = entry["commands"][key]
            if previous != request:
                raise APIError(409, "request_id_conflict")
            return 200, deepcopy(result)
        if len(entry["commands"]) >= 10000:
            raise APIError(503, "local_command_capacity")
        try:
            callback(expected_version=body["execution_version"])
        except ValueError as error:
            raise APIError(409, "transition_rejected") from error
        result = self._snapshot(entry["run"])
        entry["commands"][key] = (deepcopy(request), deepcopy(result))
        return 200, result
