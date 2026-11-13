"""
Download the three arXiv papers at the exact versions the brief
asks for, hash each PDF, and save data/manifest.json.

Safe to re-run. If the PDF already sits on disk, we skip the
download and just update the hash in the manifest.
"""
from __future__ import annotations
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from relay.config import PAPERS, DATA_PDFS, DATA_TEXT, MANIFEST


ARXIV_PDF = "https://arxiv.org/pdf/{id}{ver}"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def download(url: str, dest: Path) -> None:
    # arXiv asks clients to be identifiable. The user's email is
    # intentionally not used here; a generic UA keeps arXiv happy and
    # does not leak personal data.
    headers = {"User-Agent": "relay-assignment/0.1 (+student project)"}
    with httpx.stream("GET", url, headers=headers, timeout=60.0, follow_redirects=True) as r:
        r.raise_for_status()
        with dest.open("wb") as out:
            for chunk in r.iter_bytes(1 << 15):
                out.write(chunk)


def main() -> int:
    DATA_PDFS.mkdir(parents=True, exist_ok=True)
    DATA_TEXT.mkdir(parents=True, exist_ok=True)

    manifest: dict = {
        "downloaded_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "papers": {},
    }

    for key, meta in PAPERS.items():
        url = ARXIV_PDF.format(id=meta["arxiv_id"], ver=meta["version"])
        dest = DATA_PDFS / f"{key}.pdf"

        if dest.exists():
            print(f"[skip] {key}: {dest} already exists")
        else:
            print(f"[get ] {key}: {url}")
            try:
                download(url, dest)
            except httpx.HTTPError as e:
                print(f"[fail] {key}: {e}", file=sys.stderr)
                return 1
            # be polite to arXiv between downloads
            time.sleep(3)

        manifest["papers"][key] = {
            "arxiv_id": meta["arxiv_id"],
            "version": meta["version"],
            "title": meta["title"],
            "url": url,
            "pdf_path": str(dest.relative_to(DATA_PDFS.parent.parent)),
            "sha256": sha256(dest),
            "bytes": dest.stat().st_size,
        }

    MANIFEST.write_text(json.dumps(manifest, indent=2))
    print(f"[ok  ] wrote {MANIFEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
