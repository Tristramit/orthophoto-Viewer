# -*- coding: utf-8 -*-
"""
Ortho Viewer — Web Server Mode
===============================
Serves orthophotos as tile maps in the browser via FastAPI + Leaflet, and
lets you upload new files through a password-protected admin page. Each
uploaded file gets its own shareable/embeddable viewer URL.

Architecture:
  - FastAPI backend:
      GET  /api/metadata?file=NAME      → raster info (CRS, bbox, levels)
      GET  /tiles/{file}/{z}/{x}/{y}    → PNG tile (TMS-like, image-space coords)
      POST /api/measure                 → distance/area measurement
      GET  /view?file=NAME              → HTML page with Leaflet viewer (embeddable)
      GET  /admin                       → upload form + file list (HTTP Basic auth)
      POST /admin/upload                → upload a new raster (HTTP Basic auth)
      DELETE /admin/files/{file}        → remove a raster (HTTP Basic auth)
  - Leaflet frontend:
      Custom TileLayer that calls /tiles/{file}/{z}/{x}/{y}
      Coordinate display, distance & area measurement (Leaflet.draw)
      Dark theme to match desktop app

Usage:
    # Start the server (serves files from --data-dir, default ./data):
    python web_server.py --host 0.0.0.0 --port 8765

    # Optionally seed the data dir with a file at startup:
    python web_server.py path/to/file.tif

    Then open:        http://localhost:8765/admin   (upload files, get embed links)
    Embed a file at:  http://localhost:8765/view?file=NAME

Admin credentials come from the ADMIN_USER / ADMIN_PASSWORD environment
variables. If ADMIN_PASSWORD is not set, a random password is generated and
printed once at startup.
"""

from __future__ import annotations
import io
import math
import os
import secrets
import shutil
import sys
import threading
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import numpy as np

# ---------------------------------------------------------------------------
# Attempt to import optional web deps — give a clear error if missing
# ---------------------------------------------------------------------------
try:
    from fastapi import FastAPI, HTTPException, Query, Depends, UploadFile, File
    from fastapi.responses import HTMLResponse, Response, JSONResponse, RedirectResponse
    from fastapi.security import HTTPBasic, HTTPBasicCredentials
    import uvicorn
    _WEB_DEPS_OK = True
except ImportError:
    _WEB_DEPS_OK = False

try:
    from PIL import Image as PILImage
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

from viewer.raster import RasterLoader, TILE_SIZE
from viewer.geo import crs_is_geographic, get_transformer_to_geo, fmt_distance, fmt_area

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ortho-web")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WEB_TILE_SIZE = 256   # standard web map tile size
MAX_OPEN_RASTERS = int(os.environ.get("ORTHO_MAX_OPEN", "4"))  # cached loaders
ALLOWED_EXTENSIONS = {".tif", ".tiff", ".jp2"}

DATA_DIR = Path(os.environ.get("ORTHO_DATA_DIR", "data")).resolve()

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
_GENERATED_PASSWORD = False
if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(12)
    _GENERATED_PASSWORD = True


# ---------------------------------------------------------------------------
# Multi-file loader cache (LRU over open GDAL datasets)
# ---------------------------------------------------------------------------

class _LoaderCache:
    """Keeps up to MAX_OPEN_RASTERS RasterLoaders open, keyed by filename."""

    def __init__(self, maxsize: int):
        self._max = maxsize
        self._d: "OrderedDict[str, RasterLoader]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, filename: str) -> RasterLoader:
        with self._lock:
            ld = self._d.get(filename)
            if ld is not None:
                self._d.move_to_end(filename)
                return ld

            path = _safe_data_path(filename)
            if not path.is_file():
                raise HTTPException(status_code=404, detail=f"No such file: {filename}")

            ld = RasterLoader()
            try:
                ld.open(str(path))
            except Exception as e:
                raise HTTPException(status_code=422, detail=f"Cannot open raster: {e}")

            self._d[filename] = ld
            self._d.move_to_end(filename)
            if len(self._d) > self._max:
                _, evicted = self._d.popitem(last=False)
                evicted.close()
            return ld

    def evict(self, filename: str):
        with self._lock:
            ld = self._d.pop(filename, None)
            if ld is not None:
                ld.close()


