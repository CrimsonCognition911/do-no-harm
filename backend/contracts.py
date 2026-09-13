"""Draft 0.1 application-event validation. This is NOT authentication.

Only a trusted server adapter may supply ``producer`` after authenticating its
connection. Never copy that argument from a request body. Resource ownership and
evidence membership belong to the run, not to this shape validator.
"""
from copy import deepcopy
from datetime import datetime
import re


ACTORS = {
    "doctor_action": "doctor", "clinical_update": "simulation_service",
    "evaluation_finding": "examiner", "session_state": "simulation_service",
}
STATES = {"created", "running", "pause_requested", "paused", "coaching", "resume_requested", "debrief", "ended", "failed"}
ENVELOPE_FIELDS = {"schema_version", "type", "event_id", "run_id", "actor", "occurred_at", "simulation_time_ms", "execution_version", "evidence_ids", "visibility", "payload"}
DATE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value):
    return isinstance(value, str) and len(value) > 0


def _choice(value, choices, field):
    _require(isinstance(value, str) and value in choices, f"Invalid {field}")


def _fields(payload, required, optional=()):
    _require(isinstance(payload, dict), "payload must be an object")
    _require(set(required) <= payload.keys() <= set(required) | set(optional), "Missing or unknown payload fields")


def validate_event(event, *, producer):
    """Validate a JSON-like event against draft 0.1 and return a detached copy."""
    _require(isinstance(event, dict) and event.keys() == ENVELOPE_FIELDS, "Missing or unknown envelope fields")
    _require(event["schema_version"] == "0.1", "Unsupported schema version")
    kind = event["type"]
    _choice(kind, ACTORS, "event type")
    _require(event["actor"] == ACTORS[kind], "Actor does not match event type")
    for field in ("event_id", "run_id"):
        _require(_text(event[field]), f"Invalid {field}")
    for field, minimum in (("simulation_time_ms", 0), ("execution_version", 1)):
        _require(type(event[field]) is int and event[field] >= minimum, f"Invalid {field}")
    timestamp = event["occurred_at"]
    _require(isinstance(timestamp, str) and DATE_TIME.fullmatch(timestamp), "Expected timezone-aware RFC3339 timestamp")
    try:
        datetime.fromisoformat(timestamp.upper().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Invalid timestamp") from error
    evidence = event["evidence_ids"]
    _require(isinstance(evidence, list) and all(_text(item) for item in evidence), "Invalid evidence IDs")
    _require(len(evidence) == len(set(evidence)), "Duplicate evidence IDs")
    _choice(event["visibility"], {"participant", "examiner"}, "visibility")
    payload = event["payload"]
    if kind == "doctor_action":
        _fields(payload, {"action", "phase", "source"}, {"resource_ref"})
        _require(_text(payload["action"]), "Invalid action")
        _choice(payload["phase"], {"observed", "intent", "confirmed"}, "phase")
        _choice(payload["source"], {"browser", "speech", "openmrs_backend"}, "source")
        _require(producer == payload["source"], "Producer cannot claim this action source")
        if "resource_ref" in payload:
            _require(_text(payload["resource_ref"]), "Invalid resource reference")
        if payload["phase"] == "confirmed":
            _require(producer == "openmrs_backend" and "resource_ref" in payload, "Confirmed action requires backend resource evidence")
    elif kind == "clinical_update":
        _require(producer == "simulation_service", "Only simulation service can publish clinical updates")
        _fields(payload, {"scenario_event_id", "summary", "resource_ref", "delivery_stage"})
        for field in ("scenario_event_id", "summary", "resource_ref"):
            _require(_text(payload[field]), f"Invalid {field}")
        _choice(payload["delivery_stage"], {"published", "displayed", "spoken"}, "delivery stage")
    elif kind == "evaluation_finding":
        _require(producer == "examiner" and event["visibility"] == "examiner", "Findings are examiner-only")
        _fields(payload, {"criterion_id", "outcome", "rationale", "requires_clinician_review"})
        for field in ("criterion_id", "rationale"):
            _require(_text(payload[field]), f"Invalid {field}")
        _choice(payload["outcome"], {"acceptable", "concern", "insufficient_evidence"}, "outcome")
        _require(type(payload["requires_clinician_review"]) is bool, "Review flag must be boolean")
    else:
        _require(producer == "simulation_service", "Only simulation service can publish session state")
        _fields(payload, {"state", "mode", "assisted"}, {"reason"})
        _choice(payload["state"], STATES, "state")
        _choice(payload["mode"], {"coached", "assessment"}, "mode")
        _require(type(payload["assisted"]) is bool, "Assistance flag must be boolean")
        if "reason" in payload:
            _require(_text(payload["reason"]), "Invalid reason")
    return deepcopy(event)
