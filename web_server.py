# -*- coding: utf-8 -*-
"""
Ortho Viewer — Web Server Mode
===============================
Serves the orthophoto as a tile map in the browser via FastAPI + Leaflet.

Architecture:
  - FastAPI backend:
      GET /api/metadata        → raster info (CRS, bbox, levels)
      GET /tiles/{z}/{x}/{y}   → PNG tile (TMS-like, image-space coords)
      GET /                    → HTML page with Leaflet viewer
  - Leaflet frontend:
      Custom TileLayer that calls /tiles/{z}/{x}/{y}
      Coordinate display, distance & area measurement (Leaflet.draw)
      Dark theme to match desktop app

Usage:
    # Run with a specific file:
    python web_server.py path/to/file.tif

    # Or start server and open the file browser:
    python web_server.py

    Then open:  http://localhost:8765
"""

from __future__ import annotations
import io
import math
import os
import sys
import threading
import logging
from typing import Optional

import numpy as np

# ---------------------------------------------------------------------------
# Attempt to import optional web deps — give a clear error if missing
# ---------------------------------------------------------------------------
try:
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import HTMLResponse, Response, JSONResponse
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
# Tile server
# ---------------------------------------------------------------------------

WEB_TILE_SIZE = 256   # standard web map tile size

_loader: Optional[RasterLoader] = None


def _get_loader() -> RasterLoader:
    if _loader is None or _loader.meta is None:
        raise HTTPException(status_code=503, detail="No raster file loaded")
    return _loader


def _build_app() -> "FastAPI":
    app = FastAPI(title="Ortho Viewer Web", version="1.0")

    # ----------------------------------------------------------------
    # /api/metadata
    # ----------------------------------------------------------------
    @app.get("/api/metadata")
    def get_metadata():
        ld = _get_loader()
        m = ld.meta
        # Compute zoom levels: z=0 is the full image in one tile, z=max is native
        # Total tiles at full resolution:
        max_z = max(0, math.ceil(math.log2(
            max(m.width, m.height) / WEB_TILE_SIZE)))

        # Try to get the bounding box in WGS84 for Leaflet
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
    # /tiles/{z}/{x}/{y}  — image-space TMS tiles
    #
    # Coordinate convention (matches the frontend TileLayer):
    #   z = zoom level (0 = full image in 1 tile, z=n → 2^n × 2^n tiles)
    #   x, y = tile column / row at that zoom level
    # ----------------------------------------------------------------
    @app.get("/tiles/{z}/{x}/{y}")
    def get_tile(z: int, x: int, y: int):
        ld = _get_loader()
        m = ld.meta

        if z < 0 or z > 32:
            raise HTTPException(400, "Invalid zoom level")

        # Each tile covers (m.width / 2^z) × (m.height / 2^z) native pixels
        tile_native_w = m.width  / (2 ** z)
        tile_native_h = m.height / (2 ** z)

        # Source region in native image pixels
        x0 = int(round(x * tile_native_w))
        y0 = int(round(y * tile_native_h))
        x1 = int(round((x + 1) * tile_native_w))
        y1 = int(round((y + 1) * tile_native_h))

        x0 = max(0, x0)
        y0 = max(0, y0)
        x1 = min(m.width,  x1)
        y1 = min(m.height, y1)

        if x1 <= x0 or y1 <= y0:
            # Return a transparent tile
            return Response(content=_transparent_png(), media_type="image/png")

        src_w = x1 - x0
        src_h = y1 - y0

        # Output at WEB_TILE_SIZE × WEB_TILE_SIZE
        out_w = WEB_TILE_SIZE
        out_h = WEB_TILE_SIZE

        # Use the raster loader's overview level selection
        level = RasterLoader.get_overview_level(WEB_TILE_SIZE / max(src_w, src_h))
        scale = 2 ** level

        # Read via GDAL (simplified: direct tile read at appropriate level)
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
        ld = _get_loader()
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
    # / — Leaflet HTML viewer
    # ----------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(content=_html_page())

    return app


# ---------------------------------------------------------------------------
# Region reader (reuses GDAL directly for web tiles)
# ---------------------------------------------------------------------------

def _read_region(ld: RasterLoader, x0: int, y0: int, src_w: int, src_h: int,
                 out_w: int, out_h: int) -> Optional[np.ndarray]:
    """Read a region from the open GDAL dataset and return uint8 (H,W,3)."""
    # Access the private dataset — acceptable since this module is co-located
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
    # Minimal valid 1×1 transparent PNG
    return (
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
        b'\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89'
        b'\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01'
        b'\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
    )


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------

def _html_page() -> str:
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
  const res = await fetch('/api/metadata');
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

  // Custom TileLayer for image-space tiles
  // URL pattern: /tiles/{z}/{x}/{y}
  // Our tiles use z=0 for full image, z=n for 2^n × 2^n grid
  const tileLayer = L.tileLayer('/tiles/{z}/{x}/{y}', {
    tileSize: 256,
    minZoom: -6,
    maxZoom: m.max_zoom,
    attribution: '',
    noWrap: true,
    bounds: imageBounds,
  });

  // Override getTileUrl to use our image-space z/x/y
  tileLayer.getTileUrl = function(coords) {
    // Leaflet's internal z starts at 0 but maps to our zoom differently.
    // At zoom level z (Leaflet), the image spans 2^z tiles in each direction.
    // Our tile z = Leaflet internal z (at zoom 0, one tile covers the image).
    const z = Math.max(0, coords.z);
    const x = coords.x;
    const y = coords.y;
    // Flip y for TMS convention if needed — Leaflet Simple uses top-down y
    return `/tiles/${z}/${x}/${y}`;
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
    // Request world coords from server by converting pixel → world client-side
    // (we don't expose the geotransform to JS, so we ask the server)
    const res = await fetch('/api/measure', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ kind: 'distance', points }),
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
      body: JSON.stringify({ kind: 'area', points }),
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
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def serve(path: Optional[str] = None, host: str = "127.0.0.1", port: int = 8765):
    if not _WEB_DEPS_OK:
        print("ERROR: Missing web dependencies.  Install them with:")
        print("  pip install fastapi uvicorn[standard] pillow")
        sys.exit(1)

    global _loader
    _loader = RasterLoader()

    if path:
        print(f"Loading raster: {path}")
        try:
            _loader.open(path)
            print(f"  OK — {_loader.meta.width} × {_loader.meta.height} px, "
                  f"{_loader.meta.bands} band(s)")
        except Exception as e:
            print(f"ERROR: Cannot open raster: {e}")
            sys.exit(1)
    else:
        print("No file specified — open a file via the API after startup.")
        print("(You can POST to /api/open?path=... once that endpoint is added)")

    app = _build_app()
    print(f"\nOrtho Viewer web server running at  http://{host}:{port}")
    print("Press Ctrl+C to stop.\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Ortho Viewer web server")
    parser.add_argument("file", nargs="?", help="Raster file to open")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    serve(args.file, args.host, args.port)
