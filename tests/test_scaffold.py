"""Exercise the scaffold through real local HTTP, without provider calls."""

import http.client
import importlib.util
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class ScaffoldTests(unittest.TestCase):
    def setUp(self):
        app_path = ROOT / "backend" / "app.py"
        self.assertTrue(app_path.is_file(), "Runnable backend scaffold is missing")
        spec = importlib.util.spec_from_file_location("dnh_app", app_path)
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        self.server = app.create_server(port=0)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path):
        conn = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=2
        )
        try:
            conn.request(method, path)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            conn.close()

    def test_health_reports_liveness_without_claiming_integrations(self):
        status, headers, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body["status"], "scaffold")
        self.assertEqual(body["integrations"], {
            "agents_api": "not_connected",
            "gpt_live": "not_connected",
            "openmrs": "not_connected",
        })

    def test_readiness_fails_until_real_integrations_exist(self):
        status, _, body = self.request("GET", "/ready")
        self.assertEqual(status, 503)
        self.assertFalse(body["ready"])

    def test_frontend_can_fetch_the_versioned_shared_contract(self):
        status, _, schema = self.request("GET", "/api/contracts/events")
        self.assertEqual(status, 200)
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(set(schema["properties"]["type"]["enum"]), {
            "doctor_action", "clinical_update", "evaluation_finding", "session_state"
        })
        self.assertIn("run_id", schema["required"])
        self.assertIn("execution_version", schema["required"])
        self.assertFalse(schema["additionalProperties"])
        self.assertIn("Frozen application contract 0.1", schema["description"])

    def test_frontend_can_fetch_handshake_samples_and_voice_schema(self):
        status, _, handshake = self.request("GET", "/api/contracts/handshake")
        self.assertEqual(status, 200)
        self.assertEqual(handshake["status"], "frozen")
        public = json.dumps({
            "participant_feed": handshake["participant_feed"],
            "permitted_voice_updates": handshake["permitted_voice_updates"],
        })
        self.assertNotIn("criterion_id", public)
        self.assertNotIn("Hidden rubric", public)
        status, _, voice = self.request("GET", "/api/contracts/voice-update")
        self.assertEqual(status, 200)
        self.assertEqual(voice["properties"]["kind"]["const"], "permitted_voice_update")
        self.assertEqual(self.request("GET", "/api/contracts/adaptive-fixture.json")[0], 404)

    def test_unknown_paths_cannot_read_workspace_files(self):
        status, _, body = self.request("GET", "/../../.env.local")
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "not_found"})

    def test_mutations_are_not_accepted_by_the_scaffold(self):
        status, headers, body = self.request("POST", "/api/runs")
        self.assertEqual(status, 405)
        self.assertEqual(headers["Allow"], "GET")
        self.assertEqual(body, {"error": "method_not_allowed"})

    def test_configuration_secrets_are_never_returned_in_health(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-value-not-a-key"}):
            status, _, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertNotIn("synthetic-test-value-not-a-key", json.dumps(body))
        self.assertEqual(body["integrations"]["agents_api"], "not_connected")


if __name__ == "__main__":
    unittest.main()
