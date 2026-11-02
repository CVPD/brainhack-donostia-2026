This project is part of the  brainhack-donostia 2026 hackathon. For more information, please visit
https://brainhack-donostia.github.io/

## Project description

<img width="1996" height="702" alt="2026-11-02_15-49" src="https://github.com/user-attachments/assets/06080034-067d-47b5-a314-4ef0f1cda69a" />

**Brainbow** datasets enable researchers to distinguish individual cells and identify **lineage clusters**, providing valuable insights into brain development and cellular organization. Recent advances in microscopy, such as **ChroMS**, now make it possible to acquire **mesoscale images of entire brains**, opening new opportunities for **large-scale cellular analysis**.

The aim of this project is to develop an automated cell detection approach using deep learning. Starting from an existing publicly available brainbow dataset, participants will manually annotate neurons to create a new collection of labelled images. These annotations will then be used to train and evaluate a deep learning model using BiaPy capable of automatically detecting individual cells.

The resulting annotated dataset will be made publicly available through a Zenodo repository to support further research and reuse by the scientific community.

Depending on the model's performance and the time available, participants will also have the opportunity to investigate neuronal distribution patterns and provide quantitative statistics on cell organization across the mouse cortex.

Coding experience is useful but not needed to fully participate in this project

## Instructions to download the patches

Download all the patches according to `data/datasets.yaml` to `data/raw`
```bash
uv run python scripts/download_patches.py --config data/datasets.yaml --output-dir data/ --skip-existing
```

## Data
- Data source: https://idr.openmicroscopy.org/study/idr0048/
- Authors: Multicolor multiscale brain imaging with chromatic multiphoton serial microscopy, Abdeladim L, Matho KS, Clavreul S, Mahou P, Sintes J-M, Solinas X, Arganda-Carreras I, Turney SG, Lichtman JW, Chessel A, Bemelmans A-P, Loulier K, Supatto W, Livet J, Beaurepaire E
- License / access: CC BY-NC 4.0

## Useful skills or interest
- Non linear microscopy
- Image analysis
- Python
- Deep learning
- Annotations

## Tools to install

- BiaPy: https://biapyx.github.io/
- Napari: https://napari.org/stable/
- FIJI: https://imagej.net/software/fiji/
