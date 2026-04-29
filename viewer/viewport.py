"""
ViewportWidget — the central image canvas.

Responsibilities:
  - Render the raster image using a tile cache (QPixmap per tile).
  - Handle zoom (scroll wheel / ± keys) and pan (drag / arrow keys).
  - Dispatch mouse events to the active tool.
  - Paint completed measurements and the active tool's in-progress overlay.
"""

from __future__ import annotations
import math
from typing import Optional

from PyQt6.QtCore import (Qt, QRectF, QPointF, pyqtSignal, QSize)
from PyQt6.QtGui import (QPainter, QColor, QPen, QBrush, QFont,
                          QWheelEvent, QMouseEvent, QKeyEvent,
                          QPainterPath, QCursor)
from PyQt6.QtWidgets import QWidget, QSizePolicy, QApplication

from .raster import RasterLoader, TILE_SIZE, RasterMetadata
from .tools import Tool, PanTool, draw_completed, Measurement
from .geo import fmt_coord, crs_is_geographic


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ZOOM_MIN = 1e-4
ZOOM_MAX = 64.0
ZOOM_STEP = 1.25          # per scroll click
BG_COLOR  = QColor(22, 22, 26)
GRID_COLOR = QColor(60, 60, 80, 120)
SCALEBAR_COLOR = QColor(230, 230, 230)


