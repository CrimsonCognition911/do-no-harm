"""Offline safety and repeatability tests for the ED configuration client."""

import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest


class EDConfigurationTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "openmrs-config" / "configure.py"
        self.assertTrue(path.exists(), "ED configuration implementation is missing")
        spec = importlib.util.spec_from_file_location("ed_config", path)
        self.ed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.ed)

    def test_remote_plaintext_credentials_are_rejected(self):
        with self.assertRaises(ValueError):
            self.ed.Client("http://example.org/openmrs", "user", "secret")

    def test_metadata_rerun_reuses_server_uuid(self):
        class Fake:
            def all(self, resource):
                return [{"uuid": "server-issued", "name": "Emergency Visit", "description": self.description}]
            def request(self, *args):
                raise AssertionError("Rerun must not write existing metadata")
        client = Fake()
        client.description = self.ed.OWNER
        result = self.ed.ensure(client, "visittype", "Emergency Visit", {})
        self.assertEqual(result["uuid"], "server-issued")

    def test_name_collision_is_not_adopted(self):
        class Fake:
            def all(self, resource):
                return [{"uuid": "shared", "name": "Emergency Visit", "description": "Someone else's metadata"}]
        with self.assertRaises(ValueError):
            self.ed.ensure(Fake(), "visittype", "Emergency Visit", {})

    def test_existing_location_with_wrong_parent_is_rejected(self):
        class Fake:
            def all(self, resource):
                return [{"uuid": "owned", "name": "Triage", "description": self.description,
                         "parentLocation": {"uuid": "wrong-parent"}}]
        client = Fake()
        client.description = self.ed.OWNER
        with self.assertRaises(ValueError):
            self.ed.ensure(client, "location", "Triage", {"parentLocation": "correct-parent"})

    def test_review_privileges_exclude_writes(self):
        privileges = self.ed.role_privileges("review")
        self.assertIn("Get Patients", privileges)
        self.assertTrue(all(p.startswith(("Get ", "View ")) for p in privileges))
        self.assertNotIn("Add Observations", privileges)
        self.assertIn("Add Observations", self.ed.role_privileges("simulation"))

    def test_acuity_is_free_text_without_automated_thresholds(self):
        form = self.ed.make_form("Triage", "encounter-uuid", {"acuity": "concept-uuid"})
        question = form["pages"][0]["sections"][0]["questions"][0]
        self.assertEqual(question["questionOptions"], {"rendering": "textarea", "concept": "concept-uuid"})
        self.assertNotIn("calculate", str(form))

    def test_client_rejects_cross_origin_and_parent_paths(self):
        client = self.ed.Client("http://127.0.0.1:8090/openmrs", "user", "secret")
        for path in ("https://example.org", "../session", "/session"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                client.request("GET", path)

    def test_documented_live_test_command_imports_application_modules(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "openmrs-config/test_live.py", "-h"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_live_gate_forces_a_fresh_publication_attempt(self):
        source = (Path(__file__).resolve().parents[1] / "openmrs-config" / "test_live.py").read_text()
        self.assertIn("uuid4()", source)
        self.assertIn("TemporaryDirectory", source)
        self.assertIn("simulation_user_uuid", source)


if __name__ == "__main__":
    unittest.main()
