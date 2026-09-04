"""dify-offline-repack Web service: upload official .difypkg -> repack -> download.

Run:  .venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8010
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---- engine on sys.path (installed editable or sibling checkout) ----
_ENGINE = os.environ.get(
    "DIFY_OFFLINE_ENGINE",
    str(Path(__file__).resolve().parent.parent / "dify-offline-repack"),
)
if _ENGINE not in sys.path:
    sys.path.insert(0, _ENGINE)

from dify_offline_repack import engine as repack_engine  # noqa: E402
from dify_offline_repack import sig, util  # noqa: E402
from dify_offline_repack.model import RepackOptions  # noqa: E402

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("DIFY_OFFLINE_DATA", BASE / "data"))
JOBS = DATA / "jobs"
UPLOADS = DATA / "uploads"
os.makedirs(JOBS, exist_ok=True)
os.makedirs(UPLOADS, exist_ok=True)

executor = ThreadPoolExecutor(max_workers=int(os.environ.get("DIFY_OFFLINE_WORKERS", "2")))

app = FastAPI(title="Dify 插件离线重打包", version="0.1.0")

STATUS_LIVE: Dict[str, dict] = {}  # job_id -> runtime progress

DEFAULT_ARCHS = ["x86_64", "aarch64"]


def _job_path(job_id: str) -> Path:
    return JOBS / job_id


def _read_job(job_id: str) -> Optional[dict]:
    p = _job_path(job_id) / "job.json"
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_job(job: dict) -> None:
    d = _job_path(job["id"])
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "job.json", "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)


def _run_job(job_id: str) -> None:
    jdir = _job_path(job_id)
    job = _read_job(job_id)
    try:
        upload = jdir / "upload.difypkg"
        opts = RepackOptions(
            target_python=job.get("python", "3.12"),
            archs=job.get("archs") or list(DEFAULT_ARCHS),
            index_url=job.get("index_url") or "https://pypi.org/simple",
            glibc_minor=int(job.get("glibc_minor", 36)),
        )
        out_dir = jdir / "out"
        os.makedirs(out_dir, exist_ok=True)
        work_root = DATA / "work"
        os.makedirs(work_root, exist_ok=True)

        def progress(msg: str, pct: int) -> None:
            STATUS_LIVE[job_id] = {"phase": msg, "progress": pct}

        report = repack_engine.repack(str(upload), str(work_root), str(out_dir),
                                      opts, progress=progress)
        rep = report.to_dict()
        # optional sign (secure path): server-side org private key
        if report.ok and job.get("sign"):
            key = os.environ.get("DIFY_OFFLINE_PRIVATE_KEY")
            if not key:
                raise RuntimeError("sign requested but DIFY_OFFLINE_PRIVATE_KEY env is not set on server")
            unsigned = rep.get("artifact_path")
            signed = os.path.join(out_dir, os.path.basename(unsigned).replace(".difypkg", ".signed.difypkg"))
            sig.sign_pkg(unsigned, signed, key, category="community")
            rep["artifact_path"] = signed
            rep["signed"] = True
            rep["artifact_sha256"] = util.sha256_file(signed) if hasattr(util, "sha256_file") else None
            rep["artifact_size"] = os.path.getsize(signed)
        with open(jdir / "report.json", "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
        artifact = rep.get("artifact_path")
        pl = rep.get("plugin") or {}
        job.update({
            "status": "done" if report.ok else "failed",
            "phase": "完成" if report.ok else "失败",
            "progress": 100,
            "ok": report.ok,
            "signed": rep.get("signed", False),
            "plugin_author": pl.get("author"),
            "plugin_name": pl.get("name"),
            "plugin_version": pl.get("version"),
            "artifact_file": os.path.basename(artifact) if artifact else None,
            "artifact_size": rep.get("artifact_size"),
            "artifact_sha256": rep.get("artifact_sha256"),
            "uncompressed_size": rep.get("uncompressed_size"),
            "locked": len(rep.get("lock_lines") or []),
            "wheels": len(rep.get("wheels") or []),
            "recheck": rep.get("offline_recheck_ok"),
            "transformations": rep.get("transformations"),
            "warnings": rep.get("warnings"),
            "errors": rep.get("errors"),
            "finished": time.time(),
        })
    except Exception as e:  # noqa: BLE001
        job.update({"status": "error", "phase": "异常", "progress": 100,
                    "errors": [f"{type(e).__name__}: {e}"], "finished": time.time()})
    finally:
        STATUS_LIVE.pop(job_id, None)
        _write_job(job)


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/meta")
def meta():
    return {
        "app": "dify-offline-repack",
        "app_version": "0.2.0",
        "engine_version": getattr(repack_engine, "__version__", "0.1.0") if hasattr(repack_engine, "__version__") else "0.1.0",
        "target_daemon": "langgenius/dify-plugin-daemon:0.6.10-local (python 3.12 / uv)",
        "workers": int(os.environ.get("DIFY_OFFLINE_WORKERS", "2")),
    }


@app.post("/api/jobs")
async def create_job(
    file: UploadFile = File(...),
    arch: str = Form("both"),
    index_url: str = Form("https://pypi.org/simple"),
    python: str = Form("3.12"),
    sign: str = Form("false"),
):
    job_id = uuid.uuid4().hex[:12]
    jdir = _job_path(job_id)
    jdir.mkdir(parents=True, exist_ok=True)
    upload = jdir / "upload.difypkg"
    with open(upload, "wb") as f:
        shutil.copyfileobj(file.file, f)
    if not file.filename:
        raise HTTPException(400, "empty filename")
    archs = {"both": list(DEFAULT_ARCHS),
             "x86_64": ["x86_64"],
             "aarch64": ["aarch64"]}.get(arch)
    if not archs:
        raise HTTPException(400, f"unknown arch: {arch}")
    job = {
        "id": job_id,
        "filename": file.filename,
        "archs": archs,
        "index_url": index_url,
        "python": python,
        "sign": sign.lower() in ("true", "1", "yes"),
        "status": "queued",
        "phase": "排队中",
        "progress": 0,
        "created": time.time(),
        "errors": [],
    }
    _write_job(job)
    executor.submit(_run_job, job_id)
    return {"id": job_id}


@app.get("/api/jobs")
def list_jobs(limit: int = 20):
    ids = sorted((p.name for p in JOBS.iterdir() if (p / "job.json").exists()),
                 key=lambda i: _read_job(i)["created"], reverse=True)[:limit]
    out = []
    for i in ids:
        j = _read_job(i)
        if j is not None:
            live = STATUS_LIVE.get(i)
            if live and j["status"] in ("queued",):
                j["status"] = "running"
            out.append({
                "id": j["id"], "filename": j["filename"], "status": j["status"],
                "phase": (live or {}).get("phase", j.get("phase")),
                "progress": (live or {}).get("progress", j.get("progress")),
                "archs": j.get("archs"), "created": j.get("created"),
                "ok": j.get("ok"), "signed": j.get("signed", False),
                "artifact_file": j.get("artifact_file"),
                "errors": j.get("errors"), "locked": j.get("locked"),
                "wheels": j.get("wheels"), "recheck": j.get("recheck"),
            })
    return out


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    j = _read_job(job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    live = STATUS_LIVE.get(job_id)
    if live:
        j["status"] = "running"
        j["phase"] = live.get("phase")
        j["progress"] = live.get("progress")
    return j


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str):
    j = _read_job(job_id)
    if j is None or not j.get("artifact_file"):
        raise HTTPException(404, "artifact not found")
    path = _job_path(job_id) / "out" / j["artifact_file"]
    if not path.exists():
        raise HTTPException(404, "artifact file missing")
    return FileResponse(path, filename=j["artifact_file"],
                        media_type="application/octet-stream")


@app.get("/api/jobs/{job_id}/report")
def report(job_id: str):
    j = _read_job(job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    p = _job_path(job_id) / "report.json"
    if not p.exists():
        return JSONResponse({"note": "report not ready"})
    with open(p, "r", encoding="utf-8") as f:
        return JSONResponse(json.load(f))


# ------------------------------------------------------------------ task management
import re as _re  # noqa: E402

_ID_RE = _re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _valid_id(job_id: str) -> None:
    if not _ID_RE.match(job_id):
        raise HTTPException(400, "invalid job id")


def _running(j: dict) -> bool:
    return j.get("status") in ("queued", "running")


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    """Delete one terminal job and all its files (upload/out/report)."""
    _valid_id(job_id)
    j = _read_job(job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    if _running(j):
        raise HTTPException(409, "job is running; wait for it to finish before deleting")
    jdir = _job_path(job_id)
    import shutil
    shutil.rmtree(jdir, ignore_errors=True)
    STATUS_LIVE.pop(job_id, None)
    return {"id": job_id, "deleted": True}


@app.post("/api/jobs/{job_id}/retry")
def retry_job(job_id: str):
    """Re-run a terminal job with its original upload and options."""
    _valid_id(job_id)
    j = _read_job(job_id)
    if j is None:
        raise HTTPException(404, "job not found")
    if _running(j):
        raise HTTPException(409, "job is still running")
    jdir = _job_path(job_id)
    upload = jdir / "upload.difypkg"
    if not upload.exists():
        raise HTTPException(409, "original upload file is missing")
    # clear previous outputs, keep upload + options
    import shutil
    out = jdir / "out"
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    rep = jdir / "report.json"
    if rep.exists():
        rep.unlink()
    j.update({"status": "queued", "phase": "排队中", "progress": 0,
              "ok": None, "artifact_file": None, "artifact_sha256": None,
              "artifact_size": None, "errors": [], "finished": None})
    _write_job(j)
    executor.submit(_run_job, job_id)
    return {"id": job_id, "status": "queued"}


@app.post("/api/jobs/clear-failed")
def clear_failed():
    """Delete every failed/error job in one click."""
    removed = []
    for p in JOBS.iterdir():
        if not (p / "job.json").exists():
            continue
        j = _read_job(p.name)
        if j is None or _running(j):
            continue
        if j.get("status") in ("failed", "error"):
            import shutil
            shutil.rmtree(p, ignore_errors=True)
            STATUS_LIVE.pop(p.name, None)
            removed.append(p.name)
    return {"removed": removed}


# ------------------------------------------------------------------ url upload / artifacts / download-all

def _enqueue_upload(job_id: str, raw: bytes, filename: str, arch: str,
                    index_url: str, python: str, sign: bool) -> dict:
    jdir = _job_path(job_id)
    jdir.mkdir(parents=True, exist_ok=True)
    with open(jdir / "upload.difypkg", "wb") as f:
        f.write(raw)
    archs = {"both": list(DEFAULT_ARCHS), "x86_64": ["x86_64"], "aarch64": ["aarch64"]}.get(arch)
    if not archs:
        raise HTTPException(400, f"unknown arch: {arch}")
    job = {"id": job_id, "filename": filename, "archs": archs, "index_url": index_url,
           "python": python, "sign": bool(sign), "status": "queued", "phase": "排队中",
           "progress": 0, "created": time.time(), "errors": []}
    _write_job(job)
    executor.submit(_run_job, job_id)
    return {"id": job_id}


@app.post("/api/jobs/from-url")
def create_job_from_url(url: str = Form(...), arch: str = Form("both"),
                        index_url: str = Form("https://pypi.org/simple"),
                        python: str = Form("3.12"), sign: str = Form("false")):
    """Fetch a .difypkg from an http(s) URL, then enqueue a repack job.

    Runs as a sync endpoint (threadpool) so a slow remote never blocks the event loop.
    """
    import urllib.parse
    import urllib.request
    if not url.lower().startswith(("http://", "https://")):
        raise HTTPException(400, "url must be http(s)")
    cap = int(os.environ.get("DIFY_OFFLINE_MAX_REMOTE_MB", "200")) * 1024 * 1024
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dify-offline-repack/0.2"})
        with urllib.request.urlopen(req, timeout=120) as r:
            chunk = r.read(cap + 1)
            if len(chunk) > cap:
                raise HTTPException(413, f"remote package exceeds {cap} bytes")
            name = os.path.basename(urllib.parse.urlparse(url).path) or "remote.difypkg"
            if not name.lower().endswith(".difypkg"):
                name += ".difypkg"
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"failed to fetch url: {e}") from e
    if not chunk[:2] == b"PK":
        raise HTTPException(400, "url does not point to a valid .difypkg (zip)")
    job_id = uuid.uuid4().hex[:12]
    return _enqueue_upload(job_id, chunk, name, arch, index_url, python,
                           sign.lower() in ("true", "1", "yes"))


@app.post("/api/jobs/from-upload")
def create_job_upload(file: UploadFile = File(...), arch: str = Form("both"),
                      index_url: str = Form("https://pypi.org/simple"),
                      python: str = Form("3.12"), sign: str = Form("false")):
    job_id = uuid.uuid4().hex[:12]
    raw = file.file.read()
    if not file.filename:
        raise HTTPException(400, "empty filename")
    return _enqueue_upload(job_id, raw, file.filename, arch, index_url, python,
                           sign.lower() in ("true", "1", "yes"))


def _job_plugin_identity(j: dict):
    if j.get("plugin_author") and j.get("plugin_name") and j.get("plugin_version"):
        return {"author": j["plugin_author"], "name": j["plugin_name"], "version": j["plugin_version"]}
    rp = _job_path(j["id"]) / "report.json"
    if rp.exists():
        try:
            rep = json.load(open(rp, encoding="utf-8"))
            pl = rep.get("plugin") or {}
            return {"author": pl.get("author"), "name": pl.get("name"), "version": pl.get("version")}
        except Exception:
            return {}
    return {}


@app.get("/api/artifacts")
def artifacts():
    """Version-controlled index of produced offline packages (newest job per plugin/version)."""
    rows = []
    for p in JOBS.iterdir():
        jf = p / "job.json"
        if not jf.exists():
            continue
        j = _read_job(p.name)
        if j is None or j.get("status") != "done" or not j.get("artifact_file"):
            continue
        art = p / "out" / j["artifact_file"]
        if not art.exists():
            continue
        ident = _job_plugin_identity(j)
        rows.append({
            "job_id": j["id"], "filename": j.get("filename"),
            "plugin": ident, "signed": j.get("signed", False),
            "artifact_file": j["artifact_file"], "size": os.path.getsize(art),
            "sha256": j.get("artifact_sha256"), "archs": j.get("archs"),
            "created": j.get("created"),
        })
    seen = {}
    for row in rows:
        a, n, v = (row["plugin"].get("author"), row["plugin"].get("name"), row["plugin"].get("version"))
        if a and n and v:
            key = (a, n, v)
            if key not in seen:
                seen[key] = row
    items = sorted(seen.values(), key=lambda r: r.get("created") or 0, reverse=True)
    return {"total": len(items), "items": items}


@app.get("/api/download-all")
def download_all(background: BackgroundTasks):
    """One-click download: zip of every current done artifact."""
    arts = []
    for p in JOBS.iterdir():
        j = _read_job(p.name) if (p / "job.json").exists() else None
        if j is None or j.get("status") != "done" or not j.get("artifact_file"):
            continue
        f = _job_path(p.name) / "out" / j["artifact_file"]
        if f.exists():
            arts.append(f)
    if not arts:
        raise HTTPException(404, "no finished artifacts yet")
    import zipfile
    os.makedirs(DATA / "tmp", exist_ok=True)
    tmp = DATA / "tmp" / f"offline-packages-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for f in arts:
            z.write(f, f.name)
    background.add_task(os.remove, tmp)
    return FileResponse(tmp, filename=tmp.name, media_type="application/zip")


app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
