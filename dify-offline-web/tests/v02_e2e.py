"""E2E: v0.2 web (multi/url upload, artifacts version index, download-all)."""
import io
import os
import time
import zipfile
import sys

import requests

BASE = "http://127.0.0.1:8010"


def wait_ready():
    for _ in range(60):
        try:
            if requests.get(BASE + "/api/meta", timeout=3).status_code == 200:
                return
        except Exception:
            time.sleep(2)
    raise SystemExit("server not ready")


def wait_job(jid, timeout=420):
    for _ in range(int(timeout / 2)):
        j = requests.get(BASE + f"/api/jobs/{jid}", timeout=10).json()
        if j["status"] in ("done", "failed", "error"):
            return j
        time.sleep(2)
    raise SystemExit("job timeout")


def main():
    wait_ready()
    m = requests.get(BASE + "/api/meta", timeout=10).json()
    print("meta:", m["app"], m["app_version"], "engine", m["engine_version"])
    assert m["app_version"] == "0.2.0"

    # artifacts index reflects existing done jobs
    a = requests.get(BASE + "/api/artifacts", timeout=10).json()
    print("artifacts total:", a["total"], "first:", (a["items"][0].get("plugin") if a["items"] else None))
    assert "items" in a

    # from-url failure path (unreachable host)
    r = requests.post(BASE + "/api/jobs/from-url",
                      data={"url": "http://127.0.0.1:9/nope.difypkg",
                            "arch": "x86_64"}, timeout=30)
    print("from-url bad host status:", r.status_code)
    assert r.status_code in (400, 502)

    # from-url success: local http server serving the official agent pkg
    pkg = r"E:\DIFY_PLUGIN\.research\matrix\agent.difypkg"
    assert os.path.exists(pkg)
    import http.server
    import socketserver
    import threading
    d = os.path.dirname(pkg)
    os.chdir(d)
    h = http.server.SimpleHTTPRequestHandler
    httpd = socketserver.TCPServer(("127.0.0.1", 8321), h)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:8321/agent.difypkg"
    r = requests.post(BASE + "/api/jobs/from-url",
                      data={"url": url, "arch": "x86_64", "sign": "false"}, timeout=60)
    print("from-url:", r.status_code, r.text[:80])
    assert r.status_code == 200
    jid = r.json()["id"]
    j = wait_job(jid)
    print("url job:", j["status"], "| ok", j.get("ok"), "| wheels", j.get("wheels"))
    assert j["status"] == "done"
    httpd.shutdown()

    # artifacts contains the new one
    a2 = requests.get(BASE + "/api/artifacts", timeout=10).json()
    print("artifacts total after url job:", a2["total"])
    assert any(x.get("job_id") == jid for x in a2["items"]) or a2["total"] > a["total"]

    # download-all zip
    dl = requests.get(BASE + "/api/download-all", timeout=120)
    assert dl.status_code == 200, dl.status_code
    z = zipfile.ZipFile(io.BytesIO(dl.content))
    names = z.namelist()
    print("download-all zip entries:", len(names), "| sample:", names[:3])
    assert any(n.endswith(".difypkg") for n in names)
    print("V02-E2E PASS")


if __name__ == "__main__":
    sys.exit(main())
