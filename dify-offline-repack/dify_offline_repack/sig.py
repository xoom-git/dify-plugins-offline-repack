"""Dify-plugin signature support (RSA-4096 PKCS1v15/SHA256, zip-comment signature).

Byte-for-byte compatible with the official Go signer/verifier
(pkg/plugin_packager/signer/withkey + decoder/verifier.go in dify-plugin-daemon):

digest(ordered file shas)  := sha256(f1) || sha256(f2) || ...     (zip stored order)
data                       := digest || sha256(verification.json) || b"<unix-time>"
signature                  := RSA_PKCS1v15_SHA256(private_key, sha256(data))
zip.comment                := {"signature": base64(sig), "time": unix_time}
.verification.dify.json    := last zip entry, content {"authorized_category": "community"}
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import zipfile
from typing import Dict, List, Optional

from . import util

VERIFICATION_FILE = ".verification.dify.json"
DEFAULT_CATEGORY = "community"


def _crypto():
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        return hashes, padding, rsa, serialization
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("cryptography package required for signing (pip install cryptography)") from e


def _prehashed():
    """Go signs the *pre-hashed* digest (sha256(data)) with rsa.SignPKCS1v15(crypto.SHA256, digest).

    cryptography must therefore be told the input is already a digest via Prehashed
    (asymmetric.utils), otherwise it would hash the 32-byte digest again.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import utils
    return utils.Prehashed(hashes.SHA256())


# ---------------------------------------------------------------- key management
def generate_keypair(out_prefix: str) -> Dict[str, str]:
    """Generate an RSA-4096 keypair; writes <prefix>.private.pem / <prefix>.public.pem (PKCS1)."""
    hashes, padding, rsa, serialization = _crypto()
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    priv = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,   # PKCS1 "RSA PRIVATE KEY"
        serialization.NoEncryption(),
    )
    pub = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.PKCS1,                 # "RSA PUBLIC KEY"
    )
    priv_path = f"{out_prefix}.private.pem"
    pub_path = f"{out_prefix}.public.pem"
    with open(priv_path, "wb") as f:
        f.write(priv)
    with open(pub_path, "wb") as f:
        f.write(pub)
    return {"private_key": priv_path, "public_key": pub_path}


def _load_private(path: str):
    hashes, padding, rsa, serialization = _crypto()
    with open(path, "rb") as f:
        data = f.read()
    if b"BEGIN RSA PRIVATE KEY" in data:
        return serialization.load_pem_private_key(data, password=None)
    return serialization.load_pem_private_key(data, password=None)


def _load_public(path: str):
    hashes, padding, rsa, serialization = _crypto()
    with open(path, "rb") as f:
        data = f.read()
    if b"BEGIN RSA PUBLIC KEY" in data:
        return serialization.load_pem_public_key(data)
    return serialization.load_pem_public_key(data)


# ---------------------------------------------------------------- digest helpers
def _entries(path_or_bytes) -> List[zipfile.ZipInfo]:
    if isinstance(path_or_bytes, bytes):
        with zipfile.ZipFile(io.BytesIO(path_or_bytes)) as z:
            return z.infolist(), z
    with zipfile.ZipFile(path_or_bytes) as z:
        return z.infolist(), z


def _file_digests(zf: zipfile.ZipFile) -> bytes:
    """concatenated sha256 of every entry content in stored order (dirs -> empty content)."""
    buf = b""
    for zi in zf.infolist():
        data = zf.read(zi.filename) if not zi.is_dir() else b""
        buf += hashlib.sha256(data).digest()
    return buf


def _read_comment(zf: zipfile.ZipFile) -> Dict:
    if not zf.comment:
        return {}
    try:
        obj = json.loads(zf.comment.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return obj if isinstance(obj, dict) else {}


def _verification_bytes(category: str) -> bytes:
    return json.dumps({"authorized_category": category}, separators=(",", ":")).encode("utf-8")


# ---------------------------------------------------------------- sign / verify
def sign_pkg(src: str, dst: str, private_key_path: str,
             category: str = DEFAULT_CATEGORY, time_now: Optional[int] = None) -> None:
    """Re-sign a difypkg with our own RSA-4096 key (compatible with the daemon)."""
    hashes, padding, rsa, serialization = _crypto()
    key = _load_private(private_key_path)
    time_now = time_now if time_now is not None else int(__import__("time").time())

    with zipfile.ZipFile(src, "r") as zin:
        infos = zin.infolist()
        files = []
        for zi in infos:
            files.append((zi.filename, zin.read(zi.filename) if not zi.is_dir() else b""))

    ver_json = _verification_bytes(category)
    # data digest: file shas in original order + sha(verification) + time
    data = b""
    for _name, content in files:
        data += hashlib.sha256(content).digest()
    data += hashlib.sha256(ver_json).digest()
    data += str(time_now).encode("ascii")

    digest = hashlib.sha256(data).digest()
    sig = key.sign(digest, padding.PKCS1v15(), _prehashed())

    comment = json.dumps({"signature": base64.b64encode(sig).decode("ascii"), "time": time_now},
                         separators=(",", ":")).encode("utf-8")

    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zout:
        for name, content in files:
            if name == VERIFICATION_FILE:
                continue  # drop stale verification file from source pkg
            zout.writestr(name, content)
        zout.writestr(VERIFICATION_FILE, ver_json)
        zout.comment = comment


def verify_pkg(pkg: str, public_key_path: Optional[str] = None) -> bool:
    """Verify a difypkg signature against a public key (or official-style single key)."""
    hashes, padding, rsa, serialization = _crypto()
    pub = _load_public(public_key_path) if public_key_path else None
    with zipfile.ZipFile(pkg, "r") as zf:
        comment = _read_comment(zf)
        sig_b64 = comment.get("signature")
        t = comment.get("time")
        if not sig_b64 or t is None:
            return False
        data = _file_digests(zf)          # includes .verification.dify.json if last entry present
        data += str(int(t)).encode("ascii")
    digest = hashlib.sha256(data).digest()
    sig = base64.b64decode(sig_b64)
    try:
        pub.verify(sig, digest, padding.PKCS1v15(), _prehashed())
        return True
    except Exception:  # noqa: BLE001
        return False


def verify_pkg_any(pkg: str, public_key_paths: List[str]) -> bool:
    return any(verify_pkg(pkg, k) for k in public_key_paths)
