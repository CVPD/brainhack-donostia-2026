import tifffile
import os

for root, dirs, files in os.walk("data/raw"):
    for file in files:
        if file.endswith(".tiff"):
            tiff_path = os.path.join(root, file)

            img = tifffile.imread(tiff_path)

            # shape is (c, z, y, x), save it as (z, c, y, x)
            print(f"Converting {tiff_path} from shape {img.shape} to (z, c, y, x)")
            img = img.transpose(1, 0, 2, 3)
            tifffile.imwrite(tiff_path.replace("raw", "raw_converted"), img, imagej=True)