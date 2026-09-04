"""Poll an existing web job to completion and validate download."""
import hashlib
import sys
import time

import requests

BASE = "http://127.0.0.1:8010"
JID = sys.argv[1] if len(sys.argv) > 1 else "129cc12f9e56"
SAVE = r"E:\DIFY_PLUGIN\.research\web-downloaded.difypkg"

for _ in range(180):
    j = requests.get(BASE + f"/api/jobs/{JID}", timeout=10).json()
    if j["status"] in ("done", "failed", "error"):
        break
    time.sleep(2)
else:
    print("TIMEOUT"); sys.exit(1)

print("status", j["status"], "ok", j.get("ok"), "wheels", j.get("wheels"),
      "recheck", j.get("recheck"), "artifact", j.get("artifact_file"))
if j.get("errors"):
    print("errors:", j["errors"])
if j["status"] != "done":
    sys.exit(1)
dl = requests.get(BASE + f"/api/jobs/{JID}/download", timeout=120)
assert dl.status_code == 200, dl.text
with open(SAVE, "wb") as f:
    f.write(dl.content)
sha = hashlib.sha256(dl.content).hexdigest()
print("downloaded bytes", len(dl.content), "sha256", sha)
print("expected sha256 ", j.get("artifact_sha256"))
assert sha == j.get("artifact_sha256")
rep = requests.get(BASE + f"/api/jobs/{JID}/report", timeout=10).json()
print("report ok", rep.get("ok"), "recheck", rep.get("offline_recheck_ok"),
      "wheels", len(rep.get("wheels") or []))
print("POLL-E2E PASS")