_cache = _LoaderCache(MAX_OPEN_RASTERS)


def _safe_data_path(filename: str) -> Path:
    """Resolve filename inside DATA_DIR, rejecting path traversal."""
    if not filename or os.path.basename(filename) != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = (DATA_DIR / filename).resolve()
    if DATA_DIR not in path.parents and path != DATA_DIR:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return path


def _list_files() -> list[str]:
    if not DATA_DIR.is_dir():
        return []
    return sorted(
        p.name for p in DATA_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in ALLOWED_EXTENSIONS
    )


# ---------------------------------------------------------------------------
# Admin auth
# ---------------------------------------------------------------------------

def _build_app() -> "FastAPI":
    app = FastAPI(title="Ortho Viewer Web", version="2.0")
    security = HTTPBasic()

    def require_admin(credentials: HTTPBasicCredentials = Depends(security)):
        user_ok = secrets.compare_digest(credentials.username, ADMIN_USER)
        pass_ok = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
        if not (user_ok and pass_ok):
            raise HTTPException(
                status_code=401,
                detail="Invalid admin credentials",
                headers={"WWW-Authenticate": "Basic"},
            )
        return credentials.username

    # ----------------------------------------------------------------
    # /  → redirect to admin (nothing sensitive listed publicly here)
    # ----------------------------------------------------------------
    @app.get("/")
    def root():
        return RedirectResponse(url="/admin")

    # ----------------------------------------------------------------
    # /admin  — upload form + file list
    # ----------------------------------------------------------------
    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(user: str = Depends(require_admin)):
        return HTMLResponse(content=_admin_html_page())

    @app.get("/admin/files")
    def admin_list_files(user: str = Depends(require_admin)):
        return {"files": [_file_info(f) for f in _list_files()]}

    @app.post("/admin/upload")
    async def admin_upload(user: str = Depends(require_admin), file: UploadFile = File(...)):
        name = os.path.basename(file.filename or "")
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(400, f"Unsupported file type '{ext}'. Allowed: {sorted(ALLOWED_EXTENSIONS)}")

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        dest = _unique_path(DATA_DIR / name)

        with open(dest, "wb") as out:
            shutil.copyfileobj(file.file, out)

        # Validate it actually opens as a raster; clean up if not.
        probe = RasterLoader()
        try:
            probe.open(str(dest))
        except Exception as e:
            dest.unlink(missing_ok=True)
            raise HTTPException(422, f"Uploaded file is not a readable raster: {e}")
        finally:
            probe.close()

        return _file_info(dest.name)

    @app.delete("/admin/files/{filename}")
    def admin_delete_file(filename: str, user: str = Depends(require_admin)):
        path = _safe_data_path(filename)
        if not path.is_file():
            raise HTTPException(404, "No such file")
        _cache.evict(filename)
        path.unlink()
        return {"deleted": filename}

    # ----------------------------------------------------------------
    # /api/metadata
    # ----------------------------------------------------------------
    @app.get("/api/metadata")
    def get_metadata(file: str = Query(...)):
        ld = _cache.get(file)
        m = ld.meta
        max_z = max(0, math.ceil(math.log2(
            max(m.width, m.height) / WEB_TILE_SIZE)))

        bbox_geo = None
        try:
            tfm = get_transformer_to_geo(m.crs_wkt)
            if tfm:
                min_x, min_y, max_x, max_y = m.bbox_world
                corners_world = [
                    (min_x, min_y), (max_x, min_y),
                    (min_x, max_y), (max_x, max_y),
                ]
                geo_corners = [tfm.transform(wx, wy) for wx, wy in corners_world]
                lons = [c[0] for c in geo_corners]
                lats = [c[1] for c in geo_corners]
                bbox_geo = [min(lats), min(lons), max(lats), max(lons)]
            elif crs_is_geographic(m.crs_wkt):
                min_x, min_y, max_x, max_y = m.bbox_world
                bbox_geo = [min_y, min_x, max_y, max_x]
        except Exception:
            pass

        return {
            "filename": os.path.basename(m.path),
            "width": m.width,
            "height": m.height,
            "bands": m.bands,
            "dtype": m.dtype,
            "crs_name": m.crs_name,
            "pixel_size_x": m.pixel_size_x,
            "pixel_size_y": m.pixel_size_y,
            "bbox_world": list(m.bbox_world),
            "bbox_geo": bbox_geo,
            "format": m.format_name,
            "max_zoom": max_z,
            "has_overviews": m.has_overviews,
        }

    # ----------------------------------------------------------------
    # /tiles/{file}/{z}/{x}/{y}  — image-space TMS tiles
    # ----------------------------------------------------------------
    @app.get("/tiles/{file}/{z}/{x}/{y}")
    def get_tile(file: str, z: int, x: int, y: int):
        ld = _cache.get(file)
        m = ld.meta

        if z < 0 or z > 32:
            raise HTTPException(400, "Invalid zoom level")

        tile_native_w = m.width / (2 ** z)
        tile_native_h = m.height / (2 ** z)

        x0 = int(round(x * tile_native_w))
        y0 = int(round(y * tile_native_h))
        x1 = int(round((x + 1) * tile_native_w))
        y1 = int(round((y + 1) * tile_native_h))

        x0 = max(0, x0)
        y0 = max(0, y0)
        x1 = min(m.width, x1)
        y1 = min(m.height, y1)

        if x1 <= x0 or y1 <= y0:
            return Response(content=_transparent_png(), media_type="image/png")

        src_w = x1 - x0
        src_h = y1 - y0
        out_w = WEB_TILE_SIZE
        out_h = WEB_TILE_SIZE

        arr = _read_region(ld, x0, y0, src_w, src_h, out_w, out_h)
        if arr is None:
            return Response(content=_transparent_png(), media_type="image/png")

        png_bytes = _array_to_png(arr)
        return Response(content=png_bytes, media_type="image/png",
                        headers={"Cache-Control": "public, max-age=3600"})

    # ----------------------------------------------------------------
    # /api/measure  — server-side measurement (optional, for precision)
    # ----------------------------------------------------------------
    @app.post("/api/measure")
    def measure(body: dict):
        file = body.get("file")
        if not file:
            raise HTTPException(400, "Missing 'file'")
        ld = _cache.get(file)
        from viewer.geo import geodesic_distance_m, geodesic_area_m2
        pts = [(p["x"], p["y"]) for p in body.get("points", [])]
        kind = body.get("kind", "distance")
        crs_wkt = ld.meta.crs_wkt
        if kind == "distance":
            val = geodesic_distance_m(pts, crs_wkt)
            return {"value": val, "label": fmt_distance(val)}
        elif kind == "area":
            val = geodesic_area_m2(pts, crs_wkt)
            return {"value": val, "label": fmt_area(val)}
        raise HTTPException(400, "kind must be 'distance' or 'area'")

    # ----------------------------------------------------------------
    # /view  — Leaflet HTML viewer for one file (this is what you embed)
    # ----------------------------------------------------------------
    @app.get("/view", response_class=HTMLResponse)
    def view(file: str = Query(...)):
        # 404 early if the file doesn't exist, rather than a broken page.
        _safe_data_path(file)
        if not (DATA_DIR / file).is_file():
            raise HTTPException(404, f"No such file: {file}")
        return HTMLResponse(content=_html_page(file))

    return app


