import argparse
import itertools
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urlparse

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


def resolve_source(source):
    """Classify a source string as local or remote.

    Returns (location, is_local). Local sources (plain paths, ~/paths,
    file:// URIs) are returned as absolute filesystem paths; anything else
    with a scheme (s3://, https://, ...) is returned unchanged.
    """
    source = str(source)
    if source.startswith("file://"):
        path = unquote(urlparse(source).path)
        # file:///C:/data/x.zarr -> C:/data/x.zarr on Windows
        if len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return str(Path(path).expanduser().resolve()), True
    if "://" in source:
        return source, False
    return str(Path(source).expanduser().resolve()), True


def _open_group(location, storage_options):
    """Open a Zarr group (local path or remote URL).

    OME-Zarr 0.4 is Zarr v2: read only .zgroup/.zattrs and skip .zmetadata
    (avoids probing for files a remote server may 403 on).
    If that fails (e.g. a Zarr v3 / OME-Zarr 0.5 store), fall back to v3.
    """
    try:
        return zarr.open_group(
            location,
            mode="r",
            zarr_format=2,
            use_consolidated=False,
            storage_options=storage_options,
        )
    except Exception as v2_err:
        try:
            return zarr.open_group(
                location,
                mode="r",
                zarr_format=3,
                storage_options=storage_options,
            )
        except Exception:
            raise v2_err  # report the v2 error, it's usually the informative one


def open_array(source, level=0, workers=32, anon=True):
    """Open one resolution level of an OME-Zarr (local or remote).

    Returns (array, axes).
    """
    location, is_local = resolve_source(source)

    storage_options = None
    if is_local:
        if not Path(location).exists():
            raise FileNotFoundError(f"Local Zarr path does not exist: {location}")
    elif location.startswith("s3://"):
        storage_options = {
            "anon": anon,
            "config_kwargs": {"max_pool_connections": max(64, workers * 2)},
        }

    root = _open_group(location, storage_options)
    ms = get_multiscales(root.attrs)
    if ms is None:
        # bioformats2raw layout: the image lives in the "0" subgroup
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
                      desc="  Reading chunks", unit="chunk"):
            f.result()

    # --- Make the array ImageJ/Fiji compatible ---
    # ImageJ hyperstacks require axis order TZCYX (S for RGB samples, not used here).
    ij_order = ["t", "z", "c", "y", "x"]

    # Normalize axis names to lowercase and check we know them all
    axes_l = [a.lower() for a in axes]
    unknown = [a for a in axes_l if a not in ij_order]
    if unknown:
        raise ValueError(f"Unsupported axes for ImageJ export: {unknown}")

    # Add any missing axes as singletons so we always have TZCYX
    for a in ij_order:
        if a not in axes_l:
            patch = np.expand_dims(patch, axis=0)
            axes_l.insert(0, a)

    # Reorder to TZCYX
    perm = [axes_l.index(a) for a in ij_order]
    patch = np.transpose(patch, perm)  # shape: (T, Z, C, Y, X)

    Path(output).parent.mkdir(parents=True, exist_ok=True)
    tifffile.imwrite(
        output,
        patch,
        imagej=True,
        metadata={"axes": "TZCYX"},
        compression="zlib",
    )
    print(f"  Saved: {output}  shape (T,Z,C,Y,X): {patch.shape}")


def source_name(source):
    """Short name for output filenames, from a local path or a URL."""
    location, is_local = resolve_source(source)
    if not is_local:
        location = location.rstrip("/")
    name = Path(location).name
    for suffix in (".ome.zarr", ".zarr"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def run_config(config_path, output_dir, level, workers, anon, skip_existing):
    with open(config_path) as f:
        config = yaml.safe_load(f)

    jobs = []  # (source, level, patch_size, positions, section)
    for section, entries in config.items():
        for entry in entries:
            # "url" may be a remote URL or a local path; "path" is an alias
            source = entry.get("url") or entry.get("path")
            if source is None:
                raise ValueError(
                    f"Entry in section '{section}' needs a 'url' or 'path' key"
                )
            jobs.append((
                source,
                entry.get("level", level),
                entry["patch-size"],
                entry["positions"],
                section,
            ))

    for source, lvl, patch_size, positions, section in jobs:
        print(f"\n=== [{section}] {source} (level {lvl}) ===")
        try:
            arr, axes = open_array(source, lvl, workers, anon)
        except FileNotFoundError as e:
            print(f"  ERROR: {e}")
            continue
        print(f"  Image shape: {arr.shape} ({axes}) dtype={arr.dtype} chunks={arr.chunks}")

        pz, py, px = patch_size
        for z, y, x in positions:
            name = f"{source_name(source)}_L{lvl}_z{z}_y{y}_x{x}.tiff"
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
        description="Extract patches from an OME-Zarr (remote or local), "
                    "via CLI or YAML config."
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--config", help="YAML file describing batch extraction")
    src.add_argument("--url", "--path", dest="url",
                     help="Single-patch mode: Zarr URL (s3://, https://) "
                          "or local path (/data/img.zarr, ~/img.zarr, file://...)")

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
        parser.error("--position is required with --url/--path")

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