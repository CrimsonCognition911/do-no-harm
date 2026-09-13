"""Explicit live acceptance gate: python3 openmrs-config/test_live.py -v."""

import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from urllib.error import HTTPError
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.adaptation import AdaptivePlanner, FrozenCase
from backend.execution import OpenMRSExecutor
from backend.runtime import Run
from configure import Client, HERE, FORM_FIELDS, timestamp

RUNTIME = HERE.parent / "runs" / "openmrs"


class LiveEDTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(Path(os.environ.get("DNH_MANIFEST", RUNTIME / "manifest.json")).read_text())
        credentials = json.loads((RUNTIME / "credentials.json").read_text())
        cls.clients = {identity: Client(cls.manifest["base_url"], "dnh-" + identity, password)
                       for identity, password in credentials.items()}

    def test_fixture_has_correct_active_visit(self):
        m = self.manifest
        visit = self.clients["doctor"].request("GET", f"visit/{m['seed']['visit_uuid']}?v=full")
        self.assertEqual(visit["patient"]["uuid"], m["seed"]["patient_uuid"])
        self.assertEqual(visit["visitType"]["uuid"], m["visit_type"])
        self.assertEqual(visit["location"]["uuid"], m["locations"]["Emergency Department"])
        self.assertIsNone(visit["stopDatetime"])

    def encounter(self, identity, form_name="Triage"):
        m = self.manifest
        key = FORM_FIELDS[form_name][0]
        return {"patient": m["seed"]["patient_uuid"], "visit": m["seed"]["visit_uuid"],
                "encounterType": m["encounter_types"][form_name], "form": m["forms"][form_name],
                "location": m["locations"]["Triage"],
                "encounterDatetime": timestamp(),
                "encounterProviders": [{"provider": m["identities"][identity]["provider_uuid"],
                                         "encounterRole": m["encounter_role"]}],
                "obs": [{"concept": m["concepts"][key], "value": "SYNTHETIC API permission test"}]}

    def test_doctor_saves_all_four_forms_to_active_visit(self):
        for name in FORM_FIELDS:
            with self.subTest(form=name):
                client = self.clients["doctor"]
                saved = client.request("POST", "encounter", self.encounter("doctor", name))
                read = client.request("GET", f"encounter/{saved['uuid']}?v=full")
                self.assertEqual(read["visit"]["uuid"], self.manifest["seed"]["visit_uuid"])
                self.assertEqual(read["form"]["uuid"], self.manifest["forms"][name])
                self.assertEqual(read["auditInfo"]["creator"]["uuid"], self.manifest["identities"]["doctor"]["user_uuid"])
                self.assertEqual(len(read["obs"]), 1)

    def test_simulation_has_distinct_authorship(self):
        client = self.clients["simulation"]
        saved = client.request("POST", "encounter", self.encounter("simulation", "Reassessment"))
        read = client.request("GET", f"encounter/{saved['uuid']}?v=full")
        self.assertEqual(read["auditInfo"]["creator"]["uuid"], self.manifest["identities"]["simulation"]["user_uuid"])

    def test_authoritative_runtime_publishes_once_and_reads_bound_visit(self):
        fixture = json.loads((HERE.parent / "contracts" / "adaptive-fixture.json").read_text())
        case = FrozenCase(fixture, fixture=True)
        now = [0.0]
        concept = self.manifest["concepts"]["reassessment"]
        run_id = "dnh06-live-" + str(uuid4())
        run = Run(run_id, resource_refs={concept}, clock=lambda: now[0])
        run.start(expected_version=1)
        now[0] = 6.0
        planner = AdaptivePlanner(
            case,
            run,
            bindings={"state": concept, "result": concept, "challenge": concept},
        )
        binding = {
            "fixture_only": self.manifest["fixture_only"],
            "run_id": run_id,
            "patient_uuid": self.manifest["seed"]["patient_uuid"],
            "visit_uuid": self.manifest["seed"]["visit_uuid"],
            "encounter_type_uuid": self.manifest["encounter_types"]["Reassessment"],
            "location_uuid": self.manifest["locations"]["Observation"],
            "provider_uuid": self.manifest["identities"]["simulation"]["provider_uuid"],
            "simulation_user_uuid": self.manifest["identities"]["simulation"]["user_uuid"],
            "encounter_role_uuid": self.manifest["encounter_role"],
            "concept_uuids": sorted(self.manifest["concepts"].values()),
        }
        with TemporaryDirectory() as directory:
            executor = OpenMRSExecutor(
                run,
                planner,
                self.clients["simulation"],
                binding=binding,
                ledger_path=Path(directory) / "dnh06-publications.sqlite3",
            )

            first = executor.publish_due("unresolved-consequence", expected_version=1)
            retry = executor.publish_due("unresolved-consequence", expected_version=1)
            read = self.clients["simulation"].request(
                "GET", f"encounter/{first['encounter_uuid']}?v=full"
            )

        self.assertEqual(retry, first)
        self.assertEqual(read["patient"]["uuid"], binding["patient_uuid"])
        self.assertEqual(read["visit"]["uuid"], binding["visit_uuid"])
        self.assertEqual(read["auditInfo"]["creator"]["uuid"], binding["simulation_user_uuid"])
        self.assertTrue(any(
            item["provider"]["uuid"] == binding["provider_uuid"]
            and item["encounterRole"]["uuid"] == binding["encounter_role_uuid"]
            for item in read["encounterProviders"]
        ))
        matching = [obs for obs in read["obs"] if obs.get("comment") == first["marker"]]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["concept"]["uuid"], first["resource_ref"])
        self.assertEqual(matching[0]["value"], first["summary"])

    def test_review_reads_but_cannot_create_update_or_delete(self):
        m = self.manifest
        client = self.clients["review"]
        self.assertEqual(client.request("GET", f"patient/{m['seed']['patient_uuid']}")["uuid"], m["seed"]["patient_uuid"])
        # These are valid writes to our disposable fixture: authorization, not validation, must reject them.
        operations = [
            ("POST", "encounter", self.encounter("doctor")),
            ("POST", "obs", {"person": m["seed"]["patient_uuid"], "concept": m["concepts"]["acuity"],
                              "obsDatetime": timestamp(), "value": "DENIAL PROBE"}),
            ("POST", f"visit/{m['seed']['visit_uuid']}", {"stopDatetime": timestamp()}),
            ("DELETE", f"patient/{m['seed']['patient_uuid']}?reason=DNH-permission-probe", None),
        ]
        for method, path, body in operations:
            with self.subTest(method=method, resource=path.split('/')[0]):
                try:
                    client.request(method, path, body)
                except HTTPError as error:
                    error.close()
                    self.assertIn(error.code, (401, 403))
                else:
                    self.fail("Review identity unexpectedly wrote clinical data")

    def test_forms_have_readable_published_schemas(self):
        for name, uuid in self.manifest["forms"].items():
            form = self.clients["doctor"].request("GET", f"form/{uuid}?v=full")
            self.assertTrue(form["published"])
            resource = next(r for r in form["resources"] if r["name"] == "JSON schema")
            schema = self.clients["doctor"].request("GET", "clobdata/" + resource["valueReference"])
            self.assertEqual(schema["encounter"], self.manifest["encounter_types"][name])


if __name__ == "__main__":
    unittest.main()