def _file_info(filename: str) -> dict:
    path = DATA_DIR / filename
    return {
        "filename": filename,
        "size_bytes": path.stat().st_size if path.is_file() else None,
        "view_url": f"/view?file={_url_quote(filename)}",
        "embed_snippet": (
            f'<iframe src="/view?file={_url_quote(filename)}" '
            f'style="width:100%;height:600px;border:0;"></iframe>'
        ),
    }


def _url_quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 1
    while True:
        candidate = path.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


# ---------------------------------------------------------------------------
# Region reader (reuses GDAL directly for web tiles)
# ---------------------------------------------------------------------------

def _read_region(ld: RasterLoader, x0: int, y0: int, src_w: int, src_h: int,
                 out_w: int, out_h: int) -> Optional[np.ndarray]:
    """Read a region from the open GDAL dataset and return uint8 (H,W,3)."""
    ds = ld._ds  # type: ignore[attr-defined]
    if ds is None:
        return None
    from osgeo import gdal

    band_order = ld._band_order  # type: ignore[attr-defined]
    stretch_min = ld._stretch_min  # type: ignore[attr-defined]
    stretch_max = ld._stretch_max  # type: ignore[attr-defined]

    channels = []
    for idx, bi in enumerate(band_order):
        band = ds.GetRasterBand(bi)
        raw = band.ReadRaster(x0, y0, src_w, src_h, out_w, out_h,
                              resample_alg=gdal.GRIORA_Bilinear)
        if raw is None:
            return None
        from viewer.raster import _gdal_dtype_to_numpy
        np_dtype = _gdal_dtype_to_numpy(band.DataType)
        ch = np.frombuffer(raw, dtype=np_dtype).reshape(out_h, out_w).astype(np.float32)
        lo = float(stretch_min[idx]) if stretch_min is not None else 0.0
        hi = float(stretch_max[idx]) if stretch_max is not None else 255.0
        denom = hi - lo if hi > lo else 1.0
        ch8 = np.clip((ch - lo) / denom * 255.0, 0, 255).astype(np.uint8)
        channels.append(ch8)

    if len(channels) == 1:
        channels = channels * 3

    return np.stack(channels[:3], axis=2)


