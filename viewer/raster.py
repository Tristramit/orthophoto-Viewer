"""
Raster loading and tile management using GDAL.

RasterLoader reads GeoTIFF / JPEG 2000 / world-file rasters and exposes
them as a tile cache of QPixmap objects for efficient rendering at any zoom.
"""

from __future__ import annotations
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtCore import Qt

try:
    from osgeo import gdal, osr
    gdal.UseExceptions()
    _GDAL_OK = True
except ImportError:
    _GDAL_OK = False


TILE_SIZE = 512       # native image pixels per tile edge
MAX_CACHE_TILES = 256 # maximum number of QPixmap tiles to keep in memory


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

@dataclass
class RasterMetadata:
    """All static properties of the opened raster."""
    path: str
    width: int            # native pixel width
    height: int           # native pixel height
    bands: int            # number of bands
    dtype: str            # numpy dtype name
    nodata: Optional[float]
    geotransform: tuple   # GDAL 6-element geotransform
    crs_wkt: str          # WKT of the spatial reference
    crs_name: str         # human-readable CRS name
    pixel_size_x: float   # horizontal pixel size in CRS units (usually metres or degrees)
    pixel_size_y: float   # vertical pixel size (absolute value)

    # Bounding box in world (CRS) coordinates
    bbox_world: tuple     # (min_x, min_y, max_x, max_y)

    # Convenience flags
    is_rgb: bool = False
    has_overviews: bool = False
    overview_count: int = 0
    format_name: str = ""

    @property
    def aspect_ratio(self) -> float:
        return self.width / max(self.height, 1)

    def summary_lines(self) -> list[str]:
        w, s, e, n = self.bbox_world
        px = f"{self.pixel_size_x:.6g}"
        py = f"{self.pixel_size_y:.6g}"
        return [
            f"File:       {self.path}",
            f"Format:     {self.format_name}",
            f"Size:       {self.width} × {self.height} px  ({self.bands} band{'s' if self.bands != 1 else ''})",
            f"Data type:  {self.dtype}",
            f"CRS:        {self.crs_name}",
            f"Pixel size: {px} × {py}  (CRS units)",
            f"Bbox W:     {w:.6g}",
            f"Bbox E:     {e:.6g}",
            f"Bbox S:     {s:.6g}",
            f"Bbox N:     {n:.6g}",
            f"Overviews:  {'yes (' + str(self.overview_count) + ')' if self.has_overviews else 'none'}",
        ]


# ---------------------------------------------------------------------------
# LRU cache
# ---------------------------------------------------------------------------

class _LRU:
    def __init__(self, maxsize: int):
        self._max = maxsize
        self._d: OrderedDict = OrderedDict()

    def get(self, key):
        if key not in self._d:
            return None
        self._d.move_to_end(key)
        return self._d[key]

    def put(self, key, value):
        self._d[key] = value
        self._d.move_to_end(key)
        if len(self._d) > self._max:
            self._d.popitem(last=False)

    def clear(self):
        self._d.clear()


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

