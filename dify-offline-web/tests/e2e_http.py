"""HTTP e2e test for the repack web service."""
import hashlib
import json
import sys
import time

import requests

BASE = "http://127.0.0.1:8010"
PKG = r"E:\DIFY_PLUGIN\web_001\webscraper.difypkg"
SAVE = r"E:\DIFY_PLUGIN\.research\web-downloaded.difypkg"


def main():
    # 0. wait ready
    for _ in range(60):
        try:
            r = requests.get(BASE + "/", timeout=3)
            if r.status_code == 200:
                break
        except Exception:
            time.sleep(2)
    else:
        print("SERVER NOT READY"); return 1
    assert "Dify 插件离线重打包" in r.text, "page title missing"
    print("GET /  ->", r.status_code, "html bytes", len(r.content))

    # 1. create job (upload official pkg, dual arch)
    with open(PKG, "rb") as f:
        r = requests.post(BASE + "/api/jobs",
                          files={"file": ("webscraper.difypkg", f, "application/octet-stream")},
                          data={"arch": "both", "index_url": "https://pypi.org/simple"}, timeout=60)
    assert r.status_code == 200, r.text
    job = r.json()
    jid = job["id"]
    print("POST /api/jobs ->", jid)

    # 2. poll
    for _ in range(240):
        r = requests.get(BASE + f"/api/jobs/{jid}", timeout=10)
        j = r.json()
        st = j["status"]
        if st in ("done", "failed", "error"):
            break
        time.sleep(2)
    else:
        print("TIMEOUT waiting job"); return 1
    print("job status =", st, "| phase =", j.get("phase"), "| progress =", j.get("progress"))
    print("ok =", j.get("ok"), "| locked =", j.get("locked"), "| wheels =", j.get("wheels"),
          "| recheck =", j.get("recheck"))
    if j.get("errors"):
        print("errors:", j["errors"])
    if st != "done":
        return 1

    # 3. download artifact
    dl = requests.get(BASE + f"/api/jobs/{jid}/download", timeout=60)
    assert dl.status_code == 200, dl.text
    with open(SAVE, "wb") as f:
        f.write(dl.content)
    sha = hashlib.sha256(dl.content).hexdigest()
    print("download ok:", len(dl.content), "bytes | sha256", sha)
    print("expected  sha256:", j.get("artifact_sha256"))
    assert sha == j.get("artifact_sha256"), "sha mismatch"

    # 4. report endpoint
    rep = requests.get(BASE + f"/api/jobs/{jid}/report", timeout=10).json()
    print("report ok =", rep.get("ok"), "| wheels records =", len(rep.get("wheels") or []),
          "| recheck =", rep.get("offline_recheck_ok"))
    assert rep.get("ok") is True and rep.get("offline_recheck_ok") is True
    print("E2E-HTTP PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
