"""Capture or check the installed local O3 distribution, without exporting system secrets."""
import argparse
import json
import os
import subprocess
from urllib.request import urlopen

from configure import Client, HERE


def inventory(client):
    info = client.request("GET", "systeminformation")["systemInfo"]
    with urlopen(client.base + "/spa/importmap.json", timeout=30) as response:
        imports = json.load(response)["imports"]
    images = {}
    for service in ("gateway", "frontend", "backend", "db"):
        container = "do-no-harm-openmrs-" + service + "-1"
        image_id = subprocess.check_output(["docker", "inspect", "--format", "{{.Image}}", container], text=True).strip()
        image = json.loads(subprocess.check_output(["docker", "image", "inspect", image_id], text=True))[0]
        images[service] = {"id": image_id, "digests": image["RepoDigests"]}
    dictionary = subprocess.check_output(["docker", "exec", "do-no-harm-openmrs-backend-1", "sh", "-c",
        "find /openmrs/data/configuration/ocl -type f -name '*.zip' -exec sha256sum {} +"], text=True)
    return {"schema_version": 1, "distribution": "3.7.1", "backend": info["SystemInfo.title.openmrsInformation"]["SystemInfo.OpenMRSInstallation.openmrsVersion"],
            "modules": {k: v.strip() for k, v in info["SystemInfo.title.moduleInformation"].items() if k != "SystemInfo.Module.repositoryPath"},
            "frontend_imports": imports, "images": images,
            "dictionary_archives": dict(sorted(line.split(None, 1)[::-1] for line in dictionary.splitlines()))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", action="store_true", help="Record a reviewed installation as the lock")
    args = parser.parse_args()
    client = Client("http://127.0.0.1:8090/openmrs", os.environ["DNH_ADMIN_USERNAME"], os.environ["DNH_ADMIN_PASSWORD"])
    current = inventory(client)
    lock = HERE / "versions.lock.json"
    if args.record:
        lock.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        print("Recorded installed versions and image digests")
    elif json.loads(lock.read_text()) != current:
        raise SystemExit("Installed versions differ from versions.lock.json")
    else:
        print("Installed versions match versions.lock.json")
