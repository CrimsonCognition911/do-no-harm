"""Publish one synthetic numeric encounter once; no orders or clinical rules are implied."""
import json
from configure import Client, HERE, timestamp

runtime = HERE.parent / "runs" / "openmrs"
manifest = json.loads((runtime / "manifest.json").read_text())
credentials = json.loads((runtime / "credentials.json").read_text())
client = Client(manifest["base_url"], "dnh-simulation", credentials["simulation"])
marker = "DNH synthetic numeric display fixture v1"
patient = manifest["seed"]["patient_uuid"]
visit = manifest["seed"]["visit_uuid"]
existing = [e for e in client.all("encounter?patient=" + patient) if (e.get("visit") or {}).get("uuid") == visit
            and any(o.get("comment") == marker for o in e.get("obs", []))]
if len(existing) > 1:
    raise SystemExit("Duplicate display fixture encounters; inspect before continuing")
if existing:
    encounter = existing[0]
else:
    encounter = client.request("POST", "encounter", {
        "patient": patient, "visit": visit, "encounterType": manifest["encounter_types"]["Reassessment"],
        "location": manifest["locations"]["Observation"], "encounterDatetime": timestamp(),
        "encounterProviders": [{"provider": manifest["identities"]["simulation"]["provider_uuid"],
                                 "encounterRole": manifest["encounter_role"]}],
        "obs": [{"concept": manifest["numeric_concepts"][key]["uuid"], "value": value, "comment": marker}
                for key, value in (("temperature", 37), ("haemoglobin", 12))]})
read = client.request("GET", "encounter/" + encounter["uuid"] + "?v=full")
assert read["visit"]["uuid"] == visit
assert len(read["obs"]) == 2
print("Verified one durable synthetic numeric encounter: " + encounter["uuid"])
