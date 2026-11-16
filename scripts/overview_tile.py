"""Visualise OME-Zarr patches and their annotations at their original position.

The patches live in one OME-Zarr store, one image group per patch
(``<store>/<section>/<patch_id>/0``), as written by download_patches.py.
Position and voxel size come from each patch's NGFF ``multiscales`` metadata.
The original image is streamed lazily from the ``source`` recorded in each
patch's ``patch_provenance`` attribute; use --no-zarr to skip it.

Annotation CSVs live in the annotation directory, are named
``<patch_id>.csv`` and have columns ``axis-0``/``axis-1``/``axis-2``
(z, y, x voxel indices, local to the patch) and ``class``.

Use --no-translate to stack all patches at the origin in a grid, which is
useful for checking that the patches are correct and complete.

Usage::

    uv run overview_tiles.py data/patches.ome.zarr data/annotations [--no-translate] [--no-zarr]
    uv run overview_tiles.py patches.ome.zarr.zip annotations
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from functools import cache
from pathlib import Path

os.environ.setdefault("NAPARI_ASYNC", "1")  # keep the UI responsive while chunks stream in

import dask.array as da
import napari
import numpy as np
import pandas as pd
import zarr

POINT_SIZE = 15
BRAINBOW_COLORS = ("red", "green", "blue")
CLASS_COLORS = {-1: "white", 0: "gray", 1: "red", 2: "lime", 3: "cyan", 4: "yellow"}
COORD_COLUMNS = ["axis-0", "axis-1", "axis-2"]
S3_STORAGE = {"anon": True}


@dataclass(frozen=True)
class Patch:
    name: str
    group: zarr.Group
    source: str | None
    scale: tuple[float, float, float]        # (z, y, x) physical size per voxel
    translation: tuple[float, float, float]  # (z, y, x) physical position of the patch origin


def get_multiscales(attrs) -> dict | None:
    """OME-Zarr 0.4 (attrs["multiscales"]) or 0.5 (attrs["ome"]["multiscales"])."""
    attrs = dict(attrs)
    ms = attrs.get("ome", attrs).get("multiscales")
    return ms[0] if ms else None


def spatial_transform(ms: dict, level: int = 0) -> tuple[tuple, tuple]:
    """Return (scale, translation) of the last three axes (z, y, x) for one level."""
    n = len(ms["axes"])
    scale, translation = [1.0] * n, [0.0] * n
    for t in ms["datasets"][level].get("coordinateTransformations", []):
        if t["type"] == "scale":
            scale = list(t["scale"])
        elif t["type"] == "translation":
            translation = list(t["translation"])
    return tuple(scale[-3:]), tuple(translation[-3:])


def open_store(location: str) -> zarr.Group:
    """Open a Zarr group from a local path, a .zip file, or a remote URL."""
    if location.endswith(".zip"):
        return zarr.open_group(zarr.storage.ZipStore(location, mode="r"), mode="r")
    opts = S3_STORAGE if location.startswith("s3://") else None
    try:
        return zarr.open_group(location, mode="r", storage_options=opts)
    except Exception:
        # OME-Zarr 0.4 is Zarr v2; skip .zmetadata probing on remote servers
        return zarr.open_group(location, mode="r", zarr_format=2,
                               use_consolidated=False, storage_options=opts)


def iter_patches(group: zarr.Group, depth: int = 2):
    """Yield every image group below *group* (store root -> section -> patch)."""
    for name, child in sorted(group.groups()):
        ms = get_multiscales(child.attrs)
        if ms is not None:
            scale, translation = spatial_transform(ms)
            source = dict(child.attrs).get("patch_provenance", {}).get("source")
            yield Patch(name, child, source, scale, translation)
        elif depth > 1:
            yield from iter_patches(child, depth - 1)


def _is_unit_scale(scale: tuple[float, float, float], tol: float = 1e-9) -> bool:
    return all(abs(s - 1.0) <= tol for s in scale)


@cache
def open_source(source: str, scale_hint: tuple[float, float, float] | None = None) -> tuple[list, tuple]:
    """Open the original OME-Zarr and return (levels (c, z, y, x), scale (z, y, x))."""
    group = open_store(source)
    ms = get_multiscales(group.attrs)
    if ms is None and "0" in group:
        group = group["0"]  # bioformats2raw layout
        ms = get_multiscales(group.attrs)
    if ms is None:
        sys.exit(f"no 'multiscales' metadata in {source}")

    axes = [a["name"] for a in ms["axes"]]
    t_idx = axes.index("t") if "t" in axes else None
    levels = []
    for dataset in ms["datasets"]:
        arr = da.from_zarr(group[dataset["path"]])
        if t_idx is not None:
            arr = arr[(slice(None),) * t_idx + (0,)]
        levels.append(arr)
    scale, _ = spatial_transform(ms)
    if scale_hint is not None and _is_unit_scale(scale):
        # Some bioformats2raw OME-Zarr stores keep scale in pixel units only.
        # Use the physical voxel size propagated to the extracted patches.
        scale = scale_hint
    return levels, scale


def add_source_image(viewer: napari.Viewer, source: str, scale_hint: tuple[float, float, float] | None = None) -> None:
    levels, scale = open_source(source, scale_hint)
    n = levels[0].shape[0]
    layers = viewer.add_image(
        levels,
        multiscale=True,
        channel_axis=0,  # (c, z, y, x)
        blending="additive",
        scale=scale,
        name=[f"source_c{i}" for i in range(n)],
    )
    for layer, color in zip(layers, BRAINBOW_COLORS):
        layer.colormap = color


def add_patch(viewer: napari.Viewer, patch: Patch, annotations_dir: Path, translate: bool) -> None:
    """Add one patch's channels (lazily) and, if present, its annotated points."""
    ms = get_multiscales(patch.group.attrs)
    axes = [a["name"] for a in ms["axes"]]
    arr = da.from_zarr(patch.group[ms["datasets"][0]["path"]])
    if "t" in axes:
        t_idx = axes.index("t")
        arr = arr[(slice(None),) * t_idx + (0,)]  # -> (c, z, y, x)

    offset = np.array(patch.translation) if translate else np.zeros(3)
    n_channels = arr.shape[0]
    layers = viewer.add_image(
        arr,
        channel_axis=0,
        blending="additive",
        scale=patch.scale,
        translate=offset,
        name=[f"{patch.name}_c{i}" for i in range(n_channels)],
    )
    for layer, color in zip(layers, BRAINBOW_COLORS):
        layer.colormap = color

    csv = annotations_dir / f"{patch.name}.csv"
    if not csv.exists():
        print(f"no annotation for {patch.name}")
        return

    df = pd.read_csv(csv)
    classes = df["class"].fillna(-1).astype(int).to_numpy()
    points = viewer.add_points(
        df[COORD_COLUMNS].to_numpy(),
        size=POINT_SIZE,
        name=f"cells_{patch.name}",
        features={"class": classes},  # kept for inspection, not used for colouring
        face_color=[CLASS_COLORS.get(c, "white") for c in classes],
    )
    points.translate = offset
    points.scale = patch.scale


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("patches_zarr", help="patches OME-Zarr store (or a section inside it, or a .zip)")
    parser.add_argument("annotations_dir", type=Path, help="directory with one CSV per patch")
    parser.add_argument("--no-translate", action="store_true", help="stack all patches at the origin")
    parser.add_argument("--no-zarr", action="store_true", help="do not show the original image")
    args = parser.parse_args()

    root = open_store(args.patches_zarr)
    patches = list(iter_patches(root)) or ([Patch("patch", root, None, *spatial_transform(get_multiscales(root.attrs)))]
                                           if get_multiscales(root.attrs) else [])
    if not patches:
        sys.exit(f"no OME-Zarr image groups found in {args.patches_zarr}")

    viewer = napari.Viewer()
    if not args.no_zarr:
        sources = sorted({p.source for p in patches if p.source})
        source_scale_hints: dict[str, tuple[float, float, float]] = {}
        grouped_scales: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
        for patch in patches:
            if patch.source:
                grouped_scales[patch.source].append(patch.scale)
        for source, scales in grouped_scales.items():
            source_scale_hints[source] = scales[0]
            if any(s != scales[0] for s in scales[1:]):
                print(f"warning: inconsistent patch scales for {source}, using first {scales[0]}")

        if not sources:
            print("no source recorded in the patches' provenance, skipping the original image")
        else:
            if len(sources) > 1:
                print(f"warning: several sources {sources}, showing the first only")
            add_source_image(viewer, sources[0], source_scale_hints.get(sources[0]))

    for patch in patches:
        add_patch(viewer, patch, args.annotations_dir, translate=not args.no_translate)

    viewer.dims.axis_labels = ("z", "y", "x")
    viewer.dims.ndisplay = 3
    viewer.scene.camera.center = (50, 100, 100)
    viewer.scene.camera.zoom = 0.8
    viewer.scene.camera.angles = (-50, 0, -70)
    if args.no_translate:
        viewer.canvas.grid.enabled = True
        viewer.canvas.grid.stride = 4

    napari.run()


if __name__ == "__main__":
    main()