class ViewportWidget(QWidget):
    """Canvas widget that renders a loaded raster and measurement overlays.

    Signals
    -------
    coord_changed(str)    : human-readable cursor world coordinate
    zoom_changed(float)   : current zoom level
    """

    coord_changed = pyqtSignal(str)
    zoom_changed  = pyqtSignal(float)

    # ------------------------------------------------------------------
    # Init
    # ------------------------------------------------------------------

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.raster: Optional[RasterLoader] = None

        # View state — centre in image-pixel coordinates, zoom = screen-px / image-px
        self.view_cx: float = 0.0
        self.view_cy: float = 0.0
        self.zoom: float = 1.0

        # Tools
        self._active_tool: Tool = PanTool()
        self.setCursor(self._active_tool.cursor)

        # Completed measurements (list of Measurement objects)
        self.measurements: list[Measurement] = []

        # Options
        self.show_grid: bool = False
        self.show_scalebar: bool = True
        self.show_crosshair: bool = False

        self._cursor_screen: Optional[QPointF] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_raster(self, loader: RasterLoader):
        """Attach a loaded RasterLoader and fit the image to the window."""
        self.raster = loader
        self.measurements.clear()
        self.fit_to_window()

    def set_tool(self, tool: Tool):
        if self._active_tool:
            self._active_tool.cancel()
        self._active_tool = tool
        self.setCursor(tool.cursor)
        self.update()

    def fit_to_window(self):
        """Zoom and centre so the full raster fits in the visible area."""
        if self.raster is None or self.raster.meta is None:
            return
        m = self.raster.meta
        self.view_cx = m.width / 2.0
        self.view_cy = m.height / 2.0
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            self.zoom = 1.0
        else:
            self.zoom = min(w / max(m.width, 1), h / max(m.height, 1)) * 0.95
        self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, self.zoom))
        self.zoom_changed.emit(self.zoom)
        self.update()

    def zoom_by(self, factor: float, screen_anchor: Optional[QPointF] = None):
        """Zoom in/out around an optional screen-space anchor point."""
        if screen_anchor is not None:
            # Keep the image point under the cursor stationary
            ix, iy = self.screen_to_image(screen_anchor.x(), screen_anchor.y())
        new_zoom = max(ZOOM_MIN, min(ZOOM_MAX, self.zoom * factor))
        if screen_anchor is not None:
            # After zoom change, recalculate centre so anchor stays fixed
            self.view_cx = ix - (screen_anchor.x() - self.width()  / 2) / new_zoom
            self.view_cy = iy - (screen_anchor.y() - self.height() / 2) / new_zoom
        self.zoom = new_zoom
        self.zoom_changed.emit(self.zoom)
        self.update()

    def clear_measurements(self):
        self.measurements.clear()
        if self._active_tool:
            self._active_tool.cancel()
        self.update()

    # ------------------------------------------------------------------
    # Coordinate helpers
    # ------------------------------------------------------------------

    def screen_to_image(self, sx: float, sy: float) -> tuple[float, float]:
        """Screen px → image px (fractional)."""
        cx, cy = self.width() / 2.0, self.height() / 2.0
        px = (sx - cx) / self.zoom + self.view_cx
        py = (sy - cy) / self.zoom + self.view_cy
        return px, py

    def image_to_screen(self, px: float, py: float) -> tuple[float, float]:
        """Image px → screen px."""
        cx, cy = self.width() / 2.0, self.height() / 2.0
        sx = (px - self.view_cx) * self.zoom + cx
        sy = (py - self.view_cy) * self.zoom + cy
        return sx, sy

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, self.zoom < 1.0)

        # Background
        painter.fillRect(self.rect(), BG_COLOR)

        if self.raster is None or self.raster.meta is None:
            self._draw_placeholder(painter)
            painter.end()
            return

        self._draw_tiles(painter)

        if self.show_grid:
            self._draw_grid(painter)

        # Completed measurements
        draw_completed(painter, self.measurements, self)

        # Active tool in-progress overlay
        if self._active_tool:
            self._active_tool.draw(painter, self)

        if self.show_crosshair and self._cursor_screen:
            self._draw_crosshair(painter)

        if self.show_scalebar:
            self._draw_scalebar(painter)

        painter.end()

    def _draw_placeholder(self, painter: QPainter):
        painter.setPen(QColor(90, 90, 100))
        painter.setFont(QFont("Segoe UI", 14))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                         "Open a raster file to begin\n(File → Open  or  Ctrl+O)")

    def _draw_tiles(self, painter: QPainter):
        m = self.raster.meta
        w, h = self.width(), self.height()

        # Visible image region
        x0_img, y0_img = self.screen_to_image(0, 0)
        x1_img, y1_img = self.screen_to_image(w, h)
        x0 = max(0, int(x0_img))
        y0 = max(0, int(y0_img))
        x1 = min(m.width,  int(x1_img) + 1)
        y1 = min(m.height, int(y1_img) + 1)

        level = RasterLoader.get_overview_level(self.zoom)
        scale = 2 ** level   # how many native px per tile-pixel at this level

        # Tile indices (in native image-px space)
        tx0, ty0 = x0 // TILE_SIZE, y0 // TILE_SIZE
        tx1 = (x1 + TILE_SIZE - 1) // TILE_SIZE
        ty1 = (y1 + TILE_SIZE - 1) // TILE_SIZE

        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                pixmap = self.raster.get_tile(tx, ty, level)
                if pixmap is None:
                    continue

                # Tile origin in image pixels
                img_x0 = tx * TILE_SIZE
                img_y0 = ty * TILE_SIZE
                # Tile origin on screen
                sx0, sy0 = self.image_to_screen(img_x0, img_y0)
                # Actual image pixels covered by this tile
                actual_w = min(TILE_SIZE, m.width  - img_x0)
                actual_h = min(TILE_SIZE, m.height - img_y0)
                # Screen size of this tile
                sw = actual_w * self.zoom
                sh = actual_h * self.zoom

                target = QRectF(sx0, sy0, sw, sh)
                painter.drawPixmap(target, pixmap, QRectF(pixmap.rect()))

    def _draw_grid(self, painter: QPainter):
        """Draw a simple grid aligned to round CRS coordinates."""
        if self.raster is None:
            return
        m = self.raster.meta

        # Choose a grid spacing that gives ~4-10 grid lines on screen
        screen_span_x, _ = self.screen_to_image(self.width(), 0)
        screen_span_x -= self.view_cx - self.width() / 2 / self.zoom
        world_span = abs(screen_span_x * m.pixel_size_x * 2)
        step = _nice_step(world_span / 6)
        if step <= 0:
            return

        painter.setPen(QPen(GRID_COLOR, 1, Qt.PenStyle.DotLine))

        x0_w, y0_w = self.raster.pixel_to_world(*self.screen_to_image(0, 0))
        x1_w, y1_w = self.raster.pixel_to_world(*self.screen_to_image(self.width(), self.height()))

        import math as _m
        gx = _m.floor(min(x0_w, x1_w) / step) * step
        while gx <= max(x0_w, x1_w) + step:
            px, _ = self.raster.world_to_pixel(gx, 0)
            sx, _ = self.image_to_screen(px, 0)
            painter.drawLine(int(sx), 0, int(sx), self.height())
            gx += step

        gy = _m.floor(min(y0_w, y1_w) / step) * step
        while gy <= max(y0_w, y1_w) + step:
            _, py = self.raster.world_to_pixel(0, gy)
            _, sy = self.image_to_screen(0, py)
            painter.drawLine(0, int(sy), self.width(), int(sy))
            gy += step

    def _draw_scalebar(self, painter: QPainter):
        """Draw a simple scale bar in the bottom-left corner."""
        if self.raster is None:
            return
        m = self.raster.meta
        if m.pixel_size_x <= 0:
            return

        # Target screen length: ~150 px → compute map metres
        target_px = 150.0
        metres_per_screen_px = m.pixel_size_x / self.zoom
        # round to nice value
        raw_m = target_px * metres_per_screen_px
        bar_m = _nice_step(raw_m)
        if bar_m <= 0:
            return
        bar_px = bar_m / metres_per_screen_px

        x = 20
        y = self.height() - 24
        painter.setPen(QPen(SCALEBAR_COLOR, 2))
        painter.drawLine(x, y, int(x + bar_px), y)
        painter.drawLine(x, y - 5, x, y + 5)
        painter.drawLine(int(x + bar_px), y - 5, int(x + bar_px), y + 5)

        label = _fmt_bar(bar_m)
        painter.setFont(QFont("Segoe UI", 8))
        painter.drawText(int(x + bar_px / 2) - 30, y - 8, 60, 16,
                         Qt.AlignmentFlag.AlignCenter, label)

    def _draw_crosshair(self, painter: QPainter):
        p = self._cursor_screen
        painter.setPen(QPen(QColor(255, 80, 80, 160), 1, Qt.PenStyle.DashLine))
        painter.drawLine(0, int(p.y()), self.width(), int(p.y()))
        painter.drawLine(int(p.x()), 0, int(p.x()), self.height())

    # ------------------------------------------------------------------
    # Mouse / keyboard events
    # ------------------------------------------------------------------

    def wheelEvent(self, event: QWheelEvent):
        delta = event.angleDelta().y()
        factor = ZOOM_STEP if delta > 0 else 1.0 / ZOOM_STEP
        self.zoom_by(factor, event.position())

    def mousePressEvent(self, event: QMouseEvent):
        # Middle-button always pans
        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan_start = event.position()
            self._pan_start_cx = self.view_cx
            self._pan_start_cy = self.view_cy
            QApplication.setOverrideCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if self._active_tool:
            self._active_tool.on_press(event, self)

    def mouseMoveEvent(self, event: QMouseEvent):
        self._cursor_screen = event.position()

        # Middle-button pan
        if event.buttons() & Qt.MouseButton.MiddleButton and hasattr(self, '_pan_start'):
            dx = event.position().x() - self._pan_start.x()
            dy = event.position().y() - self._pan_start.y()
            self.view_cx = self._pan_start_cx - dx / self.zoom
            self.view_cy = self._pan_start_cy - dy / self.zoom
            self.update()
            return

        if self._active_tool:
            self._active_tool.on_move(event, self)

        # Emit world coordinate
        if self.raster:
            px, py = self.screen_to_image(event.position().x(), event.position().y())
            wx, wy = self.raster.pixel_to_world(px, py)
            is_geo = crs_is_geographic(self.raster.meta.crs_wkt)
            self.coord_changed.emit(fmt_coord(wx, wy, is_geo))

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.MiddleButton:
            QApplication.restoreOverrideCursor()
            return
        if self._active_tool:
            self._active_tool.on_release(event, self)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if self._active_tool:
            self._active_tool.on_double_click(event, self)

    def keyPressEvent(self, event: QKeyEvent):
        key = event.key()
        if key == Qt.Key.Key_Plus or key == Qt.Key.Key_Equal:
            self.zoom_by(ZOOM_STEP)
        elif key == Qt.Key.Key_Minus:
            self.zoom_by(1.0 / ZOOM_STEP)
        elif key == Qt.Key.Key_0:
            self.fit_to_window()
        elif key == Qt.Key.Key_Escape:
            if self._active_tool:
                self._active_tool.cancel()
            self.update()
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right,
                     Qt.Key.Key_Up,   Qt.Key.Key_Down):
            step = max(1.0, 50.0 / self.zoom)
            if key == Qt.Key.Key_Left:  self.view_cx -= step
            if key == Qt.Key.Key_Right: self.view_cx += step
            if key == Qt.Key.Key_Up:    self.view_cy -= step
            if key == Qt.Key.Key_Down:  self.view_cy += step
            self.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Keep the same world centre visible after resize
        self.update()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nice_step(value: float) -> float:
    """Round value up to a "nice" number (1, 2, 5 × 10^n)."""
    if value <= 0:
        return 0.0
    exp = math.floor(math.log10(value))
    frac = value / (10 ** exp)
    for nice in (1, 2, 5, 10):
        if frac <= nice:
            return nice * (10 ** exp)
    return 10 ** (exp + 1)


def _fmt_bar(metres: float) -> str:
    if metres >= 1000:
        v = metres / 1000
        return f"{v:g} km"
    return f"{metres:g} m"
