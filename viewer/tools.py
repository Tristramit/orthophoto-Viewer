"""
Interactive tools: Pan, Measure Distance, Measure Area.

Each tool receives mouse events from ViewportWidget and paints its
in-progress state via its draw() method.  Completed measurements are
stored as Measurement objects inside the viewport.
"""

from __future__ import annotations
import math
from dataclasses import dataclass, field
from typing import Optional, Sequence
from enum import Enum, auto

from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import (QPainter, QColor, QPen, QBrush, QFont,
                          QCursor, QPolygonF, QPainterPath)
from PyQt6.QtWidgets import QApplication

from .geo import (geodesic_distance_m, geodesic_area_m2,
                  fmt_distance, fmt_area, fmt_coord)


# ---------------------------------------------------------------------------
# Measurement result
# ---------------------------------------------------------------------------

class MeasureType(Enum):
    DISTANCE = auto()
    AREA = auto()


@dataclass
class Measurement:
    kind: MeasureType
    world_points: list[tuple[float, float]]  # (wx, wy) in raster CRS
    value: float                              # metres or m²
    label: str                                # formatted string

    def label_world_pos(self) -> tuple[float, float]:
        """Return world coords where the label should be drawn."""
        if not self.world_points:
            return 0.0, 0.0
        xs = [p[0] for p in self.world_points]
        ys = [p[1] for p in self.world_points]
        return sum(xs) / len(xs), sum(ys) / len(ys)


# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------

_CLR_LINE   = QColor(255, 220,  40)   # yellow
_CLR_FILL   = QColor(255, 220,  40,  45)
_CLR_DONE   = QColor( 80, 200, 120)   # green for completed
_CLR_DONE_F = QColor( 80, 200, 120,  45)
_CLR_NODE   = QColor(255, 255, 255)
_CLR_LABEL_BG = QColor(20, 20, 20, 180)
_CLR_LABEL_FG = QColor(240, 240, 240)
_PEN_LINE   = QPen(_CLR_LINE,  2, Qt.PenStyle.SolidLine)
_PEN_DONE   = QPen(_CLR_DONE,  2, Qt.PenStyle.SolidLine)
_PEN_DASH   = QPen(_CLR_LINE,  1, Qt.PenStyle.DashLine)
_PEN_NODE   = QPen(_CLR_NODE,  1)
_FONT_LABEL = QFont("Segoe UI", 9)
_FONT_LABEL.setBold(True)
NODE_R = 4   # node radius px


def _w2s(vp, wx, wy):
    """World → screen conversion via viewport."""
    px, py = vp.raster.world_to_pixel(wx, wy)
    return vp.image_to_screen(px, py)


def _world_points_to_screen(vp, pts: Sequence[tuple]) -> list[QPointF]:
    return [QPointF(*_w2s(vp, wx, wy)) for wx, wy in pts]


def _draw_label(painter: QPainter, text: str, sx: float, sy: float):
    fm = painter.fontMetrics()
    tw = fm.horizontalAdvance(text)
    th = fm.height()
    pad = 4
    rx = sx - tw / 2 - pad
    ry = sy - th / 2 - pad
    painter.setBrush(QBrush(_CLR_LABEL_BG))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(int(rx), int(ry), tw + pad * 2, th + pad * 2, 4, 4)
    painter.setPen(_CLR_LABEL_FG)
    painter.drawText(int(rx + pad), int(ry + pad + fm.ascent()), text)


def _draw_nodes(painter: QPainter, screen_pts: list[QPointF]):
    painter.setPen(_PEN_NODE)
    painter.setBrush(QBrush(_CLR_NODE))
    for p in screen_pts:
        painter.drawEllipse(p, NODE_R, NODE_R)


# ---------------------------------------------------------------------------
# Base tool
# ---------------------------------------------------------------------------

class Tool:
    cursor = Qt.CursorShape.ArrowCursor

    def on_press(self, event, vp): pass
    def on_move(self, event, vp): pass
    def on_release(self, event, vp): pass
    def on_double_click(self, event, vp): pass
    def draw(self, painter: QPainter, vp): pass
    def cancel(self): pass