def _array_to_png(arr: np.ndarray) -> bytes:
    """Convert uint8 (H,W,3) array to PNG bytes."""
    if _PIL_OK:
        img = PILImage.fromarray(arr, mode="RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False)
        return buf.getvalue()

    # Fallback: use PyQt6 if Pillow not available
    from PyQt6.QtGui import QImage
    from PyQt6.QtCore import QBuffer, QIODevice
    h, w = arr.shape[:2]
    arr_c = np.ascontiguousarray(arr)
    img = QImage(arr_c.data, w, h, w * 3, QImage.Format.Format_RGB888)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def _transparent_png() -> bytes:
    """Return a 1×1 transparent PNG."""
    return (
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
        b'\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
        b'\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01'
        b'\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
    )


# ---------------------------------------------------------------------------
# Admin HTML page
# ---------------------------------------------------------------------------

def _admin_html_page() -> str:
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Ortho Viewer — Admin</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #16161a; color: #dde; font-family: "Segoe UI", sans-serif;
         padding: 24px; max-width: 780px; margin: 0 auto; }
  h1 { font-size: 20px; color: #aad; margin-bottom: 4px; }
  p.hint { color: #889; font-size: 12px; margin-bottom: 20px; }
  #drop { border: 2px dashed #445; border-radius: 10px; padding: 28px; text-align: center;
          color: #889; margin-bottom: 24px; cursor: pointer; transition: .15s; }
  #drop.drag { border-color: #8ab4f8; background: rgba(58,130,246,.08); color: #ccd; }
  input[type=file] { display: none; }
  #status { font-size: 12px; margin-bottom: 16px; min-height: 16px; }
  #status.err { color: #f66; }
  #status.ok { color: #6d6; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #2a2a36; }
  th { color: #889; font-weight: 500; font-size: 11px; text-transform: uppercase; }
  a { color: #8ab4f8; }
  code { background: #1e1e26; padding: 2px 6px; border-radius: 4px; font-size: 11px; }
  button.del { background: #3a1e22; color: #f88; border: 1px solid #633; border-radius: 5px;
               padding: 3px 10px; font-size: 11px; cursor: pointer; }
  button.del:hover { background: #4a262c; }
  .embed-box { display: flex; gap: 6px; align-items: center; }
  .embed-box code { flex: 1; overflow-x: auto; white-space: nowrap; }
  .copy-btn { background: #2a2a36; color: #dde; border: 1px solid #444; border-radius: 5px;
              padding: 3px 8px; font-size: 11px; cursor: pointer; }
</style>
</head>
<body>
<h1>Ortho Viewer — Admin</h1>
<p class="hint">Upload a GeoTIFF or JP2, then copy its embed link or &lt;iframe&gt; snippet.</p>

<div id="drop">Click or drop a .tif / .tiff / .jp2 file here to upload</div>
<input type="file" id="fileInput" accept=".tif,.tiff,.jp2"/>
<div id="status"></div>

<table>
  <thead><tr><th>File</th><th>Size</th><th>Embed</th><th></th></tr></thead>
  <tbody id="rows"><tr><td colspan="4">Loading…</td></tr></tbody>
</table>

<script>
const drop = document.getElementById('drop');
const fileInput = document.getElementById('fileInput');
const status = document.getElementById('status');
const rows = document.getElementById('rows');

drop.addEventListener('click', () => fileInput.click());
drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('drag'); });
drop.addEventListener('dragleave', () => drop.classList.remove('drag'));
drop.addEventListener('drop', e => {
  e.preventDefault();
  drop.classList.remove('drag');
  if (e.dataTransfer.files.length) upload(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', () => {
  if (fileInput.files.length) upload(fileInput.files[0]);
});

function fmtSize(bytes) {
  if (bytes == null) return '—';
  const units = ['B','KB','MB','GB','TB'];
  let i = 0, v = bytes;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(1) + ' ' + units[i];
}

async function upload(file) {
  status.className = ''; status.textContent = `Uploading ${file.name}…`;
  const form = new FormData();
  form.append('file', file);
  try {
    const res = await fetch('/admin/upload', { method: 'POST', body: form });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Upload failed');
    status.className = 'ok'; status.textContent = `Uploaded ${data.filename}`;
    fileInput.value = '';
    loadFiles();
  } catch (err) {
    status.className = 'err'; status.textContent = 'Error: ' + err.message;
  }
}

async function loadFiles() {
  const res = await fetch('/admin/files');
  const data = await res.json();
  if (!data.files.length) {
    rows.innerHTML = '<tr><td colspan="4">No files uploaded yet.</td></tr>';
    return;
  }
  rows.innerHTML = data.files.map(f => `
    <tr>
      <td><a href="${f.view_url}" target="_blank">${f.filename}</a></td>
      <td>${fmtSize(f.size_bytes)}</td>
      <td><div class="embed-box"><code>${escapeHtml(f.embed_snippet)}</code>
        <button class="copy-btn" onclick="copySnippet(this)" data-snippet="${escapeAttr(f.embed_snippet)}">Copy</button></div></td>
      <td><button class="del" onclick="del('${encodeURIComponent(f.filename)}')">Delete</button></td>
    </tr>
  `).join('');
}

function escapeHtml(s) { return s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function escapeAttr(s) { return s.replace(/"/g, '&quot;'); }

function copySnippet(btn) {
  navigator.clipboard.writeText(btn.dataset.snippet).catch(() => {});
  btn.textContent = 'Copied!';
  setTimeout(() => btn.textContent = 'Copy', 1200);
}

async function del(filename) {
  if (!confirm('Delete this file?')) return;
  const res = await fetch('/admin/files/' + filename, { method: 'DELETE' });
  if (res.ok) loadFiles();
  else status.textContent = 'Delete failed';
}

loadFiles();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Viewer HTML page
# ---------------------------------------------------------------------------

def _html_page(file: str) -> str:
    import json
    file_json = json.dumps(file)
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Ortho Viewer</title>

<!-- Leaflet -->
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<!-- Leaflet.draw for measurements -->
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.css"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.js"></script>

<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #16161a; color: #dde; font-family: "Segoe UI", sans-serif; height: 100vh; display: flex; flex-direction: column; }
  #toolbar { display: flex; align-items: center; gap: 8px; padding: 6px 12px;
             background: #1e1e26; border-bottom: 1px solid #333; flex-shrink: 0; }
  #toolbar h1 { font-size: 15px; font-weight: 600; color: #aad; margin-right: 12px; }
  .tb-btn { background: #2a2a36; color: #dde; border: 1px solid #444; border-radius: 5px;
            padding: 4px 12px; font-size: 12px; cursor: pointer; transition: .15s; }
  .tb-btn:hover { background: #3a3a50; border-color: #667; }
  .tb-btn.active { background: rgba(58,130,246,.25); border-color: rgba(58,130,246,.7); color: #8ab4f8; }
  #status { font-size: 11px; margin-left: auto; color: #88a; }
  #map { flex: 1; background: #16161a; }
  #info-panel { position: absolute; top: 60px; right: 12px; z-index: 1000; width: 280px;
                background: rgba(22,22,30,.92); border: 1px solid #333; border-radius: 8px;
                padding: 12px 14px; font-size: 11px; line-height: 1.7; color: #ccd;
                backdrop-filter: blur(6px); }
  #info-panel h3 { font-size: 12px; color: #8ab4f8; margin-bottom: 6px; }
  #info-panel .row { display: flex; justify-content: space-between; }
  #info-panel .key { color: #889; }
  #measure-panel { position: absolute; bottom: 24px; left: 12px; z-index: 1000;
                   background: rgba(22,22,30,.92); border: 1px solid #333; border-radius: 8px;
                   padding: 10px 14px; font-size: 12px; color: #dde; min-width: 240px;
                   backdrop-filter: blur(6px); display: none; }
  #measure-panel strong { color: #f5c842; }
  #coord-bar { position: absolute; bottom: 0; left: 0; right: 0; z-index: 900;
               background: rgba(22,22,30,.85); padding: 3px 10px; font-size: 11px;
               color: #88a; pointer-events: none; }
  .leaflet-container { background: #16161a !important; }
  /* Dark Leaflet controls */
  .leaflet-control-zoom a, .leaflet-draw-toolbar a {
    background: #2a2a36 !important; color: #ccd !important;
    border-color: #444 !important; }
  .leaflet-draw-toolbar a:hover { background: #3a3a50 !important; }
  .leaflet-popup-content-wrapper { background: #1e1e2a; color: #dde; border: 1px solid #445; }
  .leaflet-popup-tip { background: #1e1e2a; }
</style>
</head>
<body>

<div id="toolbar">
  <h1>Ortho Viewer</h1>
  <button class="tb-btn" id="btn-fit" title="Fit image (F)">⊞ Fit</button>
  <button class="tb-btn" id="btn-clear" title="Clear measurements">🗑 Clear</button>
  <span id="status">Loading…</span>
</div>

<div id="map"></div>
<div id="info-panel"><h3>Metadata</h3><div id="meta-rows">Loading…</div></div>
<div id="measure-panel"><strong id="measure-val">—</strong><br/><span id="measure-hint" style="color:#889;font-size:10px"></span></div>
<div id="coord-bar">Coordinates: —</div>

<script>
// -----------------------------------------------------------------------
// Bootstrap
// -----------------------------------------------------------------------
const FILE = __FILE_JSON__;

const map = L.map('map', {
  crs: L.CRS.Simple,
  zoomControl: true,
  attributionControl: false,
  minZoom: -6,
  maxZoom: 10,
  zoomSnap: 0.25,
  zoomDelta: 0.5,
});

let meta = null;
let imageBounds = null;

async function init() {
  const res = await fetch('/api/metadata?file=' + encodeURIComponent(FILE));
  if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
  meta = await res.json();
  document.getElementById('status').textContent =
    meta.filename + ' · ' + meta.width + '×' + meta.height + ' px';
  renderMeta(meta);
  setupMap(meta);
}

function renderMeta(m) {
  const rows = [
    ['Format', m.format],
    ['Size', m.width + ' × ' + m.height + ' px'],
    ['Bands', m.bands + ' (' + m.dtype + ')'],
    ['CRS', m.crs_name],
    ['Pixel size', m.pixel_size_x.toPrecision(5) + ' × ' + m.pixel_size_y.toPrecision(5)],
    ['Overviews', m.has_overviews ? 'yes' : 'none'],
  ];
  document.getElementById('meta-rows').innerHTML =
    rows.map(([k,v]) =>
      `<div class="row"><span class="key">${k}</span><span>${v}</span></div>`
    ).join('');
}

// -----------------------------------------------------------------------
// Map setup
// -----------------------------------------------------------------------
function setupMap(m) {
  // Image-space coordinate system: y grows downward (Leaflet Simple CRS)
  // Pixel (0,0) = top-left, (width, height) = bottom-right
  // Leaflet Simple: lat = -y_pixel, lng = x_pixel  (north-up = negative y)
  imageBounds = [[-m.height, 0], [0, m.width]];

  // Custom TileLayer for image-space tiles, scoped to this file
  const tileLayer = L.tileLayer('/tiles/' + encodeURIComponent(FILE) + '/{z}/{x}/{y}', {
    tileSize: 256,
    minZoom: -6,
    maxZoom: m.max_zoom,
    attribution: '',
    noWrap: true,
    bounds: imageBounds,
  });

  tileLayer.getTileUrl = function(coords) {
    const z = Math.max(0, coords.z);
    const x = coords.x;
    const y = coords.y;
    return `/tiles/${encodeURIComponent(FILE)}/${z}/${x}/${y}`;
  };

  tileLayer.addTo(map);
  map.fitBounds(imageBounds);

  setupMeasure(m);
  setupCoordDisplay();
}

// -----------------------------------------------------------------------
// Measurement (Leaflet.draw)
// -----------------------------------------------------------------------
let drawnItems = null;

function setupMeasure(m) {
  drawnItems = new L.FeatureGroup();
  map.addLayer(drawnItems);

  const drawControl = new L.Control.Draw({
    edit: { featureGroup: drawnItems },
    draw: {
      polyline: { shapeOptions: { color: '#f5c842', weight: 2 } },
      polygon:  { shapeOptions: { color: '#50c878', weight: 2 },
                  showArea: true },
      rectangle: false,
      circle: false,
      circlemarker: false,
      marker: false,
    },
  });
  map.addControl(drawControl);

  map.on(L.Draw.Event.CREATED, async (e) => {
    drawnItems.addLayer(e.layer);
    const coords = e.layer.getLatLngs();
    await measureLayer(e.layerType, coords, e.layer);
  });

  document.getElementById('btn-clear').addEventListener('click', () => {
    drawnItems.clearLayers();
    document.getElementById('measure-panel').style.display = 'none';
  });
}

// Convert Leaflet Simple latlng (lat=-y, lng=x) → pixel coords
function latlngToPixel(ll) {
  return { x: ll.lng, y: -ll.lat };
}

async function measureLayer(type, coords, layer) {
  let points = [];
  if (type === 'polyline') {
    const flat = Array.isArray(coords[0]) ? coords[0] : coords;
    points = flat.map(latlngToPixel);
    const res = await fetch('/api/measure', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ kind: 'distance', points, file: FILE }),
    });
    const data = await res.json();
    showMeasure(data.label, 'Double-click to stop drawing');
    layer.bindPopup(`<b>Distance</b><br/>${data.label}`).openPopup();

  } else if (type === 'polygon') {
    const ring = Array.isArray(coords[0]) ? coords[0] : coords;
    points = ring.map(latlngToPixel);
    const res = await fetch('/api/measure', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ kind: 'area', points, file: FILE }),
    });
    const data = await res.json();
    showMeasure(data.label, 'Double-click to close polygon');
    layer.bindPopup(`<b>Area</b><br/>${data.label}`).openPopup();
  }
}

function showMeasure(label, hint) {
  const panel = document.getElementById('measure-panel');
  panel.style.display = 'block';
  document.getElementById('measure-val').textContent = label;
  document.getElementById('measure-hint').textContent = hint || '';

  // Copy to clipboard on click
  panel.onclick = () => {
    navigator.clipboard.writeText(label).catch(() => {});
    panel.style.opacity = '0.6';
    setTimeout(() => panel.style.opacity = '1', 300);
  };
}

// -----------------------------------------------------------------------
// Coordinate display
// -----------------------------------------------------------------------
function setupCoordDisplay() {
  const bar = document.getElementById('coord-bar');
  map.on('mousemove', (e) => {
    const px = e.latlng.lng;
    const py = -e.latlng.lat;
    bar.textContent = `Image px  X: ${px.toFixed(1)}  Y: ${py.toFixed(1)}`;
  });
}

// -----------------------------------------------------------------------
// Toolbar
// -----------------------------------------------------------------------
document.getElementById('btn-fit').addEventListener('click', () => {
  if (imageBounds) map.fitBounds(imageBounds);
});

// Keyboard shortcuts
document.addEventListener('keydown', (e) => {
  if (e.key === 'f' || e.key === 'F' || e.key === '0') {
    if (imageBounds) map.fitBounds(imageBounds);
  }
});

// -----------------------------------------------------------------------
// Start
// -----------------------------------------------------------------------
init().catch(err => {
  document.getElementById('status').textContent = 'Error: ' + err.message;
});
</script>
</body>
</html>
""".replace("__FILE_JSON__", file_json)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def serve(seed_path: Optional[str] = None, host: str = "127.0.0.1", port: int = 8765):
    if not _WEB_DEPS_OK:
        print("ERROR: Missing web dependencies.  Install them with:")
        print("  pip install fastapi \"uvicorn[standard]\" pillow python-multipart")
        sys.exit(1)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if seed_path:
        src = Path(seed_path)
        if not src.is_file():
            print(f"ERROR: File not found: {seed_path}")
            sys.exit(1)
        dest = DATA_DIR / src.name
        if not dest.exists():
            shutil.copy2(src, dest)
        print(f"Seeded data dir with: {dest.name}")

    app = _build_app()

    print(f"\nOrtho Viewer web server running at  http://{host}:{port}")
    print(f"Admin panel:                        http://{host}:{port}/admin")
    print(f"Data directory:                      {DATA_DIR}")
    print(f"Admin user:                           {ADMIN_USER}")
    if _GENERATED_PASSWORD:
        print(f"Admin password (auto-generated):      {ADMIN_PASSWORD}")
        print("  Set ADMIN_PASSWORD to pin this across restarts.")
    else:
        print("Admin password:                       (from ADMIN_PASSWORD env var)")
    print("\nPress Ctrl+C to stop.\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Ortho Viewer web server")
    parser.add_argument("file", nargs="?", help="Optional raster file to seed the data dir with")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(args.file, args.host, args.port)
