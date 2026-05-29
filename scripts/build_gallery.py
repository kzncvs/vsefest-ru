#!/usr/bin/env python3
"""Build the festival photo gallery: resize previews, upload to S3, write manifest.

Workflow
--------
1. Put each photographer's originals under  incoming/<slug>/  (any names/order;
    a symlink to the source folder works — incoming/ is gitignored).
2. Edit the CONFIG block below: bucket + photographer names/slugs/socials.
3. Resize previews + (re)generate assets/web/photos.json:
       python scripts/build_gallery.py
4. When the manifest looks right, upload both tiers to Yandex Object Storage:
       python scripts/build_gallery.py --upload

The git repo only ever stores assets/web/photos.json — image bytes live in S3.
See docs/PHOTO_GALLERY.md for the full picture (bucket setup, etc.).

Requirements: macOS `sips` (built in) for resizing; `aws` CLI for --upload
(`brew install awscli`, configured with a Yandex static access key as a profile).
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ─── CONFIG ──────────────────────────────────────────────────────────────────
REPO = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO / "incoming"                 # incoming/<slug>/<any images>
BUILD_DIR = REPO / "build" / "previews"      # staged resized previews (gitignored)
THUMB_DIR = REPO / "build" / "thumbs"        # staged grid thumbnails (gitignored)
MANIFEST = REPO / "assets" / "web" / "photos.json"

# S3 (Yandex Object Storage). PUBLIC_BASE is what ends up in the manifest.
BUCKET = "fest-vse-photos"
ENDPOINT = "https://storage.yandexcloud.net"
PUBLIC_BASE = f"https://{BUCKET}.storage.yandexcloud.net"
PREFIX = "photos"                            # key prefix inside the bucket
PROFILE = "festvse"                          # aws CLI profile (Yandex static key)
PUBLIC_ACL = False                           # bucket is already public-read at bucket level

# Edit names/slugs/socials here. The slug must match the incoming/<slug> folder.
# `socials` is a list — a photographer can have several (tg / vk / instagram …).
PHOTOGRAPHERS = [
    {"slug": "arsenka", "name": "Арсенка",
     "socials": [{"label": "@papin_olimpus", "url": "https://t.me/papin_olimpus"}]},
    {"slug": "sanya", "name": "Саня",
     "socials": [{"label": "@m69300420", "url": "https://t.me/m69300420"}]},
    {"slug": "renat", "name": "Ренат",
     "socials": [{"label": "@renato.bliss", "url": "https://www.instagram.com/renato.bliss"},
                 {"label": "VK", "url": "https://vk.com/renat_tukmakov"}]},
    {"slug": "raul", "name": "Рауль",
     "socials": [{"label": "@rmrodrigez", "url": "https://instagram.com/rmrodrigez"},
                 {"label": "@muchacho_rodrigez", "url": "https://t.me/muchacho_rodrigez"}]},
]

PREVIEW_MAX = 2000        # px, longest edge of the slider preview (lightbox)
PREVIEW_QUALITY = 82      # JPEG quality 0-100
THUMB_MAX = 640           # px, longest edge of the grid thumbnail
THUMB_QUALITY = 72        # JPEG quality for thumbnails
WORKERS = 8               # parallel resize/upload workers
IMG_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".webp"}
# ──────────────────────────────────────────────────────────────────────────────


def sh(cmd):
    """Run a command; raise RuntimeError on failure (propagates out of threads)."""
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(map(str, cmd))}\n{res.stderr.strip()}")
    return res.stdout


def list_sources(slug):
    """Image files for a photographer, plain-name sorted (this fixes each file's
    storage key NN, so it stays stable across runs / matches what's already in S3)."""
    folder = SRC_ROOT / slug
    if not folder.is_dir():
        return []
    files = [p for p in folder.iterdir()
             if p.is_file() and p.suffix.lower() in IMG_EXTS]
    return sorted(files, key=lambda p: p.name.lower())


def natkey(path):
    """Natural sort key (Finder-like): '1-2' before '1-10'. Used for DISPLAY order."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', path.name)]


def resize(src, dst, maxpx, quality):
    """Downscale to `maxpx` longest edge, re-encode JPEG (sips). Skips if already fresh."""
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        return  # already up to date — lets re-runs skip work
    dst.parent.mkdir(parents=True, exist_ok=True)
    sh(["sips", "-s", "format", "jpeg",
        "-Z", str(maxpx),
        "-s", "formatOptions", str(quality),
        str(src), "--out", str(dst)])


def s3_cp(local, key, content_disposition=None):
    cmd = ["aws", "s3", "cp", str(local), f"s3://{BUCKET}/{key}",
           "--endpoint-url", ENDPOINT, "--profile", PROFILE,
           "--content-type", "image/jpeg"]
    if PUBLIC_ACL:
        cmd += ["--acl", "public-read"]
    if content_disposition:
        cmd += ["--content-disposition", content_disposition]
    sh(cmd)


def process(task, do_upload, thumbs_only=False):
    """Resize the tiers and (if do_upload) push them. thumbs_only touches only the
    grid thumbnail — used to add the grid without re-uploading previews/originals."""
    resize(task["src"], task["thumb_path"], THUMB_MAX, THUMB_QUALITY)
    if not thumbs_only:
        resize(task["src"], task["preview_path"], PREVIEW_MAX, PREVIEW_QUALITY)
    if do_upload:
        s3_cp(task["thumb_path"], task["thumb_key"])
        if not thumbs_only:
            s3_cp(task["preview_path"], task["preview_key"])
            s3_cp(task["src"], task["original_key"],
                  content_disposition=f'attachment; filename="{task["dl_name"]}"')


def main():
    ap = argparse.ArgumentParser(description="Build the photo gallery manifest + assets.")
    ap.add_argument("--upload", action="store_true",
                    help="upload tiers to S3 (needs the aws CLI)")
    ap.add_argument("--thumbs-only", action="store_true",
                    help="only (re)build + upload grid thumbnails; leave previews/originals untouched")
    ap.add_argument("--only", metavar="SLUG",
                    help="build/upload only this photographer (manifest still includes everyone)")
    args = ap.parse_args()

    if BUCKET == "CHANGE-ME":
        sys.exit("Set BUCKET / PUBLIC_BASE in the CONFIG block first.")
    if not shutil.which("sips"):
        sys.exit("`sips` not found — this script needs macOS for resizing.")
    if args.upload and not shutil.which("aws"):
        sys.exit("--upload needs the aws CLI: brew install awscli")

    # 1. Build the task list + manifest skeleton (sequential → deterministic order).
    manifest = {"baseUrl": PUBLIC_BASE, "photographers": []}
    tasks = []
    for p in PHOTOGRAPHERS:
        slug = p["slug"]
        sources = list_sources(slug)
        if not sources:
            print(f"  · {slug}: no files in incoming/{slug}/ — skipping")
            continue
        width = max(2, len(str(len(sources))))  # 01.. or 001.. depending on count
        # Storage key (NN) is fixed by plain-name order (= what's already in S3),
        # so re-runs never reshuffle uploaded files. Each source keeps its key pair.
        keys = {}
        for i, src in enumerate(sources, 1):
            nn = f"{i:0{width}d}"
            ext = src.suffix.lower()
            keys[src] = {
                "thumb_key": f"{PREFIX}/{slug}/thumb/{nn}.jpg",
                "preview_key": f"{PREFIX}/{slug}/{nn}.jpg",
                "original_key": f"{PREFIX}/{slug}/orig/{nn}{ext}",
                "thumb_path": THUMB_DIR / slug / f"{nn}.jpg",
                "preview_path": BUILD_DIR / slug / f"{nn}.jpg",
                "dl_name": f"festvse_{slug}_{nn}{ext}",
            }
            if not args.only or args.only == slug:
                tasks.append({"src": src, **keys[src]})
        # Display order in the slider/grid = natural sort (Finder-like), independent of keys.
        photos = [{"thumb": keys[s]["thumb_key"], "preview": keys[s]["preview_key"],
                   "original": keys[s]["original_key"]}
                  for s in sorted(sources, key=natkey)]
        manifest["photographers"].append({
            "slug": slug, "name": p["name"],
            "socials": p.get("socials") or ([p["social"]] if p.get("social") else []),
            "photos": photos,
        })
        print(f"  · {slug}: {len(photos)} photos")

    if not tasks:
        sys.exit("No photos found. Put images under incoming/<slug>/ first.")

    # 2. Resize (+ upload) in parallel.
    print(f"\n{'resize + upload' if args.upload else 'resize'}: "
          f"{len(tasks)} photos, {WORKERS} workers…")
    done = 0
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(process, t, args.upload, args.thumbs_only) for t in tasks]
        for f in as_completed(futs):
            f.result()  # re-raise the first worker error, if any
            with lock:
                done += 1
                if done % 25 == 0 or done == len(tasks):
                    print(f"    …{done}/{len(tasks)}", flush=True)

    # 3. Write manifest.
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    total = sum(len(p["photos"]) for p in manifest["photographers"])
    print(f"\nWrote {MANIFEST.relative_to(REPO)} — {total} photos, "
          f"{len(manifest['photographers'])} photographers.")
    if not args.upload:
        print("Previews staged in build/previews/. Re-run with --upload to push to S3.")


if __name__ == "__main__":
    main()
