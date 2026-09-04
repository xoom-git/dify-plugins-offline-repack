"""Command-line entry: inspect / repack / verify."""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import pkglib, sig, util
from .engine import repack
from .model import RepackOptions


def _cmd_inspect(args: argparse.Namespace) -> int:
    info = pkglib.inspect_difypkg(args.input)
    print(json.dumps({
        "identity": info.identity,
        "type": info.type,
        "runner_python": info.runner_python,
        "dependency_files": info.original_dependency_files,
        "source_size": info.source_size_bytes,
        "source_uncompressed": info.source_uncompressed_bytes,
    }, ensure_ascii=False, indent=2))
    return 0


def _cmd_repack(args: argparse.Namespace) -> int:
    opts = RepackOptions(
        target_python=args.python,
        archs=[a.strip() for a in args.arch.split(",") if a.strip()],
        glibc_minor=args.glibc_minor,
        index_url=args.index_url,
        uv_path=args.uv,
        dry_run=args.dry_run,
    )
    out_dir = os.path.abspath(args.outdir)
    os.makedirs(out_dir, exist_ok=True)

    def progress(msg: str, pct: int) -> None:
        print(f"[{pct:3d}%] {msg}", file=sys.stderr)

    report = repack(os.path.abspath(args.input), args.work, out_dir, opts, progress=progress)
    rp = os.path.join(out_dir, f"report-{report.plugin.author}-{report.plugin.name}-{report.plugin.version}.json"
                      if report.plugin else os.path.join(out_dir, "report-failed.json"))
    with open(rp, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
    print(f"report: {rp}")
    print(f"ok: {report.ok}")
    if report.artifact_path:
        print(f"artifact: {report.artifact_path}")
        print(f"artifact_sha256: {report.artifact_sha256}")
        print(f"size: {report.artifact_size} bytes | uncompressed: {report.uncompressed_size}")
    for w in report.warnings:
        print(f"warning: {w}", file=sys.stderr)
    for e in report.errors:
        print(f"error: {e}", file=sys.stderr)
    return 0 if report.ok else 1


def _cmd_verify(args: argparse.Namespace) -> int:
    """Static checks on an offline package produced by this tool."""
    import zipfile
    path = args.input
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        checks = {
            "manifest.yaml": "manifest.yaml" in names,
            "requirements.txt": "requirements.txt" in names,
            "wheels_dir": any(n.startswith("wheels/") for n in names),
        }
        wheels = sorted({n.split("/")[1] for n in names if n.startswith("wheels/") and n.count("/") == 1})
        req = zf.read("requirements.txt").decode("utf-8", "replace").splitlines()
        header_ok = bool(req and req[0].startswith("--no-index --find-links=./wheels/"))
    print(json.dumps({
        "file": path,
        "checks": checks,
        "header_ok": header_ok,
        "wheel_count": len(wheels),
        "wheels": wheels,
    }, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) and header_ok else 1


def _cmd_keygen(args: argparse.Namespace) -> int:
    out = sig.generate_keypair(args.out)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def _cmd_sign(args: argparse.Namespace) -> int:
    sig.sign_pkg(args.input, args.output, args.key, category=args.category)
    print(f"signed: {args.output}")
    return 0


def _cmd_signverify(args: argparse.Namespace) -> int:
    ok = sig.verify_pkg(args.input, args.public_key)
    print("signature OK" if ok else "signature INVALID")
    return 0 if ok else 1


def _cmd_batch(args: argparse.Namespace) -> int:
    """Repack every .difypkg found in a directory (or listed in a file), one line each."""
    import glob

    sources: list = []
    if os.path.isdir(args.input):
        sources = sorted(glob.glob(os.path.join(args.input, "*.difypkg")))
        if not sources:
            print("no *.difypkg found in directory", file=sys.stderr)
            return 2
    else:
        with open(args.input, "r", encoding="utf-8") as f:
            sources = [ln.strip() for ln in f if ln.strip()]
    opts = RepackOptions(
        target_python=args.python,
        archs=[a.strip() for a in args.arch.split(",") if a.strip()],
        glibc_minor=args.glibc_minor,
        index_url=args.index_url,
        uv_path=args.uv,
    )
    out_dir = os.path.abspath(args.outdir)
    os.makedirs(out_dir, exist_ok=True)
    summary = {"ok": [], "failed": []}
    for i, src in enumerate(sources, 1):
        print(f"[{i}/{len(sources)}] repacking {os.path.basename(src)} ...", file=sys.stderr)
        try:
            report = repack(os.path.abspath(src), args.work, out_dir, opts)
        except Exception as e:  # noqa: BLE001
            summary["failed"].append({"file": src, "error": f"{type(e).__name__}: {e}"})
            continue
        item = {
            "file": os.path.basename(src),
            "ok": report.ok,
            "artifact": os.path.basename(report.artifact_path) if report.artifact_path else None,
            "sha256": report.artifact_sha256,
            "locked": len(report.lock_lines),
            "wheels": len(report.wheels),
            "errors": report.errors,
        }
        (summary["ok"] if report.ok else summary["failed"]).append(item)
        print(f"   -> ok={report.ok}", file=sys.stderr)
    summary_path = os.path.join(out_dir, "batch-summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"batch done: ok={len(summary['ok'])} failed={len(summary['failed'])}")
    print(f"summary: {summary_path}")
    return 0 if not summary["failed"] else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dify-offline-repack",
                                description="Repackage Dify plugins with bundled deps for offline install")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("inspect", help="show package metadata")
    pi.add_argument("input", help=".difypkg path")
    pi.set_defaults(func=_cmd_inspect)

    pr = sub.add_parser("repack", help="build an offline difypkg")
    pr.add_argument("input", help=".difypkg path or plugin source dir")
    pr.add_argument("-o", "--outdir", default="out", help="output directory")
    pr.add_argument("--work", default=os.path.join(os.environ.get("TEMP", "."), "dify-offline-work"),
                    help="scratch dir")
    pr.add_argument("--arch", default="x86_64,aarch64", help="comma-separated archs")
    pr.add_argument("--python", default="3.12", help="target python version")
    pr.add_argument("--glibc-minor", type=int, default=36,
                    help="target image glibc 2.<minor>; accept manylinux_2_N wheels with N<=minor "
                         "(Debian bookworm=36; older images may need 31/28)")
    pr.add_argument("--index-url", default="https://pypi.org/simple")
    pr.add_argument("--uv", default=None, help="path to uv executable")
    pr.add_argument("--dry-run", action="store_true")
    pr.set_defaults(func=_cmd_repack)

    pv = sub.add_parser("verify", help="static checks on an offline difypkg")
    pv.add_argument("input")
    pv.set_defaults(func=_cmd_verify)

    pb = sub.add_parser("batch", help="repack many difypkgs (directory of *.difypkg or a list file)")
    pb.add_argument("input", help="directory containing *.difypkg, or a text file with one path per line")
    pb.add_argument("-o", "--outdir", default="out")
    pb.add_argument("--work", default=os.path.join(os.environ.get("TEMP", "."), "dify-offline-work"))
    pb.add_argument("--arch", default="x86_64")
    pb.add_argument("--python", default="3.12")
    pb.add_argument("--glibc-minor", type=int, default=36)
    pb.add_argument("--index-url", default="https://pypi.org/simple")
    pb.add_argument("--uv", default=None)
    pb.set_defaults(func=_cmd_batch)

    pk = sub.add_parser("keygen", help="generate an RSA-4096 signing key pair (PKCS1 PEM)")
    pk.add_argument("-o", "--out", default="dify_plugin_signing_key")
    pk.set_defaults(func=_cmd_keygen)

    ps = sub.add_parser("sign", help="re-sign a difypkg with our own key (daemon-compatible)")
    ps.add_argument("input", help="difypkg to sign")
    ps.add_argument("-k", "--key", required=True, help="private key PEM")
    ps.add_argument("-o", "--output", required=True, help="output signed difypkg")
    ps.add_argument("--category", default=sig.DEFAULT_CATEGORY,
                    choices=["community", "partner", "langgenius"])
    ps.set_defaults(func=_cmd_sign)

    psv = sub.add_parser("signverify", help="verify a difypkg signature with a public key")
    psv.add_argument("input")
    psv.add_argument("-k", "--public-key", required=True, help="public key PEM")
    psv.set_defaults(func=_cmd_signverify)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    util.setup_logging(args.verbose)
    try:
        return args.func(args)
    except Exception as e:  # noqa: BLE001
        print(f"fatal: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
