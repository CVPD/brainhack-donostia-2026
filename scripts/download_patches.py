import argparse
import itertools
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import tifffile
import yaml
import zarr
from tqdm import tqdm


def format_size(nbytes):
    size = float(nbytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def get_multiscales(attrs):
    # OME-Zarr 0.4 (attrs["multiscales"]) vs 0.5 (attrs["ome"]["multiscales"])
    if "multiscales" in attrs:
        return attrs["multiscales"][0]
    if "ome" in attrs and "multiscales" in attrs["ome"]:
        return attrs["ome"]["multiscales"][0]
    return None


def open_array(url, level=0, workers=32, anon=True):
    """Open one resolution level of a remote OME-Zarr. Returns (array, axes)."""
    storage_options = None
    if url.startswith("s3://"):
        storage_options = {
            "anon": anon,
            "config_kwargs": {"max_pool_connections": max(64, workers * 2)},
        }

    root = zarr.open_group(url, mode="r", storage_options=storage_options)
    ms = get_multiscales(root.attrs)
    if ms is None:
        root = root["0"]
        ms = get_multiscales(root.attrs)

    axes = [a["name"] for a in ms["axes"]]
    arr = root[ms["datasets"][level]["path"]]
    return arr, axes


def download_patch(arr, axes, position, output, workers=32, verbose=True):
    """position = (zmin, ymin, xmin, zmax, ymax, xmax)"""
    zmin, ymin, xmin, zmax, ymax, xmax = position
    bounds = {"z": (zmin, zmax), "y": (ymin, ymax), "x": (xmin, xmax)}

    ranges = [(0, n) for n in arr.shape]
    for axis, (start, end) in bounds.items():
        i = axes.index(axis)
        if start < 0 or end > arr.shape[i] or start >= end:
            raise ValueError(
                f"Invalid {axis} coordinates: {start}:{end} (size {arr.shape[i]})"
            )
        ranges[i] = (start, end)

    patch_shape = tuple(e - s for s, e in ranges)
    image_size = math.prod(arr.shape) * arr.dtype.itemsize
    patch_size = math.prod(patch_shape) * arr.dtype.itemsize

    if verbose:
        print(f"  Patch shape: {patch_shape} | size: {format_size(patch_size)} "
              f"({100 * patch_size / image_size:.4f}% of image)")

    chunk_ranges = [
        range(s // c, (e - 1) // c + 1) for (s, e), c in zip(ranges, arr.chunks)
    ]
    total_chunks = math.prod(len(r) for r in chunk_ranges)
    patch = np.empty(patch_shape, dtype=arr.dtype)

    def fetch_chunk(idx):
        src, dst = [], []
        for i, c, (s, e) in zip(idx, arr.chunks, ranges):
            start, end = max(i * c, s), min((i + 1) * c, e)
            src.append(slice(start, end))
            dst.append(slice(start - s, end - s))
        patch[tuple(dst)] = arr[tuple(src)]  # disjoint regions, no lock needed

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(fetch_chunk, idx)
                   for idx in itertools.product(*chunk_ranges)]
        for f in tqdm(as_completed(futures), total=total_chunks,
                      desc="  Downloading chunks", unit="chunk"):
            f.result()

    # Drop singleton non-spatial axes (t, c) so the TIFF is (Z, Y, X)
    keep_axes = list(axes)
    for i in reversed(range(len(axes))):
        if axes[i] not in ("z", "y", "x") and patch.shape[i] == 1:
            patch = np.squeeze(patch, axis=i)
            keep_axes.pop(i)

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(output, patch, compression="zlib")
    print(f"  Saved: {output}  shape ({', '.join(keep_axes)}): {patch.shape}")


def url_name(url):
    return Path(url.rstrip("/")).name.replace(".zarr", "")


def run_config(config_path, output_dir, level, workers, anon, skip_existing):
    with open(config_path) as f:
        config = yaml.safe_load(f)

    jobs = []  # (url, level, patch_size, positions)
    for section, entries in config.items():
        for entry in entries:
            jobs.append((
                entry["url"],
                entry.get("level", level),
                entry["patch-size"],
                entry["positions"],
                section,
            ))

    for url, lvl, patch_size, positions, section in jobs:
        print(f"\n=== [{section}] {url} (level {lvl}) ===")
        arr, axes = open_array(url, lvl, workers, anon)
        print(f"  Image shape: {arr.shape} ({axes}) dtype={arr.dtype} chunks={arr.chunks}")

        pz, py, px = patch_size
        for z, y, x in positions:
            name = f"{url_name(url)}_L{lvl}_z{z}_y{y}_x{x}.tiff"
            output = Path(output_dir) / section / name
            if skip_existing and output.exists():
                print(f"  Skipping existing: {output}")
                continue
            try:
                download_patch(arr, axes, (z, y, x, z + pz, y + py, x + px),
                               output, workers)
            except ValueError as e:
                print(f"  ERROR at position {[z, y, x]}: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Download patches from remote OME-Zarr, via CLI or YAML config."
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--config", help="YAML file describing batch downloads")
    src.add_argument("--url", help="Single-patch mode: Zarr URL")

    parser.add_argument("--position", nargs=6, type=int,
                        metavar=("ZMIN", "YMIN", "XMIN", "ZMAX", "YMAX", "XMAX"),
                        help="Single-patch mode: min and max corner (z y x z y x)")
    parser.add_argument("--output", default="patch.tiff",
                        help="Single-patch mode: output file")
    parser.add_argument("--output-dir", default="patches",
                        help="Config mode: output directory")
    parser.add_argument("--level", type=int, default=0,
                        help="Resolution level (default for config entries)")
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--no-anon", action="store_true",
                        help="S3 only: use AWS credentials instead of anonymous access")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Config mode: skip patches whose output file exists")
    args = parser.parse_args()

    if args.url and not args.position:
        parser.error("--position is required with --url")

    zarr.config.set({"async.concurrency": max(64, args.workers * 2)})
    anon = not args.no_anon

    if args.config:
        run_config(args.config, args.output_dir, args.level,
                   args.workers, anon, args.skip_existing)
    else:
        arr, axes = open_array(args.url, args.level, args.workers, anon)
        print(f"Image shape: {arr.shape} ({axes}) dtype={arr.dtype} chunks={arr.chunks}")
        download_patch(arr, axes, args.position, args.output, args.workers)


if __name__ == "__main__":
    main()