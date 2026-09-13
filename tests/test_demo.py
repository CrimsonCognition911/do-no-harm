import importlib
import unittest


class DemoTests(unittest.TestCase):
    def test_offline_walkthrough_exercises_real_controller(self):
        try:
            demo = importlib.import_module("backend.demo")
        except ModuleNotFoundError:
            self.fail("Offline controller walkthrough is not implemented")
        result = demo.run_demo()
        self.assertEqual(result["mode"], "offline_fixture")
        self.assertEqual(result["final_state"], "running")
        self.assertTrue(result["assisted"])
        self.assertEqual(result["simulation_time_ms"], 5000)
        self.assertEqual(result["blocked"], ["forged_confirmation", "premature_review", "stale_action"])
        self.assertEqual(result["hidden_findings"], 1)
        self.assertTrue(all(event["visibility"] == "participant" for event in result["participant_events"]))
        self.assertNotIn("evaluation_finding", {e["type"] for e in result["participant_events"]})
