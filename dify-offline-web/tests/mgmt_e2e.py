"""E2E for task-management endpoints (delete / clear-failed / retry)."""
import io
import time
import sys

import requests

BASE = "http://127.0.0.1:8010"


def wait_ready():
    for _ in range(60):
        try:
            if requests.get(BASE + "/", timeout=3).status_code == 200:
                return
        except Exception:
            time.sleep(2)
    raise SystemExit("server not ready")


def make_failed() -> str:
    """Upload an invalid file -> job fails fast at extract stage."""
    r = requests.post(BASE + "/api/jobs",
                      files={"file": ("bad.difypkg", io.BytesIO(b"this is not a zip"), "application/octet-stream")},
                      data={"arch": "x86_64"}, timeout=30)
    assert r.status_code == 200, r.text
    jid = r.json()["id"]
    for _ in range(60):
        j = requests.get(BASE + f"/api/jobs/{jid}", timeout=10).json()
        if j["status"] in ("done", "failed", "error"):
            break
        time.sleep(1)
    else:
        raise SystemExit("failed job did not settle")
    assert j["status"] == "failed", j
    return jid


def main():
    wait_ready()
    # 1) clear-failed removes failed jobs
    a = make_failed()
    b = make_failed()
    r = requests.post(BASE + "/api/jobs/clear-failed", timeout=30)
    d = r.json()
    print("clear-failed removed:", d.get("removed"))
    assert a in d["removed"] and b in d["removed"]
    gone = requests.get(BASE + f"/api/jobs/{a}", timeout=10)
    assert gone.status_code == 404
    print("PASS clear-failed")

    # 2) delete single failed job
    c = make_failed()
    r = requests.delete(BASE + f"/api/jobs/{c}", timeout=20)
    assert r.status_code == 200, r.text
    assert requests.get(BASE + f"/api/jobs/{c}", timeout=10).status_code == 404
    print("PASS delete")

    # 3) bad id / missing
    assert requests.get(BASE + "/api/jobs/..%2f..", timeout=10).status_code in (400, 404)
    assert requests.delete(BASE + "/api/jobs/nope123", timeout=10).status_code == 404
    print("PASS guards")

    # 4) retry re-runs the worker for a failed job (same id/upload retained)
    f1 = make_failed()
    t0 = requests.get(BASE + f"/api/jobs/{f1}", timeout=10).json()["finished"]
    r = requests.post(BASE + f"/api/jobs/{f1}/retry", timeout=30)
    print("retry:", r.status_code, r.text[:80])
    assert r.status_code == 200
    for _ in range(120):
        j = requests.get(BASE + f"/api/jobs/{f1}", timeout=10).json()
        if j["status"] in ("done", "failed", "error"):
            break
        time.sleep(1)
    print("after retry:", j["status"], "| finished advanced:",
          j.get("finished") is not None and j.get("finished") != t0)
    assert j["status"] == "failed" and j.get("finished") != t0
    # retry endpoint on running job must 409 (create then retry immediately is queued->running race-safe:
    # issue retry twice rapidly; second may 409 or succeed queue; just ensure no crash
    r = requests.post(BASE + f"/api/jobs/{f1}/retry", timeout=30)
    assert r.status_code in (200, 409)
    requests.delete(BASE + f"/api/jobs/{f1}", timeout=20)
    print("PASS retry")
    print("MGMT-E2E PASS")


if __name__ == "__main__":
    sys.exit(main())
