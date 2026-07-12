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
python web_server.py
# then open http://localhost:8765/admin to upload a file and get its embed link
```

Optional flags: `--host 0.0.0.0 --port 8765`

---

## Run as a server / embed on a webpage

The web server supports multiple orthophotos at once: upload files through a
password-protected admin page, then embed any of them with an `<iframe>`.

**With Docker:**

```bash
# 1. Set a real admin password in docker-compose.yml (ADMIN_PASSWORD), then:
docker compose up -d --build

# 2. Open the admin page to upload files and copy embed links:
#    http://your-server:8765/admin
```

Uploaded rasters persist in `./data` (mounted as a volume). Each file gets
its own viewer URL, e.g. `http://your-server:8765/view?file=site1.tif`, which
you can drop straight into an `<iframe>`:

```html
<iframe src="http://your-server:8765/view?file=site1.tif"
        style="width:100%;height:600px;border:0;"></iframe>
```

If the embedding page is served over HTTPS, put the ortho server behind a
reverse proxy (Caddy, nginx, Traefik) with TLS — browsers block mixed
HTTP content in an HTTPS iframe.

Admin credentials come from the `ADMIN_USER` / `ADMIN_PASSWORD` environment
variables. If `ADMIN_PASSWORD` isn't set, a random one is generated and
printed to the server log on startup — set it explicitly for anything
long-running.

**Without Docker**, the same server works directly:

```bash
ADMIN_PASSWORD=your-password python web_server.py --host 0.0.0.0 --port 8765
```

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
