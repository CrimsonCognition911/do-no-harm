"""Authoritative, synthetic-only OpenMRS publication for DNH-06."""

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock


class ExternalPublicationError(RuntimeError):
    """An external write or its authoritative read-back was not verified."""


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _uuid(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty")
    return value


class PublicationLedger:
    """SQLite receipt ledger; its transaction serializes external publications."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS publication_receipts ("
                "run_id TEXT NOT NULL, scenario_event_id TEXT NOT NULL, "
                "fingerprint TEXT NOT NULL, receipt TEXT NOT NULL, "
                "PRIMARY KEY (run_id, scenario_event_id))"
            )
        finally:
            connection.close()
        os.chmod(self.path, 0o600)

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30, isolation_level=None)

    def get(self, run_id, event_id):
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT receipt FROM publication_receipts WHERE run_id=? AND scenario_event_id=?",
                (run_id, event_id),
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else json.loads(row[0])

    def execute_once(self, run_id, event_id, fingerprint, operation):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT fingerprint, receipt FROM publication_receipts "
                "WHERE run_id=? AND scenario_event_id=?",
                (run_id, event_id),
            ).fetchone()
            if row is not None:
                if row[0] != fingerprint:
                    raise ValueError("Publication identity conflicts with durable receipt")
                connection.commit()
                return json.loads(row[1])
            receipt = operation()
            connection.execute(
                "INSERT INTO publication_receipts "
                "(run_id, scenario_event_id, fingerprint, receipt) VALUES (?, ?, ?, ?)",
                (run_id, event_id, fingerprint, _canonical(receipt)),
            )
            connection.commit()
            return deepcopy(receipt)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


class OpenMRSExecutor:
    """Revalidates server plans, writes one encounter, and verifies its read-back."""

    _BINDING_FIELDS = {
        "fixture_only", "run_id", "patient_uuid", "visit_uuid",
        "encounter_type_uuid", "location_uuid", "provider_uuid",
        "simulation_user_uuid", "encounter_role_uuid", "concept_uuids",
    }
    _FAILURE_COMPONENTS = {"openmrs", "examiner_model", "voice_model", "voice_transport"}

    def __init__(self, run, planner, client, *, binding, ledger_path):
        if not isinstance(binding, dict) or set(binding) != self._BINDING_FIELDS:
            raise ValueError("Missing or unknown authoritative binding fields")
        if binding["fixture_only"] is not True:
            raise ValueError("Only explicitly synthetic OpenMRS bindings are supported")
        run_id = run.events()[0]["run_id"]
        if binding["run_id"] != run_id:
            raise ValueError("OpenMRS binding belongs to another run")
        for field in self._BINDING_FIELDS - {"fixture_only", "concept_uuids"}:
            _uuid(binding[field], field)
        concepts = binding["concept_uuids"]
        if not isinstance(concepts, list) or not concepts or len(concepts) != len(set(concepts)):
            raise ValueError("concept_uuids must be a nonempty unique list")
        if any(not isinstance(value, str) or not value for value in concepts):
            raise ValueError("Invalid concept UUID")
        self._run, self._planner, self._client = run, planner, client
        self._binding = deepcopy(binding)
        self._run_id = run_id
        self._ledger = PublicationLedger(ledger_path)
        self._lock = RLock()

    def publish_due(self, event_id, *, expected_version):
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("scenario event ID is required")
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("Invalid execution version")
        with self._lock:
            with self._run.execution_guard(expected_version=expected_version):
                try:
                    prior = self._ledger.get(self._run_id, event_id)
                except Exception as error:
                    self._external_error(error, expected_version, "Publication ledger read failed")
                if prior is not None:
                    try:
                        return self._recover_receipt(prior, event_id, expected_version)
                    except Exception as error:
                        self._external_error(error, expected_version, "OpenMRS receipt recovery failed")
                plan = self._authoritative_plan(event_id, expected_version)
                fingerprint = sha256(_canonical(self._identity(plan)).encode()).hexdigest()
                marker = "DNH06:" + fingerprint
                try:
                    receipt = self._ledger.execute_once(
                        self._run_id,
                        event_id,
                        fingerprint,
                        lambda: self._publish_and_verify(plan, marker, expected_version),
                    )
                    return self._recover_receipt(receipt, event_id, expected_version)
                except Exception as error:
                    self._external_error(error, expected_version, "OpenMRS publication failed")

    def _external_error(self, error, expected_version, message):
        self._pause_if_current(expected_version)
        if isinstance(error, ExternalPublicationError):
            raise error
        raise ExternalPublicationError(message + "; simulation paused") from error

    def _authoritative_plan(self, event_id, expected_version):
        due = self._planner.due_events(expected_version=expected_version)
        matches = [plan for plan in due if plan["event_id"] == event_id]
        if len(matches) != 1:
            raise ValueError("Event is not currently due in the authoritative plan")
        plan = matches[0]
        if plan["run_id"] != self._run_id or plan["status"] != "planned_not_published":
            raise ValueError("Planner returned an invalid publication claim")
        if plan["resource_ref"] not in self._binding["concept_uuids"]:
            raise ValueError("Plan resource is not bound to this synthetic visit")
        return plan

    def _identity(self, plan):
        return {
            "run_id": self._run_id,
            "scenario_event_id": plan["event_id"],
            "summary": plan["summary"],
            "resource_ref": plan["resource_ref"],
            "case_hash": plan["case_hash"],
            "rubric_hash": plan["rubric_hash"],
            "policy_hash": plan["policy_hash"],
            **{field: self._binding[field] for field in (
                "patient_uuid", "visit_uuid", "encounter_type_uuid", "location_uuid",
                "provider_uuid", "simulation_user_uuid", "encounter_role_uuid",
            )},
        }

    def _publish_and_verify(self, plan, marker, expected_version):
        patient = self._binding["patient_uuid"]
        encounters = self._client.all("encounter?patient=" + patient)
        matches = [
            encounter for encounter in encounters
            if any(obs.get("comment") == marker for obs in encounter.get("obs", []))
        ]
        if len(matches) > 1:
            raise ExternalPublicationError("OpenMRS read-back found duplicate publication markers")
        if matches:
            encounter_uuid = _uuid(matches[0].get("uuid"), "encounter UUID")
        else:
            response = self._client.request("POST", "encounter", {
                "patient": patient,
                "visit": self._binding["visit_uuid"],
                "encounterType": self._binding["encounter_type_uuid"],
                "location": self._binding["location_uuid"],
                "encounterDatetime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000%z"),
                "encounterProviders": [{
                    "provider": self._binding["provider_uuid"],
                    "encounterRole": self._binding["encounter_role_uuid"],
                }],
                "obs": [{
                    "concept": plan["resource_ref"],
                    "value": plan["summary"],
                    "comment": marker,
                }],
            })
            encounter_uuid = _uuid((response or {}).get("uuid"), "encounter UUID")
        readback = self._client.request("GET", f"encounter/{encounter_uuid}?v=full")
        self._verify_readback(readback, plan, marker, encounter_uuid)
        event_id = "publication:" + marker.removeprefix("DNH06:")[:24]
        event = self._record_publication(plan, event_id, expected_version)
        return {
            "run_id": self._run_id,
            "scenario_event_id": plan["event_id"],
            "summary": plan["summary"],
            "resource_ref": plan["resource_ref"],
            "case_hash": plan["case_hash"],
            "rubric_hash": plan["rubric_hash"],
            "policy_hash": plan["policy_hash"],
            "visit_uuid": self._binding["visit_uuid"],
            "encounter_uuid": encounter_uuid,
            "observation_uuid": next(
                obs["uuid"] for obs in readback["obs"] if obs.get("comment") == marker
            ),
            "clinical_update_event_id": event["event_id"],
            "marker": marker,
        }

    def _record_publication(self, plan, event_id, expected_version):
        return self._run.record(
            event_id=event_id,
            expected_version=expected_version,
            kind="clinical_update",
            producer="simulation_service",
            payload={
                "scenario_event_id": plan["event_id"],
                "summary": plan["summary"],
                "resource_ref": plan["resource_ref"],
                "delivery_stage": "published",
            },
            evidence_ids=[],
            visibility="participant",
        )

    def _recover_receipt(self, receipt, event_id, expected_version):
        required = {
            "run_id", "scenario_event_id", "summary", "resource_ref", "visit_uuid",
            "encounter_uuid", "observation_uuid", "clinical_update_event_id", "marker",
            "case_hash", "rubric_hash", "policy_hash",
        }
        if not isinstance(receipt, dict) or set(receipt) != required:
            raise ExternalPublicationError("Durable publication receipt is malformed")
        if (receipt["run_id"] != self._run_id or receipt["scenario_event_id"] != event_id
                or receipt["visit_uuid"] != self._binding["visit_uuid"]
                or receipt["resource_ref"] not in self._binding["concept_uuids"]):
            raise ExternalPublicationError("Durable publication receipt conflicts with run binding")
        matching = [
            event for event in self._run.events()
            if event["type"] == "clinical_update"
            and event["payload"]["scenario_event_id"] == event_id
            and event["payload"]["delivery_stage"] == "published"
        ]
        if len(matching) > 1:
            raise ExternalPublicationError("Run ledger contains duplicate publication evidence")
        if matching:
            payload = matching[0]["payload"]
            if (payload["summary"] != receipt["summary"]
                    or payload["resource_ref"] != receipt["resource_ref"]):
                raise ExternalPublicationError("Run evidence conflicts with durable receipt")
            plan = {
                "event_id": event_id,
                "summary": receipt["summary"],
                "resource_ref": receipt["resource_ref"],
                "case_hash": receipt["case_hash"],
                "rubric_hash": receipt["rubric_hash"],
                "policy_hash": receipt["policy_hash"],
            }
        else:
            plan = self._authoritative_plan(event_id, expected_version)
            expected_marker = "DNH06:" + sha256(_canonical(self._identity(plan)).encode()).hexdigest()
            if (receipt["marker"] != expected_marker
                    or any(receipt[field] != plan[field] for field in (
                        "summary", "resource_ref", "case_hash", "rubric_hash", "policy_hash"
                    ))):
                raise ExternalPublicationError("Durable receipt conflicts with current authoritative plan")
        readback = self._client.request("GET", f"encounter/{receipt['encounter_uuid']}?v=full")
        self._verify_readback(readback, plan, receipt["marker"], receipt["encounter_uuid"])
        if not matching:
            self._record_publication(plan, receipt["clinical_update_event_id"], expected_version)
        return deepcopy(receipt)

    def _verify_readback(self, encounter, plan, marker, encounter_uuid):
        def ref(field):
            value = encounter.get(field)
            return value.get("uuid") if isinstance(value, dict) else None

        valid = (
            isinstance(encounter, dict)
            and encounter.get("uuid") == encounter_uuid
            and ref("patient") == self._binding["patient_uuid"]
            and ref("visit") == self._binding["visit_uuid"]
            and ref("encounterType") == self._binding["encounter_type_uuid"]
            and ref("location") == self._binding["location_uuid"]
        )
        observations = encounter.get("obs", []) if isinstance(encounter, dict) else []
        matching = [obs for obs in observations if obs.get("comment") == marker]
        providers = encounter.get("encounterProviders", []) if isinstance(encounter, dict) else []
        provider_ok = len(providers) == 1 and (
            providers[0].get("provider") or {}
        ).get("uuid") == self._binding["provider_uuid"] and (
            providers[0].get("encounterRole") or {}
        ).get("uuid") == self._binding["encounter_role_uuid"]
        creator = (encounter.get("auditInfo") or {}).get("creator") or {} if isinstance(encounter, dict) else {}
        valid = valid and len(matching) == 1 and (
            matching[0].get("concept") or {}
        ).get("uuid") == plan["resource_ref"] and matching[0].get("value") == plan["summary"]
        valid = valid and provider_ok and creator.get("uuid") == self._binding["simulation_user_uuid"]
        if not valid:
            raise ExternalPublicationError("OpenMRS read-back did not match the bound synthetic visit")

    def _pause_if_current(self, expected_version):
        if self._run.state == "running" and self._run.execution_version == expected_version:
            self._run.request_pause(expected_version=expected_version)

    def external_failure(self, component, *, expected_version):
        if component not in self._FAILURE_COMPONENTS:
            raise ValueError("Unknown external component")
        if self._run.state != "running" or self._run.execution_version != expected_version:
            raise ValueError("Run is paused or execution version is stale")
        self._run.request_pause(expected_version=expected_version)
        return {"state": self._run.state, "execution_version": self._run.execution_version}

    def speech_interrupted(self, *, expected_version):
        if self._run.state != "running" or self._run.execution_version != expected_version:
            raise ValueError("Run is paused or execution version is stale")
        return {"state": self._run.state, "execution_version": self._run.execution_version}
