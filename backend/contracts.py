"""Frozen 0.1 application-event validation. This is NOT authentication.

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


VOICE_FIELDS = {
    "schema_version", "kind", "run_id", "execution_version", "simulation_time_ms",
    "source_event_id", "say", "evidence_ids",
}
SPOKEN_STATES = {
    "paused": "Simulation paused. Wait for examiner instructions.",
    "coaching": "Coaching has started. This attempt is assisted.",
    "debrief": "Assessment feedback has started. This attempt is assisted.",
}
HIDDEN_RUBRIC_FIELDS = ("criterion_id", "rationale", "outcome", "requires_clinician_review")


def validate_event(event, *, producer):
    """Validate a JSON-like event against contract 0.1 and return a detached copy."""
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


def validate_voice_update(update):
    """Validate the Live-facing packet. It is not an application ledger event."""
    _require(isinstance(update, dict) and update.keys() == VOICE_FIELDS, "Missing or unknown voice fields")
    _require(update["schema_version"] == "0.1", "Unsupported schema version")
    _require(update["kind"] == "permitted_voice_update", "Invalid voice kind")
    for field in ("run_id", "source_event_id", "say"):
        _require(_text(update[field]), f"Invalid {field}")
    _require(type(update["simulation_time_ms"]) is int and update["simulation_time_ms"] >= 0, "Invalid simulation_time_ms")
    _require(type(update["execution_version"]) is int and update["execution_version"] >= 1, "Invalid execution_version")
    evidence = update["evidence_ids"]
    _require(isinstance(evidence, list) and all(_text(item) for item in evidence), "Invalid evidence IDs")
    _require(len(evidence) == len(set(evidence)), "Duplicate evidence IDs")
    for field in HIDDEN_RUBRIC_FIELDS:
        _require(field not in update, "Hidden rubric leaked into voice update")
    return deepcopy(update)


def project_permitted_voice_update(event):
    """Build the only payload a Live adapter may speak from an application event.

    Findings, unpublished scenario events and UI observations are not speakable.
    """
    _require(isinstance(event, dict), "Invalid event")
    _require(event.get("visibility") == "participant", "Hidden examiner content cannot be projected")
    _require(event.get("type") != "evaluation_finding", "Findings cannot be projected")
    payload = event.get("payload")
    _require(isinstance(payload, dict), "payload must be an object")
    kind = event.get("type")
    if kind == "clinical_update":
        _require(payload.get("delivery_stage") == "published", "Unreleased events cannot be spoken")
        say = payload.get("summary")
        _require(_text(say), "Invalid summary")
    elif kind == "session_state":
        say = SPOKEN_STATES.get(payload.get("state"))
        _require(say is not None, "Routine state changes are not spoken")
    else:
        raise ValueError("Event type is not speakable")
    return validate_voice_update({
        "schema_version": "0.1",
        "kind": "permitted_voice_update",
        "run_id": event["run_id"],
        "execution_version": event["execution_version"],
        "simulation_time_ms": event["simulation_time_ms"],
        "source_event_id": event["event_id"],
        "say": say,
        "evidence_ids": list(event.get("evidence_ids") or []),
    })
