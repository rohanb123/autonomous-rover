"""Shared download helper for ml/download/*: resumable HTTP GET with size and hash checks."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import httpx

CHUNK = 1 << 20
TIMEOUT = httpx.Timeout(30.0, read=120.0)


def file_hash(path: Path, algo: str) -> str:
    h = hashlib.new(algo)
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def fetch(
    client: httpx.Client,
    url: str,
    dest: Path,
    size: int | None = None,
    checksum: str | None = None,
    retries: int = 5,
) -> Path:
    """Download url -> dest (resuming dest.part). checksum is "md5:<hex>" or "sha256:<hex>".

    Skips the download if dest already exists with the right size (and checksum, if given).
    Deletes a file whose checksum doesn't match and raises.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    algo, _, digest = (checksum or "").partition(":")
    if dest.exists() and (size is None or dest.stat().st_size == size):
        if not checksum or file_hash(dest, algo) == digest:
            print(f"  ok (already here) {dest.name}", flush=True)
            return dest
    part = dest.with_name(dest.name + ".part")
    for attempt in range(retries):
        have = part.stat().st_size if part.exists() else 0
        if size is not None and have > size:
            part.unlink()
            have = 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 200 and have:
                    have = 0  # server ignored Range: start over
                elif resp.status_code not in (200, 206):
                    resp.raise_for_status()
                with part.open("ab" if have else "wb") as fh:
                    for chunk in resp.iter_bytes(CHUNK):
                        fh.write(chunk)
            break
        except (httpx.HTTPError, OSError) as e:
            print(f"    retry {attempt + 1}/{retries} after error: {e}", flush=True)
            time.sleep(2 * (attempt + 1))
    else:
        raise SystemExit(f"giving up on {url}")
    got = part.stat().st_size
    if size is not None and got != size:
        raise SystemExit(f"size mismatch for {dest.name}: {got} != {size}")
    if checksum and file_hash(part, algo) != digest:
        part.unlink()
        raise SystemExit(f"{algo} mismatch for {dest.name} (deleted; rerun to retry)")
    part.replace(dest)
    note = f", {algo} verified" if checksum else ""
    print(f"  ok {dest.name} ({got / 1e6:.1f} MB{note})", flush=True)
    return dest
