"""Table of annotated points per class and per crop.

Each CSV in ANNOTATIONS_DIR is one crop (named <patch_id>.csv) with a
``class`` column. Missing classes (NaN) are counted as "unlabeled".
If a manifest.csv is given, crops without an annotation file are listed
with zero points.

Usage::

    uv run count_annotations.py data/annotations [--manifest data/manifest.csv] [--csv out.csv]
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("annotations_dir", type=Path)
    parser.add_argument("--manifest", type=Path, help="manifest.csv, to also list crops with no annotation file")
    parser.add_argument("--csv", type=Path, help="also save the table to this CSV file")
    args = parser.parse_args()

    frames = []
    for path in sorted(args.annotations_dir.glob("*.csv")):
        df = pd.read_csv(path)
        label = df["class"].fillna(-1).astype(int) if "class" in df else pd.Series(-1, index=df.index)
        frames.append(pd.DataFrame({"crop": path.stem, "class": label}))

    if not frames:
        raise SystemExit(f"no CSV files found in {args.annotations_dir}")

    counts = (
        pd.concat(frames)
        .groupby(["crop", "class"]).size()
        .unstack(fill_value=0)
        .rename(columns=lambda c: "unlabeled" if c == -1 else f"class {c}")
    )

    if args.manifest:
        expected = pd.read_csv(args.manifest)["patch_id"]
        counts = counts.reindex(counts.index.union(expected), fill_value=0)
        extra = counts.index.difference(expected)
        if len(extra):
            print(f"warning: annotations without a crop in the manifest: {list(extra)}\n")

    counts["total"] = counts.sum(axis=1)
    counts.loc["TOTAL"] = counts.sum()

    print(counts.to_string())
    if args.csv:
        counts.to_csv(args.csv)
        print(f"\nsaved to {args.csv}")


if __name__ == "__main__":
    main()