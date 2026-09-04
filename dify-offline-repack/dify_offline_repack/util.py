"""Small helpers: subprocess execution, hashing, logging to stderr."""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
import urllib.request
from typing import List, Optional, Tuple

log = logging.getLogger("dify_offline_repack")


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def run(cmd: List[str], cwd: Optional[str] = None, env: Optional[dict] = None,
        timeout: int = 900) -> Tuple[int, str, str]:
    """Run a command; returns (exitcode, stdout, stderr). Never raises on non-zero."""
    p = subprocess.run(
        cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
    )
    return p.returncode, p.stdout or "", p.stderr or ""


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def http_get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "dify-offline-repack/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def find_uv(uv_path: Optional[str] = None) -> str:
    if uv_path:
        return uv_path
    for name in ("uv", "uv.exe"):
        p = os.path.join(".")  # noqa
    import shutil
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv executable not found on PATH (set --uv / UV_PATH)")
    return uv


def ensure_dir(p: str) -> str:
    os.makedirs(p, exist_ok=True)
    return p
