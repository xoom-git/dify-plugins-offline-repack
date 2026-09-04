"""E2E: multi-select artifact download."""
import io
import time
import sys
import zipfile

import requests

BASE = "http://127.0.0.1:8010"


def wait_ready():
    for _ in range(60):
        try:
            if requests.get(BASE + "/api/artifacts", timeout=3).status_code == 200:
                return
        except Exception:
            time.sleep(2)
    raise SystemExit("server not ready")


def ensure_two_done():
    """Create a quick done job if fewer than two artifacts exist."""
    a = requests.get(BASE + "/api/artifacts", timeout=10).json()["items"]
    if len(a) >= 2:
        return [x["job_id"] for x in a[:2]]
    # create webscraper x86 job (1-2 min)
    r = requests.post(BASE + "/api/jobs/from-upload",
                      files={"file": ("webscraper.difypkg", open(r"E:\DIFY_PLUGIN\web_001\webscraper.difypkg", "rb"))},
                      data={"arch": "x86_64"}, timeout=60)
    jid = r.json()["id"]
    for _ in range(300):
        j = requests.get(BASE + f"/api/jobs/{jid}", timeout=10).json()
        if j["status"] in ("done", "failed", "error"):
            break
        time.sleep(2)
    assert j["status"] == "done", j.get("errors")
    a = requests.get(BASE + "/api/artifacts", timeout=10).json()["items"]
    return [x["job_id"] for x in a[:2]]


def main():
    wait_ready()
    ids = ensure_two_done()
    print("selected ids:", ids)
    r = requests.post(BASE + "/api/download-selected",
                      json={"job_ids": ids}, timeout=120)
    assert r.status_code == 200, r.text[:200]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    print("zip entries:", names)
    assert len(names) == len(ids) and all(n.endswith(".difypkg") for n in names)
    # invalid/empty
    assert requests.post(BASE + "/api/download-selected", json={"job_ids": []},
                         timeout=30).status_code == 404
    assert requests.post(BASE + "/api/download-selected",
                         json={"job_ids": ["nope-does-not-exist"]}, timeout=30).status_code == 404
    print("SELECT-DL E2E PASS")


if __name__ == "__main__":
    sys.exit(main())
