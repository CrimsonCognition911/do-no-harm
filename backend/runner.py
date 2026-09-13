"""Server-owned allowlist connecting synthetic OpenMRS runs to the session API."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

from backend.adaptation import AdaptivePlanner, FrozenCase
from backend.case_compiler import load_reviewed_case
from backend.execution import OpenMRSExecutor, PublicationLedger
from backend.runtime import Run


def openmrs_module():
    path = Path(__file__).resolve().parents[1] / "openmrs-config" / "configure.py"
    spec = importlib.util.spec_from_file_location("dnh_openmrs_configuration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunnerFactory:
    """Configuration is local operator input, never supplied by the doctor/model.

    Each entry uses a distinct DNH-02 manifest. Reusing an existing patient/visit
    after service restart fails closed; retained SQLite evidence is not reset.
    """
    def __init__(self, entries, ledger_path):
        self.entries = deepcopy(entries)
        self.ledger_path = Path(ledger_path)
        self.ledger = PublicationLedger(ledger_path)

    @classmethod
    def from_file(cls, path):
        path = Path(path).resolve()
        config = json.loads(path.read_text())
        if set(config) != {"ledger_path", "runs"}:
            raise ValueError("Runner configuration requires ledger_path and runs")
        def resolve(value):
            return path.parent / value
        entries = {}
        module = openmrs_module()
        for item in config["runs"]:
            if set(item) != {"manifest", "credentials", "case", "review", "fixture", "bindings"}:
                raise ValueError("Unknown runner configuration fields")
            manifest = json.loads(resolve(item["manifest"]).read_text())
            credentials = json.loads(resolve(item["credentials"]).read_text())
            if type(item["fixture"]) is not bool:
                raise ValueError("fixture must be boolean")
            if item["fixture"]:
                case = FrozenCase(json.loads(resolve(item["case"]).read_text()), fixture=True)
            else:
                # Content approval alone does not authorize clinical runtime.
                review = json.loads(resolve(item["review"]).read_text())
                if review.get("runtime_release_approved") is not True:
                    raise ValueError("Clinical runtime release approval is missing")
                case = load_reviewed_case(resolve(item["case"]), resolve(item["review"]))
            key = manifest["run_key"]
            if key in entries:
                raise ValueError("Duplicate run allowlist key")
            entries[key] = {"manifest": manifest, "case": case, "bindings": item["bindings"],
                            "client": module.Client(manifest["base_url"], "dnh-simulation", credentials["simulation"])}
        # Clients contain opener locks and cannot be deep-copied.
        factory = cls({}, resolve(config["ledger_path"]))
        factory.entries = entries
        return factory

    def create(self, key, run_id, mode, clock):
        if key not in self.entries:
            raise ValueError("Synthetic run is not allowlisted")
        entry = self.entries[key]
        manifest, case = entry["manifest"], entry["case"]
        if manifest.get("fixture_only") is not True or manifest["seed"].get("fixture_only") is not True:
            raise ValueError("Only synthetic manifests are supported")
        bindings = {name: manifest["concepts"][concept] for name, concept in entry["bindings"].items()}
        binding = {
            "fixture_only": True, "run_id": run_id,
            "patient_uuid": manifest["seed"]["patient_uuid"], "visit_uuid": manifest["seed"]["visit_uuid"],
            "encounter_type_uuid": manifest["encounter_types"]["Reassessment"],
            "location_uuid": manifest["locations"]["Observation"],
            "provider_uuid": manifest["identities"]["simulation"]["provider_uuid"],
            "simulation_user_uuid": manifest["identities"]["simulation"]["user_uuid"],
            "encounter_role_uuid": manifest["encounter_role"], "concept_uuids": sorted(set(bindings.values())),
        }
        # Visit identity rather than caller request identity prevents accidental
        # reuse across process restarts and different allowlist labels.
        self.ledger.register(manifest["base_url"] + "/" + binding["visit_uuid"],
                             {"run_id": run_id, "binding": binding, "case_hash": case.case_hash,
                              "rubric_hash": case.rubric_hash, "policy_hash": case.policy_hash})
        run = Run(run_id, resource_refs=set(bindings.values()), mode=mode, clock=clock,
                  evidence_sink=lambda event: self.ledger.append(run_id, "event", event))
        run.environment = "synthetic_openmrs_fixture" if case.fixture else "synthetic_openmrs"
        planner = AdaptivePlanner(case, run, bindings=bindings)
        executor = OpenMRSExecutor(run, planner, entry["client"], binding=binding, ledger_path=self.ledger_path)
        return {"run": run, "planner": planner, "executor": executor, "commands": {}}
