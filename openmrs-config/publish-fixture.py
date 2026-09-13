"""Publish numeric display fixtures into the explicitly selected synthetic run."""
import argparse
import json
from pathlib import Path
from configure import Client, HERE, timestamp


def publish(manifest, credentials):
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
    return encounter["uuid"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=HERE.parent / "runs/openmrs/manifest.json")
    parser.add_argument("--credentials", type=Path, default=HERE.parent / "runs/openmrs/credentials.json")
    args = parser.parse_args(argv)
    manifest = json.loads(args.manifest.read_text())
    credentials = json.loads(args.credentials.read_text())
    encounter_uuid = publish(manifest, credentials)
    print("Verified one durable synthetic numeric encounter: " + encounter_uuid)


if __name__ == "__main__":
    main()