# ---------------------------------------------------------------------------
# Pan
# ---------------------------------------------------------------------------

class PanTool(Tool):
    cursor = Qt.CursorShape.OpenHandCursor

    def __init__(self):
        self._drag_start_screen = None
        self._drag_start_center = None

    def on_press(self, event, vp):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_screen = event.position()
            self._drag_start_center = (vp.view_cx, vp.view_cy)
            QApplication.setOverrideCursor(Qt.CursorShape.ClosedHandCursor)

    def on_move(self, event, vp):
        if self._drag_start_screen and event.buttons() & Qt.MouseButton.LeftButton:
            dx = event.position().x() - self._drag_start_screen.x()
            dy = event.position().y() - self._drag_start_screen.y()
            vp.view_cx = self._drag_start_center[0] - dx / vp.zoom
            vp.view_cy = self._drag_start_center[1] - dy / vp.zoom
            vp.update()

    def on_release(self, event, vp):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start_screen = None
            QApplication.restoreOverrideCursor()


# ---------------------------------------------------------------------------
# Measure Distance
# ---------------------------------------------------------------------------

class MeasureDistanceTool(Tool):
    cursor = Qt.CursorShape.CrossCursor

    def __init__(self):
        self._points: list[tuple[float, float]] = []   # world coords
        self._hover: Optional[tuple[float, float]] = None

    def cancel(self):
        self._points.clear()
        self._hover = None

    def on_press(self, event, vp):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        px, py = vp.screen_to_image(event.position().x(), event.position().y())
        wx, wy = vp.raster.pixel_to_world(px, py)
        self._points.append((wx, wy))
        vp.update()

    def on_move(self, event, vp):
        px, py = vp.screen_to_image(event.position().x(), event.position().y())
        wx, wy = vp.raster.pixel_to_world(px, py)
        self._hover = (wx, wy)
        vp.update()

    def on_double_click(self, event, vp):
        # Remove the last point added by the preceding single-click
        if self._points:
            self._points.pop()
        self._finalize(vp)

    def _finalize(self, vp):
        if len(self._points) >= 2:
            dist = geodesic_distance_m(self._points, vp.raster.meta.crs_wkt)
            m = Measurement(
                kind=MeasureType.DISTANCE,
                world_points=list(self._points),
                value=dist,
                label=fmt_distance(dist),
            )
            vp.measurements.append(m)
        self._points.clear()
        self._hover = None
        vp.update()

    def draw(self, painter: QPainter, vp):
        painter.setFont(_FONT_LABEL)
        all_pts = self._points + ([self._hover] if self._hover else [])
        if len(all_pts) < 1:
            return

        screen_pts = _world_points_to_screen(vp, all_pts)

        # Lines
        painter.setPen(_PEN_LINE)
        for i in range(len(screen_pts) - 1):
            painter.drawLine(screen_pts[i], screen_pts[i + 1])

        # Dashed line to hover
        if self._hover and self._points:
            painter.setPen(_PEN_DASH)
            painter.drawLine(screen_pts[-2], screen_pts[-1])

        # Nodes
        _draw_nodes(painter, screen_pts[:-1] if self._hover else screen_pts)

        # Running distance label
        if len(self._points) >= 2:
            dist = geodesic_distance_m(self._points, vp.raster.meta.crs_wkt)
            mid = screen_pts[len(screen_pts) // 2]
            _draw_label(painter, fmt_distance(dist), mid.x(), mid.y() - 16)

        # Segment label on last segment when hovering
        if self._hover and self._points:
            seg_dist = geodesic_distance_m(
                [self._points[-1], self._hover], vp.raster.meta.crs_wkt)
            last = screen_pts[-1]
            _draw_label(painter, fmt_distance(seg_dist), last.x(), last.y() + 16)


# ---------------------------------------------------------------------------
# Measure Area
# ---------------------------------------------------------------------------

class MeasureAreaTool(Tool):
    cursor = Qt.CursorShape.CrossCursor

    def __init__(self):
        self._points: list[tuple[float, float]] = []
        self._hover: Optional[tuple[float, float]] = None

    def cancel(self):
        self._points.clear()
        self._hover = None

    def on_press(self, event, vp):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        px, py = vp.screen_to_image(event.position().x(), event.position().y())
        wx, wy = vp.raster.pixel_to_world(px, py)
        self._points.append((wx, wy))
        vp.update()

    def on_move(self, event, vp):
        px, py = vp.screen_to_image(event.position().x(), event.position().y())
        wx, wy = vp.raster.pixel_to_world(px, py)
        self._hover = (wx, wy)
        vp.update()

    def on_double_click(self, event, vp):
        if self._points:
            self._points.pop()
        self._finalize(vp)

    def _finalize(self, vp):
        if len(self._points) >= 3:
            area = geodesic_area_m2(self._points, vp.raster.meta.crs_wkt)
            perim = geodesic_distance_m(self._points + [self._points[0]],
                                        vp.raster.meta.crs_wkt)
            label = f"{fmt_area(area)}  ({fmt_distance(perim)})"
            m = Measurement(
                kind=MeasureType.AREA,
                world_points=list(self._points),
                value=area,
                label=label,
            )
            vp.measurements.append(m)
        self._points.clear()
        self._hover = None
        vp.update()

    def draw(self, painter: QPainter, vp):
        painter.setFont(_FONT_LABEL)
        preview = self._points + ([self._hover] if self._hover else [])
        if len(preview) < 1:
            return

        screen_pts = _world_points_to_screen(vp, preview)

        # Filled polygon
        if len(screen_pts) >= 3:
            poly = QPolygonF(screen_pts)
            path = QPainterPath()
            path.addPolygon(poly)
            path.closeSubpath()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(_CLR_FILL))
            painter.drawPath(path)

        # Outline
        painter.setPen(_PEN_LINE)
        for i in range(len(screen_pts) - 1):
            painter.drawLine(screen_pts[i], screen_pts[i + 1])
        # Close with dash
        if len(self._points) >= 2 and self._hover:
            painter.setPen(_PEN_DASH)
            painter.drawLine(screen_pts[-1], screen_pts[0])

        _draw_nodes(painter, screen_pts[:-1] if self._hover else screen_pts)

        # Area label at centroid
        if len(self._points) >= 3:
            area = geodesic_area_m2(self._points, vp.raster.meta.crs_wkt)
            xs = [p.x() for p in screen_pts[:-1]]
            ys = [p.y() for p in screen_pts[:-1]]
            _draw_label(painter, fmt_area(area),
                        sum(xs) / len(xs), sum(ys) / len(ys))


