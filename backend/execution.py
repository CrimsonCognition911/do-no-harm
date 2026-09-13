"""Authoritative, synthetic-only OpenMRS publication for DNH-06."""

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import fcntl
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
    """Append-only evidence and durable write intents; process lock serializes dispatch."""

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
            connection.execute("CREATE TABLE IF NOT EXISTS attempts (run_id TEXT, event_id TEXT, fingerprint TEXT, PRIMARY KEY(run_id,event_id))")
            connection.execute("CREATE TABLE IF NOT EXISTS evidence (sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, kind TEXT, value TEXT)")
            connection.execute("CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, identity TEXT)")
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

    def attempted(self, run_id, event_id):
        connection = self._connect()
        try:
            return connection.execute("SELECT 1 FROM attempts WHERE run_id=? AND event_id=?", (run_id, event_id)).fetchone() is not None
        finally:
            connection.close()

    def append(self, run_id, kind, value):
        connection = self._connect()
        try:
            connection.execute("INSERT INTO evidence(run_id, kind, value) VALUES (?, ?, ?)",
                               (run_id, kind, _canonical(value)))
        finally:
            connection.close()

    def register(self, run_id, identity):
        """A process restart never silently restarts simulation time or scoring."""
        connection = self._connect()
        try:
            connection.execute("INSERT INTO runs(run_id, identity) VALUES (?, ?)",
                               (run_id, _canonical(identity)))
        except sqlite3.IntegrityError as error:
            raise ValueError("Run already exists; retain its evidence and start a fresh synthetic run") from error
        finally:
            connection.close()

    def execute_once(self, run_id, event_id, fingerprint, operation):
        # The intent must commit BEFORE the HTTP write. A crash or lost response
        # permits read-only reconciliation, never a second POST. flock also covers
        # other executor instances/processes using this ledger.
        with self.path.with_suffix(".lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            connection = self._connect()
            try:
                row = connection.execute(
                    "SELECT fingerprint, receipt FROM publication_receipts WHERE run_id=? AND scenario_event_id=?",
                    (run_id, event_id)).fetchone()
                if row:
                    if row[0] != fingerprint:
                        raise ValueError("Publication identity conflicts with durable receipt")
                    return json.loads(row[1])
                prior = connection.execute(
                    "SELECT fingerprint FROM attempts WHERE run_id=? AND event_id=?",
                    (run_id, event_id)).fetchone()
                if prior and prior[0] != fingerprint:
                    raise ValueError("Publication identity conflicts with durable intent")
                def reserve():
                    connection.execute("INSERT INTO attempts VALUES (?, ?, ?)", (run_id, event_id, fingerprint))
                receipt = operation(None if prior else reserve)
                connection.execute("INSERT INTO publication_receipts VALUES (?, ?, ?, ?)",
                                   (run_id, event_id, fingerprint, _canonical(receipt)))
                return deepcopy(receipt)
            finally:
                connection.close()


class OpenMRSExecutor:
    """Revalidates server plans, writes one encounter, and verifies its read-back."""

    _BINDING_FIELDS = {
        "fixture_only", "run_id", "patient_uuid", "visit_uuid",
        "encounter_type_uuid", "location_uuid", "provider_uuid",
        "encounter_role_uuid", "concept_uuids",
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
        self._attempt_plans = {}

    def publish_due(self, event_id, *, expected_version):
        with self._lock:
            return self._publish_due(event_id, expected_version=expected_version)

    def _publish_due(self, event_id, *, expected_version):
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("scenario event ID is required")
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("Invalid execution version")
        prior = self._ledger.get(self._run_id, event_id)
        if prior is not None:
            try:
                return self._recover_receipt(prior, event_id, expected_version)
            except Exception as error:
                self._pause_if_current(expected_version)
                if isinstance(error, ExternalPublicationError):
                    raise
                raise ExternalPublicationError("OpenMRS receipt recovery failed; simulation paused") from error
        with self._lock:
            if self._run.state != "running" or self._run.execution_version != expected_version:
                raise ValueError("Run is paused or execution version is stale")
            due = self._planner.due_events(expected_version=expected_version)
            matches = [plan for plan in due if plan["event_id"] == event_id]
            if len(matches) != 1:
                raise ValueError("Event is not currently due in the authoritative plan")
            plan = matches[0]
            if plan["run_id"] != self._run_id or plan["status"] != "planned_not_published":
                raise ValueError("Planner returned an invalid publication claim")
            if plan["resource_ref"] not in self._binding["concept_uuids"]:
                raise ValueError("Plan resource is not bound to this synthetic visit")
            identity = {
                "run_id": self._run_id,
                "scenario_event_id": event_id,
                "summary": plan["summary"],
                "resource_ref": plan["resource_ref"],
                "patient_uuid": self._binding["patient_uuid"],
                "visit_uuid": self._binding["visit_uuid"],
                "case_hash": plan["case_hash"], "rubric_hash": plan["rubric_hash"],
                "policy_hash": plan["policy_hash"],
            }
            fingerprint = sha256(_canonical(identity).encode()).hexdigest()
            marker = "DNH06:" + fingerprint
            self._attempt_plans[event_id] = (deepcopy(plan), marker, fingerprint)
            try:
                self._ledger.append(self._run_id, "publication_plan", plan)
                receipt = self._ledger.execute_once(
                    self._run_id,
                    event_id,
                    fingerprint,
                    lambda may_write: self._publish_and_verify(plan, marker, expected_version, may_write),
                )
                self._run.finish_external_write(event_id, verified=True)
                self._ledger.append(self._run_id, "publication", {"plan": plan, "receipt": receipt})
                if self._run.state == "running" and self._run.execution_version == expected_version:
                    self._record_publication(plan, receipt["clinical_update_event_id"], expected_version)
                return receipt
            except Exception as error:
                self._run.finish_external_write(event_id, verified=not self._ledger.attempted(self._run_id, event_id))
                self._ledger.append(self._run_id, "technical_failure", {"component": "openmrs", "event_id": event_id, "execution_version": expected_version})
                self._pause_if_current(expected_version)
                if isinstance(error, ExternalPublicationError):
                    raise
                raise ExternalPublicationError("OpenMRS publication failed; simulation paused") from error

    def _publish_and_verify(self, plan, marker, expected_version, may_write):
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
            if not may_write:
                raise ExternalPublicationError("Uncertain prior write: read-back not yet visible; no retry write permitted")
            self._verify_binding(plan)
            self._run.admit_external_write(plan["event_id"], expected_version=expected_version)
            # A pause after admission waits for this operation to drain. A pause
            # before admission prevents POST, including after a slow preflight.
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000%z")
            try:
                may_write()
                response = self._client.request("POST", "encounter", {
                "patient": patient,
                "visit": self._binding["visit_uuid"],
                "encounterType": self._binding["encounter_type_uuid"],
                "location": self._binding["location_uuid"],
                "encounterDatetime": timestamp,
                "encounterProviders": [{
                    "provider": self._binding["provider_uuid"],
                    "encounterRole": self._binding["encounter_role_uuid"],
                }],
                "obs": [{
                    "concept": plan["resource_ref"],
                    "obsDatetime": timestamp,
                    "value": plan["summary"],
                    "comment": marker,
                }],
                })
            except Exception:
                self._run.finish_external_write(plan["event_id"], verified=False)
                raise
            encounter_uuid = _uuid((response or {}).get("uuid"), "encounter UUID")
        readback = self._client.request("GET", f"encounter/{encounter_uuid}?v=full")
        self._verify_readback(readback, plan, marker, encounter_uuid)
        event_id = "publication:" + marker.removeprefix("DNH06:")[:24]
        return {
            "run_id": self._run_id,
            "scenario_event_id": plan["event_id"],
            "summary": plan["summary"],
            "resource_ref": plan["resource_ref"],
            "visit_uuid": self._binding["visit_uuid"],
            "encounter_uuid": encounter_uuid,
            "observation_uuid": next(
                obs["uuid"] for obs in readback["obs"] if obs.get("comment") == marker
            ),
            "clinical_update_event_id": event_id,
            "marker": marker,
        }

    def _verify_binding(self, plan):
        patient = self._client.request("GET", "patient/" + self._binding["patient_uuid"] + "?v=full")
        visit = self._client.request("GET", "visit/" + self._binding["visit_uuid"] + "?v=full")
        provider = self._client.request("GET", "provider/" + self._binding["provider_uuid"] + "?v=full")
        concept = self._client.request("GET", "concept/" + plan["resource_ref"] + "?v=full")
        if (patient.get("voided") or patient.get("uuid") != self._binding["patient_uuid"]
                or patient.get("person", {}).get("display") != "DNH SYNTHETIC"
                or not any(i.get("identifier", "").startswith("DNH-") for i in patient.get("identifiers", []))
                or visit.get("voided") or visit.get("stopDatetime")
                or visit.get("uuid") != self._binding["visit_uuid"]
                or visit.get("patient", {}).get("uuid") != self._binding["patient_uuid"]
                or provider.get("retired") or provider.get("uuid") != self._binding["provider_uuid"]
                or provider.get("identifier") != "dnh-simulation"
                or concept.get("uuid") != plan["resource_ref"]
                or concept.get("retired") or concept.get("datatype", {}).get("display") != "Text"):
            raise ExternalPublicationError("Synthetic patient, active visit, provider or text concept binding failed")

    def reconcile(self, event_id):
        """Read-only recovery while paused; never replays a write or advances state."""
        _uuid(event_id, "event_id")
        with self._lock:
            receipt = self._ledger.get(self._run_id, event_id)
            if receipt:
                plan = {"event_id": event_id, "summary": receipt["summary"], "resource_ref": receipt["resource_ref"]}
                read = self._client.request("GET", f"encounter/{receipt['encounter_uuid']}?v=full")
                self._verify_readback(read, plan, receipt["marker"], receipt["encounter_uuid"])
            else:
                # Recover the frozen plan saved before dispatch; failure leaves
                # execution unacknowledged. No score is attached to this fault.
                plan = self._attempt_plans.get(event_id)
                if plan is None:
                    raise ValueError("Unknown publication attempt")
                plan, marker, fingerprint = plan
                receipt = self._ledger.execute_once(self._run_id, event_id, fingerprint,
                    lambda _: self._publish_and_verify(plan, marker, self._run.execution_version, False))
            self._run.finish_external_write(event_id, verified=True)
            self._ledger.append(self._run_id, "reconciled", receipt)
            return receipt

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
        }
        if not isinstance(receipt, dict) or set(receipt) != required:
            raise ExternalPublicationError("Durable publication receipt is malformed")
        if (receipt["run_id"] != self._run_id or receipt["scenario_event_id"] != event_id
                or receipt["visit_uuid"] != self._binding["visit_uuid"]
                or receipt["resource_ref"] not in self._binding["concept_uuids"]):
            raise ExternalPublicationError("Durable publication receipt conflicts with run binding")
        event = self._planner._events.get(event_id)
        if (event is None or receipt["summary"] != event["summary"]
                or receipt["resource_ref"] != self._planner._bindings[event["resource_key"]]):
            raise ExternalPublicationError("Receipt does not match the frozen case")
        identity = {"run_id": self._run_id, "scenario_event_id": event_id,
                    "summary": event["summary"], "resource_ref": receipt["resource_ref"],
                    "patient_uuid": self._binding["patient_uuid"], "visit_uuid": self._binding["visit_uuid"],
                    "case_hash": self._planner._case.case_hash, "rubric_hash": self._planner._case.rubric_hash,
                    "policy_hash": self._planner._case.policy_hash}
        if receipt["marker"] != "DNH06:" + sha256(_canonical(identity).encode()).hexdigest():
            raise ExternalPublicationError("Receipt case or policy identity changed")
        plan = {"event_id": event_id, "summary": receipt["summary"], "resource_ref": receipt["resource_ref"]}
        readback = self._client.request(
            "GET", f"encounter/{receipt['encounter_uuid']}?v=full"
        )
        self._verify_readback(readback, plan, receipt["marker"], receipt["encounter_uuid"])
        self._run.finish_external_write(event_id, verified=True)
        matching = [
            event for event in self._run.events()
            if event["type"] == "clinical_update"
            and event["payload"]["scenario_event_id"] == event_id
            and event["payload"]["delivery_stage"] == "published"
        ]
        if len(matching) > 1:
            raise ExternalPublicationError("Run ledger contains duplicate publication evidence")
        if not matching:
            if self._run.state != "running" or self._run.execution_version != expected_version:
                raise ValueError("Run is paused or execution version is stale")
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
        if not isinstance(encounter, dict):
            raise ExternalPublicationError("Invalid OpenMRS read-back")
        valid = valid and not encounter.get("voided") and any(
            item.get("provider", {}).get("uuid") == self._binding["provider_uuid"]
            and item.get("encounterRole", {}).get("uuid") == self._binding["encounter_role_uuid"]
            for item in encounter.get("encounterProviders", []))
        observations = encounter.get("obs", []) if isinstance(encounter, dict) else []
        matching = [obs for obs in observations if obs.get("comment") == marker]
        valid = valid and len(matching) == 1 and not matching[0].get("voided") and (
            matching[0].get("concept") or {}
        ).get("uuid") == plan["resource_ref"] and matching[0].get("value") == plan["summary"]
        if not valid:
            raise ExternalPublicationError("OpenMRS read-back did not match the bound synthetic visit")

    def _pause_if_current(self, expected_version):
        if self._run.state == "running" and self._run.execution_version == expected_version:
            self._run.request_pause(expected_version=expected_version)

    def external_failure(self, component, *, expected_version):
        if not isinstance(component, str) or component not in self._FAILURE_COMPONENTS:
            raise ValueError("Unknown external component")
        if self._run.state != "running" or self._run.execution_version != expected_version:
            raise ValueError("Run is paused or execution version is stale")
        self._ledger.append(self._run_id, "technical_failure", {"component": component, "execution_version": expected_version})
        self._run.request_pause(expected_version=expected_version)
        return {"state": self._run.state, "execution_version": self._run.execution_version}

    def speech_interrupted(self, *, expected_version):
        if self._run.state != "running" or self._run.execution_version != expected_version:
            raise ValueError("Run is paused or execution version is stale")
        return {"state": self._run.state, "execution_version": self._run.execution_version}
