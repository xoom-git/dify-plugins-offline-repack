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

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
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
        job.update({
            "status": "done" if report.ok else "failed",
            "phase": "完成" if report.ok else "失败",
            "progress": 100,
            "ok": report.ok,
            "signed": rep.get("signed", False),
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


app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