# ---------------------------------------------------------------------------
# Draw completed measurements
# ---------------------------------------------------------------------------

def draw_completed(painter: QPainter, measurements: list[Measurement], vp):
    painter.setFont(_FONT_LABEL)
    for m in measurements:
        screen_pts = _world_points_to_screen(vp, m.world_points)
        if not screen_pts:
            continue

        if m.kind == MeasureType.DISTANCE:
            painter.setPen(_PEN_DONE)
            for i in range(len(screen_pts) - 1):
                painter.drawLine(screen_pts[i], screen_pts[i + 1])
            _draw_nodes(painter, screen_pts)
            if len(screen_pts) >= 2:
                mid = screen_pts[len(screen_pts) // 2]
                _draw_label(painter, m.label, mid.x(), mid.y() - 16)

        elif m.kind == MeasureType.AREA:
            if len(screen_pts) >= 3:
                poly = QPolygonF(screen_pts)
                path = QPainterPath()
                path.addPolygon(poly)
                path.closeSubpath()
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QBrush(_CLR_DONE_F))
                painter.drawPath(path)
                painter.setPen(_PEN_DONE)
                painter.drawPolygon(poly)
            _draw_nodes(painter, screen_pts)
            cx = sum(p.x() for p in screen_pts) / len(screen_pts)
            cy = sum(p.y() for p in screen_pts) / len(screen_pts)
            _draw_label(painter, m.label, cx, cy)
