"""
Geographic utilities: geodesic calculations, CRS helpers, unit formatting.
"""

from __future__ import annotations
import math
from typing import Sequence

try:
    from pyproj import Geod, CRS, Transformer
    _PYPROJ_OK = True
except ImportError:
    _PYPROJ_OK = False


# ---------------------------------------------------------------------------
# CRS helpers
# ---------------------------------------------------------------------------

def crs_is_geographic(wkt: str) -> bool:
    """Return True if the CRS uses angular units (degrees), False if projected."""
    if not wkt or not _PYPROJ_OK:
        return False
    try:
        return CRS.from_wkt(wkt).is_geographic
    except Exception:
        return False


def crs_units(wkt: str) -> str:
    """Return a short unit string for the horizontal axis of a CRS."""
    if not wkt or not _PYPROJ_OK:
        return "units"
    try:
        axes = CRS.from_wkt(wkt).axis_info
        if axes:
            u = axes[0].unit_name
            return {"metre": "m", "meter": "m", "degree": "°", "foot": "ft"}.get(u, u)
    except Exception:
        pass
    return "units"


def get_transformer_to_geo(wkt: str):
    """Return a Transformer that converts from the given CRS to WGS-84 lon/lat.

    Returns None if pyproj is unavailable or the CRS is already geographic.
    """
    if not _PYPROJ_OK or not wkt:
        return None
    try:
        src = CRS.from_wkt(wkt)
        dst = CRS.from_epsg(4326)
        if src == dst:
            return None
        return Transformer.from_crs(src, dst, always_xy=True)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def geodesic_distance_m(points_world: Sequence[tuple[float, float]], wkt: str) -> float:
    """Return the total length of a polyline in metres.

    points_world: list of (x, y) in the raster's CRS.
    Falls back to Euclidean distance in CRS units if pyproj is unavailable.
    """
    if len(points_world) < 2:
        return 0.0

    if _PYPROJ_OK and wkt:
        try:
            tfm = get_transformer_to_geo(wkt)
            geod = Geod(ellps="WGS84")
            total = 0.0
            prev = points_world[0]
            for pt in points_world[1:]:
                if tfm:
                    lon0, lat0 = tfm.transform(prev[0], prev[1])
                    lon1, lat1 = tfm.transform(pt[0], pt[1])
                else:
                    lon0, lat0 = prev
                    lon1, lat1 = pt
                _, _, dist = geod.inv(lon0, lat0, lon1, lat1)
                total += dist
                prev = pt
            return total
        except Exception:
            pass

    # Euclidean fallback (assumes CRS units ≈ metres)
    total = 0.0
    prev = points_world[0]
    for pt in points_world[1:]:
        dx, dy = pt[0] - prev[0], pt[1] - prev[1]
        total += math.hypot(dx, dy)
        prev = pt
    return total


def geodesic_area_m2(points_world: Sequence[tuple[float, float]], wkt: str) -> float:
    """Return the area of a polygon in square metres (always positive).

    Uses pyproj.Geod for geodesic area when possible, otherwise Shoelace formula.
    """
    if len(points_world) < 3:
        return 0.0

    if _PYPROJ_OK and wkt:
        try:
            tfm = get_transformer_to_geo(wkt)
            geod = Geod(ellps="WGS84")
            if tfm:
                lons = [tfm.transform(p[0], p[1])[0] for p in points_world]
                lats = [tfm.transform(p[0], p[1])[1] for p in points_world]
            else:
                lons = [p[0] for p in points_world]
                lats = [p[1] for p in points_world]
            area, _ = geod.polygon_area_perimeter(lons, lats)
            return abs(area)
        except Exception:
            pass

    # Shoelace fallback (CRS units assumed ≈ metres)
    n = len(points_world)
    area = 0.0
    for i in range(n):
        x1, y1 = points_world[i]
        x2, y2 = points_world[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return abs(area) / 2.0


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def fmt_distance(metres: float) -> str:
    if metres >= 1000:
        return f"{metres / 1000:.4f} km"
    return f"{metres:.2f} m"


def fmt_area(m2: float) -> str:
    if m2 >= 1_000_000:
        return f"{m2 / 1_000_000:.4f} km²"
    if m2 >= 10_000:
        return f"{m2 / 10_000:.4f} ha"
    return f"{m2:.2f} m²"


def fmt_coord(x: float, y: float, is_geo: bool) -> str:
    if is_geo:
        def _dms(v: float, pos: str, neg: str) -> str:
            hemi = pos if v >= 0 else neg
            v = abs(v)
            d = int(v)
            m = int((v - d) * 60)
            s = ((v - d) * 60 - m) * 60
            return f"{d}°{m}'{s:.2f}\"{hemi}"
        return f"{_dms(y, 'N', 'S')}  {_dms(x, 'E', 'W')}"
    return f"X: {x:,.3f}   Y: {y:,.3f}"
