# Ortho Viewer

A lightweight viewer for large GeoTIFF and JP2 orthophotos. No GIS software required.

- **Desktop mode** — pan, zoom, measure distances and areas
- **Web mode** — tile server + Leaflet.js browser interface

![Ortho Viewer Desktop Application](examples/Screenshot_3.png)

---

## Download & Run (no installation needed)

1. Go to the [Releases page](../../releases)
2. Download `OrthoViewer-windows.zip`
3. Extract the zip
4. Run `OrthoViewer.exe` — optionally drag a `.tif` or `.jp2` file onto it

No Python, no GDAL, no conda required.

---

## Run from Source

Requires Python 3.11+ with GDAL installed via conda-forge.

```bash
conda create -n orthoviewer python=3.11
conda activate orthoviewer
conda install -c conda-forge gdal pyproj numpy
pip install PyQt6 fastapi "uvicorn[standard]" pillow
```

**Desktop app:**
```bash
python main.py                        # open file dialog
python main.py path/to/file.tif       # open directly
```

**Web server:**
```bash
python web_server.py path/to/file.tif
# then open http://localhost:8765
```

Optional flags: `--host 0.0.0.0 --port 8765`

---

## Features

- Reads GeoTIFF and JP2 (JPEG 2000) via GDAL — including multi-gigabyte files
- Hardware-accelerated tile rendering with overview levels
- Auto contrast stretch (2nd–98th percentile)
- Measure distance and area tools (geodesic, using pyproj)
- Web tile server compatible with any Leaflet.js map

---

## Building from Source (Windows)

```bash
conda activate orthoviewer
pip install pyinstaller
pyinstaller ortho_viewer.spec
# output: dist/OrthoViewer/
```
