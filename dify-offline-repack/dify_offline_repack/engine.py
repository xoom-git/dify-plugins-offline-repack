"""Repack orchestration: difypkg/source-dir -> offline-installable difypkg."""
from __future__ import annotations

import os
import shutil
import time
from typing import Callable, Dict, Optional

from . import inject, lock, pkglib, util, wheels
from .model import RepackOptions, RepackReport

Progress = Callable[[str, int], None]


def _progress_none(msg: str, pct: int) -> None:
    pass


def _write_text(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _ensure_no_runtime_leftovers(src: str, report: RepackReport) -> None:
    leftovers = (".venv", ".uv-cache")
    for name in leftovers:
        p = os.path.join(src, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
            report.transformations.append(f"removed leftover runtime dir: {name}")
    # drop a stale signature file carried from a source package: our repack changes the
    # payload, so any inherited .verification.dify.json would mislead upload-time checks
    verf = os.path.join(src, ".verification.dify.json")
    if os.path.exists(verf):
        os.remove(verf)
        report.transformations.append("removed stale .verification.dify.json inherited from source package")
    for root, dirs, _files in os.walk(src):
        dirs[:] = [d for d in dirs if d != "__pycache__"]


def repack(input_path: str, work_root: str, out_dir: str, opts: Optional[RepackOptions] = None,
           progress: Progress = _progress_none) -> RepackReport:
    """Turn one .difypkg (or plugin source dir) into an offline difypkg.

    Returns a report (ok field is the source of truth). Raises on hard environment errors.
    """
    opts = opts or RepackOptions()
    start = time.time()

    # ---------------- stage dirs ----------------
    job = os.path.join(work_root, f"job-{int(time.time() * 1000)}")
    src = os.path.join(job, "src")
    os.makedirs(src, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)
    is_dir_input = os.path.isdir(input_path)

    try:
        # ---------------- 1. source ----------------
        progress("解析插件包", 5)
        if is_dir_input:
            for entry in os.listdir(input_path):
                s = os.path.join(input_path, entry)
                d = os.path.join(src, entry)
                if os.path.isdir(s):
                    shutil.copytree(s, d)
                else:
                    shutil.copy2(s, d)
            info = pkglib.inspect_manifest(open(os.path.join(src, "manifest.yaml"), "rb").read())
        else:
            pkglib.extract_difypkg(input_path, src)
            info = pkglib.inspect_difypkg(input_path)
        report = RepackReport(ok=False, plugin=info, lock_lines=[], wheels=[])
        util.log.info("plugin: %s (%s)", info.identity, info.type)
        _ensure_no_runtime_leftovers(src, report)

        # ---------------- 2. dependency file normalization ----------------
        progress("归一化依赖文件", 12)
        has_req = os.path.exists(os.path.join(src, "requirements.txt"))
        has_pyproject = os.path.exists(os.path.join(src, "pyproject.toml"))
        if not (has_req or has_pyproject):
            raise RuntimeError("plugin declares no requirements.txt / pyproject.toml dependency file")
        if has_pyproject and not has_req:
            report.transformations += inject.pyproject_to_requirements(src, info.name)
        elif has_pyproject and has_req:
            # daemon prefers pyproject when no mirror configured; normalize to requirements path
            report.transformations += inject.pyproject_to_requirements(src, info.name)
        req_path = os.path.join(src, "requirements.txt")
        if not os.path.exists(req_path):
            raise RuntimeError("no requirements.txt after normalization")

        # ---------------- 3. lock with uv (cp312 guard + down-pin) ----------------
        progress("uv 锁定依赖 (linux / py" + opts.target_python + ")", 25)
        uv = util.find_uv(opts.uv_path)
        lock_dir = os.path.join(job, "lockwork")
        os.makedirs(lock_dir, exist_ok=True)
        lock_path = os.path.join(lock_dir, "lock.txt")
        report.lock_lines = lock.lock_pins_cp312(
            req_path, lock_dir,
            target_python=opts.target_python,
            index_url=opts.index_url,
            uv_path=uv,
            archs=opts.archs,
            glibc_minor=opts.glibc_minor,
        )
        util.log.info("locked %d packages (cp312-guarded)", len(report.lock_lines))

        # ---------------- 4. acquire wheels per arch ----------------
        wheels_dir = os.path.join(src, "wheels")
        os.makedirs(wheels_dir, exist_ok=True)
        any_shared: Dict[str, str] = {}
        all_missing: Dict[str, list] = {}
        arch_records = {}
        total = len(opts.archs) * max(1, len(report.lock_lines))
        done = 0
        for arch in opts.archs:
            progress(f"获取 {arch} wheel", 30 + int(35 * done / max(total, 1)))
            records, missing = wheels.download_wheels_for_pins(
                report.lock_lines, wheels_dir,
                arch=arch, index_url=opts.index_url, glibc_minor=opts.glibc_minor,
                allow_any_shared=any_shared,
            )
            arch_records[arch] = records
            if missing:
                all_missing[arch] = missing
            done += max(1, len(report.lock_lines))
        report.wheels = [r for recs in arch_records.values() for r in recs]
        if all_missing:
            # sdist fallback: deps with no cp312 wheels on any requested arch are fetched as
            # sdists; uv builds them offline from local build-backend wheels (see docs V12).
            unique_missing = sorted({p for v in all_missing.values() for p in v})
            handled, still_missing = [], []
            for pin in unique_missing:
                fn = wheels.download_sdist(pin, wheels_dir, opts.index_url)
                if fn:
                    handled.append(f"{pin} -> {fn}")
                else:
                    still_missing.append(pin)
            if handled:
                backend = wheels.ensure_build_backends(wheels_dir, opts.index_url)
                report.transformations.append(
                    "sdist fallback for deps without cp312 wheels: " + "; ".join(handled))
                if backend:
                    report.transformations.append("added offline build backends: " + ", ".join(backend))
            if still_missing:
                report.errors.append(
                    "missing wheels for requested archs and no sdist available to build offline. "
                    f"pins: {'; '.join(still_missing)}. "
                    "The dependency is not offline-bundlable on daemon python 3.12.")
                progress("失败：缺依赖源", 100)
                report.ok = False
                return report
            all_missing.clear()

        # ---------------- 5. offline injection ----------------
        progress("注入离线配置", 70)
        report.transformations += inject.rewrite_requirements(req_path)
        report.transformations.append(f"bundled {len(os.listdir(wheels_dir))} wheel/sdist files into wheels/")

        # ---------------- 6. package ----------------
        progress("重新打包", 82)
        artifact = os.path.join(
            out_dir,
            f"{info.author}-{info.name}-{info.version}-offline.difypkg",
        )
        pkglib.write_zip_from_dir(src, artifact)
        report.artifact_path = artifact
        report.artifact_size = os.path.getsize(artifact)
        report.artifact_sha256 = util.sha256_file(artifact)
        report.uncompressed_size = inject.summarize_uncompressed(artifact)
        # size-limit pre-warning: daemon default MAX_PLUGIN_PACKAGE_SIZE = 52428800 applies to
        # both the upload body and the total uncompressed size
        for label, value in (("compressed", report.artifact_size), ("uncompressed", report.uncompressed_size)):
            if value >= 45 * 1024 * 1024:
                report.warnings.append(
                    f"artifact {label} size {value:,} bytes is near/above the default 52428800-byte "
                    "limit: set PLUGIN_MAX_PACKAGE_SIZE (and NGINX_CLIENT_MAX_BODY_SIZE) on the target "
                    "Dify .env before upload")

        # ---------------- 7. verify (per requested arch) ----------------
        progress("离线闭包自检", 90)
        _triple = {"x86_64": "x86_64-unknown-linux-gnu", "aarch64": "aarch64-unknown-linux-gnu"}
        per_arch = []
        for arch in opts.archs:
            platform = _triple.get(arch, "linux")
            ok_arch = lock.offline_recheck(
                lock_path, wheels_dir,
                target_python=opts.target_python, uv_path=uv,
                python_platform=platform,
            )
            per_arch.append(ok_arch)
            report.transformations.append(f"offline recheck[{arch}]={'ok' if ok_arch else 'FAIL'}")
        recheck_ok = all(per_arch)
        report.offline_recheck_ok = recheck_ok
        if not recheck_ok:
            report.errors.append("offline recheck failed: the bundled wheel set cannot satisfy the "
                                 "locked closure with --no-index on every requested arch; the artifact "
                                 "is NOT offline-capable")
        if recheck_ok and not report.errors:
            report.ok = True
        report.transformations.append(f"elapsed {time.time() - start:.1f}s")
        progress("完成", 100)
        return report
    except Exception as e:  # noqa: BLE001
        util.log.exception("repack failed")
        report = report if "report" in dir() else None
        rep = report if report is not None else RepackReport(ok=False, plugin=info if "info" in dir() else None,
                                                             lock_lines=[], wheels=[])
        rep.errors.append(f"{type(e).__name__}: {e}")
        progress("失败", 100)
        return rep
    finally:
        if os.path.isdir(job):
            shutil.rmtree(job, ignore_errors=True)
