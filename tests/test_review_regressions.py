"""Review regressions using real BFF -> session HTTP and isolated fixture files."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from frontend.server import SessionGateway, create_server
from tests import test_session_api as session_tests


class TechnicalPauseTests(unittest.TestCase):
    setUp = session_tests.SessionAPITests.setUp
    request = session_tests.SessionAPITests.request
    command = session_tests.SessionAPITests.command
    action = session_tests.SessionAPITests.action

    def test_bff_fault_stops_real_clock_and_requires_both_workers_to_resume(self):
        from tests.test_doctor_interface_green import DoctorInterfaceGreenTests
        self.command("start")
        self.now = 3
        gateway = SessionGateway(f"http://127.0.0.1:{self.server.server_port}", self.session["run_id"],
                                 self.tokens["doctor"], self.tokens["audio"])
        bff = create_server(0, gateway=gateway)
        self.addCleanup(bff.server_close)
        thread = threading.Thread(target=bff.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(bff.shutdown)
        request = lambda *args, **kwargs: DoctorInterfaceGreenTests.request(self, bff, *args, **kwargs)
        csrf = request("GET", "/api/bootstrap")[1]["csrf"]
        body = {"request_id": "fault-1"}
        status, paused = request("POST", "/api/technical-pause", body, csrf=csrf)
        self.assertEqual((status, paused["state"], paused["execution_version"]), (200, "pause_requested", 2))
        self.now = 30
        self.assertEqual(self.action()[0], 409)
        snapshot = self.request("GET", self.path, self.tokens["doctor"])[2]
        self.assertEqual(snapshot["simulation_time_ms"], 3000)
        self.assertFalse(snapshot["assisted"])
        self.assertEqual(request("POST", "/api/technical-pause", body, csrf=csrf)[1], paused)
        for role in ("audio", "execution"):
            self.request("POST", self.path + "/acks", self.tokens[role],
                         {"request_id": "pause-ack", "transition": "pause", "execution_version": 2})
        self.assertEqual(self.command("resume", 2)[2]["state"], "resume_requested")
        for role, expected in (("audio", "resume_requested"), ("execution", "running")):
            result = self.request("POST", self.path + "/acks", self.tokens[role],
                                  {"request_id": "resume-ack", "transition": "resume", "execution_version": 3})
            self.assertEqual(result[2]["state"], expected)
        # A delayed retry cannot pause the resumed attempt again.
        request("POST", "/api/technical-pause", body, csrf=csrf)
        self.assertEqual(self.request("GET", self.path, self.tokens["doctor"])[2]["state"], "running")

    def test_fault_cancels_inflight_resume_without_granting_audio_examiner_control(self):
        self.command("start")
        self.command("pause")
        for role in ("audio", "execution"):
            self.request("POST", self.path + "/acks", self.tokens[role],
                         {"request_id": "ack", "transition": "pause", "execution_version": 2})
        self.command("resume", 2)
        result = self.request("POST", self.path + "/technical-pause", self.tokens["audio"], {"request_id": "fault"})
        self.assertEqual((result[2]["state"], result[2]["execution_version"]), ("pause_requested", 4))
        self.assertEqual(self.command("resume", 4, role="audio")[0], 403)
        self.assertEqual(self.request("POST", self.path + "/technical-pause", self.tokens["doctor"], {"request_id": "fault"})[0], 403)
        self.assertEqual(self.request("POST", self.path + "/technical-pause", self.tokens["audio"], {"request_id": "extra", "command": "resume"})[0], 422)


class FixtureTargetTests(unittest.TestCase):
    def load(self, name):
        directory = Path(__file__).resolve().parents[1] / "openmrs-config"
        spec = importlib.util.spec_from_file_location(name, directory / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"configure": self.config} if hasattr(self, "config") else {}):
            spec.loader.exec_module(module)
        return module

    def setUp(self):
        self.config = self.load("configure")

    def test_custom_manifest_selects_new_patient_and_visit_for_publication(self):
        publisher = self.load("publish-fixture")
        manifest = {"base_url": "http://127.0.0.1:8090/openmrs", "seed": {"patient_uuid": "new-patient", "visit_uuid": "new-visit"},
                    "encounter_types": {"Reassessment": "type"}, "locations": {"Observation": "location"},
                    "identities": {"simulation": {"provider_uuid": "provider", "user_uuid": "simulation-user"}}, "encounter_role": "role",
                    "numeric_concepts": {key: {"uuid": key} for key in ("temperature", "haemoglobin")}}
        writes = []
        class Client:
            def __init__(self, *args): pass
            def all(self, resource):
                assert resource == "encounter?patient=new-patient"
                return []
            def request(self, method, resource, body=None):
                if method == "POST":
                    writes.append(body)
                    return {"uuid": "encounter"}
                return {"visit": {"uuid": "new-visit"}, "obs": [{}, {}]}
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "another-run.json"
            run.write_text(json.dumps(manifest))
            credentials = Path(temp) / "credentials.json"
            credentials.write_text(json.dumps({"simulation": "synthetic-password"}))
            with patch.object(publisher, "Client", Client), contextlib.redirect_stdout(io.StringIO()):
                publisher.main(["--manifest", str(run), "--credentials", str(credentials)])
        self.assertEqual((writes[0]["patient"], writes[0]["visit"]), ("new-patient", "new-visit"))

    def test_existing_provider_must_match_account_person_and_be_active(self):
        for person, retired in (("someone-else", False), ("doctor-person", True)):
            with self.subTest(person=person, retired=retired):
                config = self.config
                class Client:
                    def all(self, resource):
                        if resource == "privilege":
                            return [{"name": n, "uuid": n} for n in config.role_privileges("doctor")]
                        if resource.startswith("user?"):
                            return [{"uuid": "doctor", "username": "dnh-doctor", "person": {"uuid": "doctor-person"}, "roles": [{"uuid": "role"}]}]
                        if resource.startswith("provider?"):
                            return [{"uuid": "provider", "identifier": "dnh-doctor", "person": {"uuid": person}, "retired": retired}]
                        raise AssertionError(resource)
                role = {"uuid": "role", "inheritedRoles": [], "privileges": [{"name": n} for n in config.role_privileges("doctor")]}
                with patch.object(config, "ensure", return_value=role), self.assertRaisesRegex(ValueError, "Provider identity drift"):
                    config.configure_identities(Client())
