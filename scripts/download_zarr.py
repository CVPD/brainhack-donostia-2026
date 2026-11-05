#!/usr/bin/env python3
"""
Download a complete remote Zarr / OME-Zarr store as fast as possible.

Strategy:
  * List every object under the store prefix once (with sizes).
  * Copy the raw bytes of each object (no Zarr decoding, no decompression).
  * Run many concurrent async requests via a fixed pool of workers.
  * Write to "<file>.part" and rename, so interrupted runs never leave
    half-written files; re-running resumes by skipping files whose size matches.

Examples:
  python download_zarr.py --url s3://bucket/path/image.zarr --output ./image.zarr
  python download_zarr.py --url s3://bucket/path/image.zarr --include "0/*" --include "*.json" --include ".z*"
  python download_zarr.py --url gs://bucket/image.zarr --workers 256
"""

import argparse
import asyncio
import os
import sys
import time
from fnmatch import fnmatch
from pathlib import Path

import fsspec
from tqdm import tqdm


def format_size(nbytes):
    size = float(nbytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def url_name(url):
    return Path(url.rstrip("/")).name or "store.zarr"


def make_fs(url, workers, anon, endpoint_url=None):
    """Create an async fsspec filesystem for the URL."""
    protocol = url.split("://", 1)[0] if "://" in url else "file"
    opts = {}
    if protocol in ("s3", "s3a"):
        opts = {
            "anon": anon,
            "config_kwargs": {
                "max_pool_connections": max(64, workers * 2),
                "retries": {"max_attempts": 3},
            },
        }
        if endpoint_url:
            opts["client_kwargs"] = {"endpoint_url": endpoint_url}
    elif protocol in ("gs", "gcs"):
        opts = {"token": "anon"} if anon else {}
    elif protocol in ("http", "https"):
        opts = {"client_kwargs": {"timeout": None}}
    fs, root = fsspec.core.url_to_fs(url, asynchronous=True, **opts)
    if not getattr(fs, "async_impl", False):
        sys.exit(f"Filesystem for '{protocol}' has no async support; cannot use fast mode.")
    return fs, root.rstrip("/")


async def list_store(fs, root, includes, excludes):
    """List all files under root. Returns list of (relative_path, size)."""
    print(f"Listing objects under {root} ...", flush=True)
    t0 = time.time()
    try:
        listing = await fs._find(root, detail=True)
    except Exception as e:
        sys.exit(
            f"Could not list the store ({e}).\n"
            "Plain HTTP servers usually don't support listing; use an S3/GCS URL."
        )

    files = []
    prefix = root + "/"
    for key, info in listing.items():
        if info.get("type") not in (None, "file"):
            continue
        rel = key[len(prefix):] if key.startswith(prefix) else key.split("/", 1)[-1]
        if includes and not any(fnmatch(rel, p) for p in includes):
            continue
        if excludes and any(fnmatch(rel, p) for p in excludes):
            continue
        files.append((rel, int(info.get("size") or 0)))

    print(f"Found {len(files):,} files, {format_size(sum(s for _, s in files))} "
          f"(listed in {time.time() - t0:.1f}s)", flush=True)
    return files


def write_file(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


async def download_all(fs, root, files, out_dir, workers, retries, skip_existing):
    total_bytes = sum(s for _, s in files)
    queue: asyncio.Queue = asyncio.Queue(maxsize=workers * 4)
    failures = []
    stats = {"done": 0, "skipped": 0}

    bar = tqdm(total=total_bytes, unit="B", unit_scale=True, unit_divisor=1024,
               desc="Downloading", smoothing=0.1, mininterval=0.25)

    def refresh_postfix():
        bar.set_postfix(files=f"{stats['done'] + stats['skipped']:,}/{len(files):,}",
                        refresh=False)

    async def worker():
        while True:
            item = await queue.get()
            if item is None:
                queue.task_done()
                return
            rel, size = item
            dest = out_dir / rel
            try:
                if skip_existing and dest.exists() and dest.stat().st_size == size:
                    stats["skipped"] += 1
                    bar.update(size)
                    refresh_postfix()
                    continue

                last_err = None
                for attempt in range(retries + 1):
                    try:
                        data = await fs._cat_file(f"{root}/{rel}")
                        await asyncio.to_thread(write_file, dest, data)
                        stats["done"] += 1
                        bar.update(len(data))
                        refresh_postfix()
                        last_err = None
                        break
                    except FileNotFoundError as e:
                        last_err = e
                        break  # no point retrying
                    except Exception as e:
                        last_err = e
                        await asyncio.sleep(min(30, 0.5 * 2 ** attempt))
                if last_err is not None:
                    failures.append((rel, repr(last_err)))
            finally:
                queue.task_done()

    tasks = [asyncio.create_task(worker()) for _ in range(workers)]
    # Largest files first keeps workers busy at the tail end.
    for item in sorted(files, key=lambda x: -x[1]):
        await queue.put(item)
    for _ in tasks:
        await queue.put(None)
    await asyncio.gather(*tasks)
    bar.close()
    return stats, failures


async def amain(args):
    fs, root = make_fs(args.url, args.workers, anon=not args.no_anon,
                   endpoint_url=args.endpoint_url)
    if hasattr(fs, "set_session"):
        await fs.set_session()  # s3fs needs an explicit session in async mode

    try:
        files = await list_store(fs, root, args.include, args.exclude)
        if not files:
            sys.exit("No files matched. Nothing to download.")

        out_dir = Path(args.output or url_name(args.url))
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"Output: {out_dir.resolve()} | workers: {args.workers}", flush=True)

        t0 = time.time()
        stats, failures = await download_all(
            fs, root, files, out_dir, args.workers, args.retries, args.skip_existing
        )
        elapsed = time.time() - t0
    finally:
        # Close the underlying session cleanly if possible
        close = getattr(fs, "_session", None)
        if close is not None and hasattr(close, "close"):
            try:
                await close.close()
            except Exception:
                pass

    total = sum(s for _, s in files)
    print(f"\nDone in {elapsed:.1f}s | downloaded {stats['done']:,} files, "
          f"skipped {stats['skipped']:,} | avg {format_size(total / max(elapsed, 1e-9))}/s")
    if failures:
        print(f"\n{len(failures)} files FAILED (re-run to retry them):")
        for rel, err in failures[:20]:
            print(f"  {rel}: {err}")
        if len(failures) > 20:
            print(f"  ... and {len(failures) - 20} more")
        sys.exit(1)


def main():
    p = argparse.ArgumentParser(description="Download a complete remote Zarr store (raw copy).")
    p.add_argument("--url", required=True, help="Zarr URL, e.g. s3://bucket/path/image.zarr")
    p.add_argument("--output", help="Output directory (default: name of the zarr in the URL)")
    p.add_argument("--workers", type=int, default=16,
                   help="Concurrent requests (default 128; try 256+ on fast links)")
    p.add_argument("--retries", type=int, default=5, help="Retries per file (default 5)")
    p.add_argument("--no-anon", action="store_true",
                   help="Use credentials instead of anonymous access")
    p.add_argument("--no-skip-existing", dest="skip_existing", action="store_false",
                   help="Re-download files even if present with the same size")
    p.add_argument("--include", action="append", default=[],
                   help="Only download relative paths matching this glob (repeatable). "
                        "E.g. --include '0/*' --include '*.json' --include '.z*'")
    p.add_argument("--exclude", action="append", default=[],
                   help="Skip relative paths matching this glob (repeatable)")
    p.add_argument("--endpoint-url",
               help="Custom S3-compatible endpoint, e.g. https://livingobjects.ebi.ac.uk")
    args = p.parse_args()

    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run the same command to resume.")
        sys.exit(130)


if __name__ == "__main__":
    main()