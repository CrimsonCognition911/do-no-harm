"""Compile the hash-approved STEMI authoring package into the runtime contract."""
from hashlib import sha256
import json
from pathlib import Path

import yaml

from backend.adaptation import FrozenCase, canonical, require, text


CASE_KEY = "cases/stemi/case.yaml"
RUBRIC_KEY = "cases/stemi/README.md"


def _file_hash(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def _approved_sources(case_path, rubric_path, review_path):
    review = json.loads(Path(review_path).read_text())
    require(review.get("status") == "approved_by_user", "Clinical content approval is required")
    require(text(review.get("reviewer_github")), "Clinical reviewer identity is required")
    expected = review.get("approved_authoring_artifacts")
    require(isinstance(expected, dict), "Approved authoring hashes are required")
    require(expected.get(CASE_KEY) == _file_hash(case_path), "Case does not match its approved authoring hash")
    require(expected.get(RUBRIC_KEY) == _file_hash(rubric_path), "Rubric does not match its approved authoring hash")
    return review


def _event_summary(event):
    payload = event["payload"]
    for key in ("interpretation", "qualitative_result", "symptoms", "update"):
        if text(payload.get(key)):
            return payload[key]
    raise ValueError("Reviewed event has no participant-facing summary")


def _compile_event(event):
    trigger = event["trigger"]
    kind = event["kind"]
    compiled_kind = "optional_challenge" if kind == "optional_educational_challenge" else "clinical_consequence"
    if trigger["kind"] in ("confirmed_action", "confirmed_action_plus_delay"):
        schedule = {
            "basis": "state_transition",
            "state_ref": trigger["state_ref"],
            "delay_ms": event["publication_delay_ms"],
        }
        earliest = schedule["delay_ms"]
        latest = schedule["delay_ms"]
        requires = {trigger["state_ref"]: True}
    elif trigger["kind"] == "state_and_elapsed_time":
        earliest = trigger["earliest_ms"]
        latest = earliest
        schedule = {"basis": "simulation_time", "state_ref": None, "delay_ms": earliest}
        requires = trigger["requires"]
    elif trigger["kind"] == "examiner_selection":
        window = trigger["permitted_window_ms"]
        earliest, latest = window["earliest"], window["latest"]
        schedule = {"basis": "examiner_selection", "state_ref": None, "delay_ms": 0}
        requires = trigger["requires"]
    else:
        raise ValueError("Unsupported reviewed event trigger")

    set_state = {"diagnostic_ecg_published": True} if event["id"] == "diagnostic_ecg" else {}
    return {
        "id": event["id"],
        "kind": compiled_kind,
        "summary": _event_summary(event),
        "resource_key": event["resource_key"],
        "earliest_ms": earliest,
        "latest_ms": latest,
        "requires": requires,
        "difficulty": event.get("difficulty", 0),
        "criterion_id": "mi.reperfusion_escalation" if compiled_kind == "optional_challenge" else None,
        "outcome": "acceptable" if compiled_kind == "optional_challenge" else None,
        "allowed_modes": ["coached", "assessment"],
        "schedule": schedule,
        "set_state": set_state,
    }


def compile_reviewed_case(case_path, rubric_path, review_path):
    """Return a deterministic compiled document after verifying reviewed bytes."""
    _approved_sources(case_path, rubric_path, review_path)
    authoring = yaml.safe_load(Path(case_path).read_text())
    require(isinstance(authoring, dict) and authoring.get("schema_version") == 3, "Unsupported authoring schema")
    require(authoring.get("synthetic_only") is True, "Only synthetic cases may compile")
    require(authoring.get("clinical_review", {}).get("required") is True, "Clinical review gate is required")
    rubric = authoring["rubric_contract"]
    criteria = [
        {
            "id": criterion["id"],
            "description": (
                f"{criterion['clinical_judgment']} Evidence: "
                f"{', '.join(criterion['objective_observations'])}."
            ),
        }
        for criterion in rubric["criteria"]
    ]
    state = authoring["state_contract"]["initial"]
    action_states = {
        "confirm_ecg_acquisition": "ecg_acquired",
        "confirm_blood_sample_collection": "blood_sample_collected",
        "confirm_reperfusion_pathway_activation": "reperfusion_pathway_activated",
        "confirm_receiving_team_handoff": "handoff_completed",
    }
    require(all(name in state for name in action_states.values()), "Reviewed state contract is incomplete")
    actions = [
        {"action": action, "set_state": {state_name: True}}
        for action, state_name in action_states.items()
    ]
    policy = authoring["adaptation"]
    document = {
        "format": "dnh.compiled-case/0.2",
        "id": authoring["id"],
        "version": authoring["version"],
        "description": authoring["title"],
        "rubric": {
            "id": "stemi-clinical-rubric",
            "version": rubric["version"],
            "criteria": criteria,
            "outcomes": rubric["outcomes"],
            "requires_clinician_review": rubric["requires_clinician_review"],
            "numeric_pass_fail_score_enabled": rubric["numeric_pass_fail_score_enabled"],
            "alternatives_policy": rubric["alternatives_policy"],
        },
        "policy": {
            "version": policy["version"],
            "min_difficulty": 1,
            "max_difficulty": max(event.get("difficulty", 1) for event in authoring["events"]),
            "max_pending_challenges": policy["max_pending_challenges"],
        },
        "initial_state": state,
        "actions": actions,
        "events": [_compile_event(event) for event in authoring["events"]],
    }
    # Validation here proves the compiler emitted a contract accepted by runtime.
    FrozenCase(document, fixture=True)
    return document


def load_reviewed_case(compiled_path, review_path):
    """Load a compiled case only when its canonical hash matches the review record."""
    document = json.loads(Path(compiled_path).read_text())
    review = json.loads(Path(review_path).read_text())
    approved_hash = review.get("compiled_runtime_artifact_sha256")
    require(text(approved_hash), "Compiled runtime artifact has not been frozen")
    require(sha256(canonical(document).encode()).hexdigest() == approved_hash, "Compiled runtime artifact hash mismatch")
    return FrozenCase(document, approved_sha256=approved_hash)
