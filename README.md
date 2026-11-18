This project is part of the  brainhack-donostia 2026 hackathon. For more information, please visit
https://brainhack-donostia.github.io/

## Brainhack project description

<img width="1996" height="702" alt="2026-11-02_15-49" src="https://github.com/user-attachments/assets/06080034-067d-47b5-a314-4ef0f1cda69a" />

**Brainbow** datasets enable researchers to distinguish individual cells and identify **lineage clusters**, providing valuable insights into brain development and cellular organization. Recent advances in microscopy, such as **ChroMS**, now make it possible to acquire **mesoscale images of entire brains**, opening new opportunities for **large-scale cellular analysis**.

Here we used the publicly available **Brainbow dataset** from the **IDR** (https://idr.openmicroscopy.org/study/idr0048/) to extract **brainbow labeled patches of a mouse cortex**.




## Instructions to download the patches

Download all the patches according to `data/datasets.yaml` to `data/patches.ome.zarr` using the following command:
```bash
uv run python scripts/download_patches.py --config data/datasets.yaml --output-dir data/ --skip-existing
```

It will create a `data/patches.ome.zarr` folder containing all the patches and a `data/manifest.csv` file with the metadata of each patch.

## Instructions to visualize the patches
To visualize the patches in a grid, you can use the following command:
```bash
uv run python scripts/overview_tile.py data/patches.ome.zarr data/annotations --no-translate --no-zarr
```

If you want to visualize the patches in 3D in the original coordinate system, you can use the following command:
```bash
uv run python scripts/overview_tile.py data/patches.ome.zarr/raw data/annotations
```

# Results

## Annotations

`data/annotations/` contains one CSV per patch, named after the patch
(e.g. `9846151_L0_z250_y500_x1000.csv` annotates the patch group
`9846151_L0_z250_y500_x1000`).

| column   | description |
|----------|-------------|
| `index`  | annotation index within the patch |
| `axis-0` | z position of the cell center, in voxels |
| `axis-1` | y position, in voxels |
| `axis-2` | x position, in voxels |
| `class`  | cell type (see below) |

Coordinates are voxel indices in (z, y, x) order, **local to the patch**
(origin at the patch corner), and can be loaded directly as napari points.
- Source-image voxels: add the patch start corner (`patch_provenance.bbox_pixels`
  in the patch metadata, or the `z/y/x` values in the patch name).
- Micrometers: multiply by the voxel size, 1.5 x 0.4 x 0.4 µm (z, y, x),
  as stored in the patch's NGFF `scale`.

| class | cell type |
|-------|-----------|
| 1 | Astrocyte |
| 2 | Pyramidal neuron |
| 3 | Unknown |

<img width="1996" alt="2026-11-02_15-49" src="docs/patch_grid.png" />

## Statistics of the patches

You can run
```bash
uv run scripts/count_annotations.py data/annotations --manifest data/manifest.csv --csv count_annotations.csv
```

In `count_annotations.csv` you will find the number of annotations per patch, as well as the total number of annotations per class:
| Astrocyte | Pyramidal neuron | Unknown | Total |
|-----------|------------------|---------|-------|
| 284 | 377 | 341 | 1002 |

# License
## Data citation
- source: https://idr.openmicroscopy.org/study/idr0048/
- Authors: Multicolor multiscale brain imaging with chromatic multiphoton serial microscopy, Abdeladim L, Matho KS, Clavreul S, Mahou P, Sintes J-M, Solinas X, Arganda-Carreras I, Turney SG, Lichtman JW, Chessel A, Bemelmans A-P, Loulier K, Supatto W, Livet J, Beaurepaire E
- License / access: CC BY-NC 4.0

## This annotations citation

- Source: doi:10.5281/zenodo.24339993
- Authors: Clément Caporal, Michal Ejankowski, Leonidas Ioannidis, Helen Fiorella Medina, Tania Orosco, Ignacio Arganda-Carreras
- License / access: CC BY-NC 4.0

If you find those annotations useful please cite this repository using its DOI as follows:

> Caporal, C., Ejankowski, M., Ioannidis, Medina, H. F., L., Orosco Salinas, T., & Arganda-Carreras, I. (2026). Point annotations of astrocytes and layer 2/3 pyramidal neurons in Brainbow mouse cortex (OME-Zarr crops from IDR idr0048) (Version v1.0). Zenodo. https://doi.org/10.5281/zenodo.24339994

Note this DOI will resolve to all versions of this repository. If you want to cite a specific version, please use the DOI of that version, which can be found in the Zenodo page of this repository.