class RasterLoader:
    """Open a georeferenced raster and serve tiles as QPixmap objects.

    Thread-safe tile reads are serialised via a lock because GDAL datasets
    are not thread-safe by default.
    """

    def __init__(self):
        self._ds = None          # gdal.Dataset
        self._meta: Optional[RasterMetadata] = None
        self._cache = _LRU(MAX_CACHE_TILES)
        self._lock = threading.Lock()
        self._stretch_min: Optional[np.ndarray] = None  # per-band display min
        self._stretch_max: Optional[np.ndarray] = None  # per-band display max
        self._band_order: list[int] = []               # 1-based GDAL band indices → R,G,B

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def meta(self) -> Optional[RasterMetadata]:
        return self._meta

    def open(self, path: str) -> RasterMetadata:
        """Open a raster file and compute display stretch from an overview."""
        if not _GDAL_OK:
            raise ImportError(
                "GDAL is not installed.\n\n"
                "Install it with one of:\n"
                "  pip install gdal\n"
                "  conda install -c conda-forge gdal"
            )
        with self._lock:
            if self._ds is not None:
                self._ds = None
            self._cache.clear()

            ds = gdal.Open(path, gdal.GA_ReadOnly)
            if ds is None:
                raise IOError(f"GDAL cannot open: {path}")

            self._ds = ds
            self._meta = self._build_metadata(path, ds)
            self._band_order = self._detect_band_order(ds)
            self._compute_stretch(ds)
            return self._meta

    def get_tile(self, tx: int, ty: int, level: int) -> Optional[QPixmap]:
        """Return a cached QPixmap for tile (tx, ty) at overview level.

        Returns None if the tile is outside the raster bounds.
        """
        if self._ds is None:
            return None
        key = (tx, ty, level)
        pix = self._cache.get(key)
        if pix is not None:
            return pix
        with self._lock:
            # Double-check after acquiring lock
            pix = self._cache.get(key)
            if pix is not None:
                return pix
            arr = self._read_tile(tx, ty, level)
            if arr is None:
                return None
            pix = self._array_to_pixmap(arr)
            self._cache.put(key, pix)
            return pix

    def pixel_to_world(self, px: float, py: float) -> tuple[float, float]:
        """Convert image pixel (col, row) → world (CRS) coordinates."""
        gt = self._meta.geotransform
        wx = gt[0] + px * gt[1] + py * gt[2]
        wy = gt[3] + px * gt[4] + py * gt[5]
        return wx, wy

    def world_to_pixel(self, wx: float, wy: float) -> tuple[float, float]:
        """Convert world coordinates → image pixel (col, row)."""
        gt = self._meta.geotransform
        # Solve the 2x2 system: [gt1 gt2; gt4 gt5] * [px; py] = [wx-gt0; wy-gt3]
        det = gt[1] * gt[5] - gt[2] * gt[4]
        if abs(det) < 1e-15:
            return 0.0, 0.0
        dx, dy = wx - gt[0], wy - gt[3]
        px = (dx * gt[5] - dy * gt[2]) / det
        py = (dy * gt[1] - dx * gt[4]) / det
        return px, py

    @staticmethod
    def get_overview_level(zoom: float) -> int:
        """Choose a power-of-2 downsample level given the current zoom factor.

        zoom < 1 means the image is smaller than native on screen.
        """
        if zoom >= 1.0:
            return 0
        return max(0, int(math.floor(-math.log2(zoom + 1e-12))))

    def close(self):
        with self._lock:
            self._ds = None
            self._cache.clear()
            self._meta = None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_metadata(self, path: str, ds) -> RasterMetadata:
        gt = ds.GetGeoTransform() or (0, 1, 0, 0, 0, -1)
        wkt = ds.GetProjection() or ""

        srs = osr.SpatialReference(wkt=wkt) if wkt else osr.SpatialReference()
        crs_name = srs.GetName() or "Unknown CRS"

        w, h = ds.RasterXSize, ds.RasterYSize
        nb = ds.RasterCount
        band1 = ds.GetRasterBand(1)
        dtype = gdal.GetDataTypeName(band1.DataType).lower()
        nodata = band1.GetNoDataValue()

        px = abs(gt[1])
        py = abs(gt[5])

        # World bounding box (top-left + bottom-right corners)
        corners = [
            self._gt_point(gt, 0, 0),
            self._gt_point(gt, w, 0),
            self._gt_point(gt, 0, h),
            self._gt_point(gt, w, h),
        ]
        xs = [c[0] for c in corners]
        ys = [c[1] for c in corners]
        bbox = (min(xs), min(ys), max(xs), max(ys))

        ovr_count = band1.GetOverviewCount()

        fmt = ds.GetDriver().ShortName if ds.GetDriver() else "Unknown"

        return RasterMetadata(
            path=path,
            width=w, height=h, bands=nb,
            dtype=dtype, nodata=nodata,
            geotransform=tuple(gt),
            crs_wkt=wkt, crs_name=crs_name,
            pixel_size_x=px, pixel_size_y=py,
            bbox_world=bbox,
            is_rgb=(nb >= 3),
            has_overviews=(ovr_count > 0),
            overview_count=ovr_count,
            format_name=fmt,
        )

    @staticmethod
    def _gt_point(gt, px: float, py: float) -> tuple[float, float]:
        return (gt[0] + px * gt[1] + py * gt[2],
                gt[3] + px * gt[4] + py * gt[5])

    def _detect_band_order(self, ds) -> list[int]:
        """Return 1-based band indices in R,G,B order, falling back to [1,2,3]."""
        from osgeo import gdal as _gdal
        mapping = {}
        for i in range(1, ds.RasterCount + 1):
            ci = ds.GetRasterBand(i).GetColorInterpretation()
            mapping[ci] = i
        R, G, B = _gdal.GCI_RedBand, _gdal.GCI_GreenBand, _gdal.GCI_BlueBand
        if R in mapping and G in mapping and B in mapping:
            return [mapping[R], mapping[G], mapping[B]]
        if ds.RasterCount >= 3:
            return [1, 2, 3]
        return [1]  # grayscale

    def _compute_stretch(self, ds):
        """Sample an overview (or the full raster at 1 % resolution) to find
        per-band 2nd–98th-percentile limits for display normalisation."""
        sample_w = max(256, min(2048, ds.RasterXSize // 8))
        sample_h = max(256, min(2048, ds.RasterYSize // 8))

        mins, maxs = [], []
        for bi in self._band_order:
            band = ds.GetRasterBand(bi)
            raw = band.ReadRaster(0, 0, ds.RasterXSize, ds.RasterYSize,
                                  sample_w, sample_h)
            dt = band.DataType
            np_dtype = _gdal_dtype_to_numpy(dt)
            arr = np.frombuffer(raw, dtype=np_dtype).reshape(sample_h, sample_w).astype(np.float32)
            # Mask nodata
            nd = band.GetNoDataValue()
            if nd is not None:
                arr = np.where(arr == nd, np.nan, arr)
            p2 = float(np.nanpercentile(arr, 2))
            p98 = float(np.nanpercentile(arr, 98))
            if p98 <= p2:
                p2 = float(np.nanmin(arr))
                p98 = float(np.nanmax(arr))
            if p98 <= p2:
                p2, p98 = 0.0, 255.0
            mins.append(p2)
            maxs.append(p98)

        self._stretch_min = np.array(mins, dtype=np.float32)
        self._stretch_max = np.array(maxs, dtype=np.float32)

    def _read_tile(self, tx: int, ty: int, level: int) -> Optional[np.ndarray]:
        """Read one tile from GDAL and return a uint8 (H,W,3) array or None."""
        ds = self._ds
        if ds is None:
            return None

        scale = 2 ** level
        x0 = tx * TILE_SIZE
        y0 = ty * TILE_SIZE
        x_size = min(TILE_SIZE, ds.RasterXSize - x0)
        y_size = min(TILE_SIZE, ds.RasterYSize - y0)
        if x_size <= 0 or y_size <= 0:
            return None

        out_w = max(1, x_size // scale)
        out_h = max(1, y_size // scale)

        channels = []
        for bi in self._band_order:
            band = ds.GetRasterBand(bi)
            raw = band.ReadRaster(x0, y0, x_size, y_size, out_w, out_h,
                                  resample_alg=gdal.GRIORA_Bilinear)
            if raw is None:
                return None
            np_dtype = _gdal_dtype_to_numpy(band.DataType)
            ch = np.frombuffer(raw, dtype=np_dtype).reshape(out_h, out_w).astype(np.float32)
            channels.append(ch)

        # Normalise to uint8 per band using pre-computed stretch
        rgb_channels = []
        for idx, ch in enumerate(channels):
            lo = self._stretch_min[idx] if self._stretch_min is not None else 0.0
            hi = self._stretch_max[idx] if self._stretch_max is not None else 255.0
            denom = hi - lo
            if denom < 1e-6:
                denom = 1.0
            ch8 = np.clip((ch - lo) / denom * 255.0, 0, 255).astype(np.uint8)
            rgb_channels.append(ch8)

        if len(rgb_channels) == 1:
            # Grayscale → replicate to RGB
            rgb_channels = [rgb_channels[0]] * 3

        return np.stack(rgb_channels, axis=2)  # (H, W, 3)

    @staticmethod
    def _array_to_pixmap(arr: np.ndarray) -> QPixmap:
        h, w = arr.shape[:2]
        arr = np.ascontiguousarray(arr)
        img = QImage(arr.data, w, h, w * 3, QImage.Format.Format_RGB888)
        return QPixmap.fromImage(img)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _gdal_dtype_to_numpy(gdal_type: int):
    from osgeo import gdal as _g
    mapping = {
        _g.GDT_Byte: np.uint8,
        _g.GDT_UInt16: np.uint16,
        _g.GDT_Int16: np.int16,
        _g.GDT_UInt32: np.uint32,
        _g.GDT_Int32: np.int32,
        _g.GDT_Float32: np.float32,
        _g.GDT_Float64: np.float64,
    }
    return mapping.get(gdal_type, np.uint8)
