"""Explicit live acceptance gate: python3 openmrs-config/test_live.py -v."""

import json
import unittest
from urllib.error import HTTPError

from configure import Client, HERE, FORM_FIELDS, timestamp

RUNTIME = HERE.parent / "runs" / "openmrs"


class LiveEDTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((RUNTIME / "manifest.json").read_text())
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
