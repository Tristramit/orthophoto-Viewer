# -*- coding: utf-8 -*-
"""
Ortho Viewer — lightweight orthophoto viewer
=========================================
Main entry point and application window.

Usage:
    python main.py [file]

Keyboard shortcuts:
    Ctrl+O      Open file
    0           Fit image to window
    + / -       Zoom in / out
    P           Pan tool
    D           Distance measurement
    A           Area measurement
    Escape      Cancel active measurement
    Delete      Clear all measurements
    G           Toggle grid overlay
    S           Toggle scale bar
    C           Toggle crosshair
    Ctrl+C      Copy last measurement to clipboard
    Ctrl+Q      Quit
"""

from __future__ import annotations
import sys
import os

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QFileDialog, QDockWidget,
    QTextEdit, QStatusBar, QToolBar, QLabel, QWidget,
    QSizePolicy, QMessageBox, QVBoxLayout,
)
from PyQt6.QtGui import (
    QAction, QIcon, QKeySequence, QPalette, QColor, QFont, QPixmap
)
from PyQt6.QtCore import Qt, QSettings, QSize

from viewer.raster import RasterLoader
from viewer.viewport import ViewportWidget
from viewer.tools import PanTool, MeasureDistanceTool, MeasureAreaTool


# ---------------------------------------------------------------------------
# Dark palette
# ---------------------------------------------------------------------------

def _apply_dark_palette(app: QApplication):
    app.setStyle("Fusion")
    p = QPalette()
    base    = QColor(28,  28,  32)
    alt     = QColor(36,  36,  42)
    text    = QColor(220, 220, 225)
    mid     = QColor(55,  55,  65)
    bright  = QColor(90,  90, 110)
    hl      = QColor(58, 130, 246)   # blue accent
    hl_text = QColor(255, 255, 255)
    disabled= QColor(110, 110, 120)

    p.setColor(QPalette.ColorRole.Window,          base)
    p.setColor(QPalette.ColorRole.WindowText,       text)
    p.setColor(QPalette.ColorRole.Base,             alt)
    p.setColor(QPalette.ColorRole.AlternateBase,    base)
    p.setColor(QPalette.ColorRole.ToolTipBase,      mid)
    p.setColor(QPalette.ColorRole.ToolTipText,      text)
    p.setColor(QPalette.ColorRole.Text,             text)
    p.setColor(QPalette.ColorRole.Button,           mid)
    p.setColor(QPalette.ColorRole.ButtonText,       text)
    p.setColor(QPalette.ColorRole.BrightText,       QColor(255, 100, 100))
    p.setColor(QPalette.ColorRole.Link,             hl)
    p.setColor(QPalette.ColorRole.Highlight,        hl)
    p.setColor(QPalette.ColorRole.HighlightedText,  hl_text)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text,       disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled)
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled)
    app.setPalette(p)

    app.setStyleSheet("""
        QToolBar { border: none; spacing: 4px; padding: 2px; }
        QToolButton {
            border: 1px solid transparent;
            border-radius: 4px;
            padding: 4px 8px;
            font-size: 12px;
        }
        QToolButton:hover  { background: rgba(255,255,255,0.08); border-color: rgba(255,255,255,0.15); }
        QToolButton:checked { background: rgba(58,130,246,0.25); border-color: rgba(58,130,246,0.6); }
        QStatusBar { border-top: 1px solid #333; font-size: 11px; }
        QDockWidget::title { background: #2a2a32; padding: 4px; font-weight: bold; }
        QTextEdit { font-family: "Consolas","Cascadia Code","Courier New",monospace; font-size: 11px; }
        QMenuBar::item:selected { background: rgba(255,255,255,0.1); }
        QMenu::item:selected { background: rgba(58,130,246,0.4); }
        QSplitter::handle { background: #333; }
    """)


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ortho Viewer")
        self.resize(1280, 800)
        self.setMinimumSize(800, 500)

        self._loader = RasterLoader()
        self._settings = QSettings("OrthoViewer", "OrthoViewer")
        self.setAcceptDrops(True)

        self._build_viewport()
        self._build_toolbar()
        self._build_metadata_dock()
        self._build_menus()
        self._build_status_bar()

        self._viewport.coord_changed.connect(self._on_coord_changed)
        self._viewport.zoom_changed.connect(self._on_zoom_changed)

        # Restore recent geometry
        if self._settings.value("geometry"):
            self.restoreGeometry(self._settings.value("geometry"))

        # Auto-open if file passed on command line
        args = sys.argv[1:]
        if args and os.path.isfile(args[0]):
            self._open_file(args[0])

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_viewport(self):
        self._viewport = ViewportWidget(self)
        self.setCentralWidget(self._viewport)

    def _build_toolbar(self):
        tb = QToolBar("Tools", self)
        tb.setMovable(False)
        tb.setIconSize(QSize(18, 18))
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, tb)

        # Tool group (exclusive)
        self._act_pan  = self._tool_action(tb, "✋ Pan",      "P",  "Pan / Navigate (P)")
        self._act_dist = self._tool_action(tb, "📏 Distance", "D",  "Measure distance (D)")
        self._act_area = self._tool_action(tb, "⬡ Area",     "A",  "Measure area (A)")

        for act in (self._act_pan, self._act_dist, self._act_area):
            act.setCheckable(True)
        self._act_pan.setChecked(True)

        self._act_pan.triggered.connect(self._use_pan)
        self._act_dist.triggered.connect(self._use_distance)
        self._act_area.triggered.connect(self._use_area)

        tb.addSeparator()

        act_fit = QAction("⊞ Fit", self)
        act_fit.setToolTip("Fit image to window (0)")
        act_fit.setShortcut(QKeySequence("0"))
        act_fit.triggered.connect(self._viewport.fit_to_window)
        tb.addAction(act_fit)

        act_clear = QAction("🗑 Clear", self)
        act_clear.setToolTip("Clear all measurements (Delete)")
        act_clear.setShortcut(QKeySequence("Delete"))
        act_clear.triggered.connect(self._viewport.clear_measurements)
        tb.addAction(act_clear)

        tb.addSeparator()

        self._act_grid = QAction("# Grid", self)
        self._act_grid.setToolTip("Toggle grid overlay (G)")
        self._act_grid.setShortcut(QKeySequence("G"))
        self._act_grid.setCheckable(True)
        self._act_grid.toggled.connect(self._toggle_grid)
        tb.addAction(self._act_grid)

        self._act_xhair = QAction("+ Xhair", self)
        self._act_xhair.setToolTip("Toggle crosshair (C)")
        self._act_xhair.setShortcut(QKeySequence("C"))
        self._act_xhair.setCheckable(True)
        self._act_xhair.setChecked(False)
        self._act_xhair.toggled.connect(self._toggle_crosshair)
        tb.addAction(self._act_xhair)

    def _tool_action(self, toolbar, label, shortcut, tip) -> QAction:
        act = QAction(label, self)
        act.setToolTip(f"{tip}")
        act.setShortcut(QKeySequence(shortcut))
        toolbar.addAction(act)
        return act

    def _build_menus(self):
        mb = self.menuBar()

        # File
        file_menu = mb.addMenu("&File")
        act_open = QAction("&Open…", self)
        act_open.setShortcut(QKeySequence.StandardKey.Open)
        act_open.triggered.connect(self._open_dialog)
        file_menu.addAction(act_open)

        self._recent_menu = file_menu.addMenu("Recent Files")
        self._refresh_recent_menu()

        file_menu.addSeparator()

        act_copy = QAction("&Copy Measurement", self)
        act_copy.setShortcut(QKeySequence.StandardKey.Copy)
        act_copy.triggered.connect(self._copy_measurement)
        file_menu.addAction(act_copy)

        file_menu.addSeparator()

        act_quit = QAction("&Quit", self)
        act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        # View
        view_menu = mb.addMenu("&View")
        act_fit = QAction("Fit to &Window", self)
        act_fit.setShortcut(QKeySequence("0"))
        act_fit.triggered.connect(self._viewport.fit_to_window)
        view_menu.addAction(act_fit)
        view_menu.addAction(self._act_grid)
        view_menu.addAction(self._act_xhair)

        act_meta = QAction("&Metadata Panel", self)
        act_meta.setCheckable(True)
        act_meta.setChecked(True)
        act_meta.triggered.connect(self._dock.setVisible)
        view_menu.addAction(act_meta)

        # Help
        help_menu = mb.addMenu("&Help")
        act_about = QAction("&About", self)
        act_about.triggered.connect(self._show_about)
        help_menu.addAction(act_about)

    def _build_status_bar(self):
        sb = self.statusBar()

        self._lbl_coord = QLabel("No file loaded")
        self._lbl_coord.setMinimumWidth(320)
        sb.addWidget(self._lbl_coord)

        sb.addPermanentWidget(QLabel("  "))  # spacer

        self._lbl_zoom = QLabel("Zoom: —")
        self._lbl_zoom.setMinimumWidth(100)
        sb.addPermanentWidget(self._lbl_zoom)

        self._lbl_tool = QLabel("Tool: Pan")
        self._lbl_tool.setMinimumWidth(120)
        sb.addPermanentWidget(self._lbl_tool)

    def _build_metadata_dock(self):
        self._dock = QDockWidget("Metadata", self)
        self._dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea |
                                   Qt.DockWidgetArea.LeftDockWidgetArea)
        self._meta_text = QTextEdit()
        self._meta_text.setReadOnly(True)
        self._meta_text.setMinimumWidth(280)
        self._meta_text.setMaximumWidth(380)
        self._meta_text.setPlaceholderText("File metadata will appear here after opening a raster.")
        self._dock.setWidget(self._meta_text)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self._dock)

    # ------------------------------------------------------------------
    # File handling
    # ------------------------------------------------------------------

    def _open_dialog(self):
        recent = self._settings.value("last_dir", "")
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Raster",
            recent,
            "Raster files (*.tif *.tiff *.jp2 *.j2k *.png *.jpg *.jpeg *.img *.vrt);;"
            "GeoTIFF (*.tif *.tiff);;"
            "JPEG 2000 (*.jp2 *.j2k);;"
            "All files (*.*)",
        )
        if path:
            self._open_file(path)

    def _open_file(self, path: str):
        try:
            meta = self._loader.open(path)
        except Exception as exc:
            QMessageBox.critical(self, "Open Error",
                                 f"Cannot open file:\n{path}\n\n{exc}")
            return

        self._viewport.load_raster(self._loader)
        self.setWindowTitle(f"Ortho Viewer — {os.path.basename(path)}")

        # Update metadata panel
        self._meta_text.setPlainText("\n".join(meta.summary_lines()))

        # Save to recent
        self._settings.setValue("last_dir", os.path.dirname(path))
        recents = self._settings.value("recent_files", []) or []
        if path in recents:
            recents.remove(path)
        recents.insert(0, path)
        self._settings.setValue("recent_files", recents[:10])
        self._refresh_recent_menu()

    def _refresh_recent_menu(self):
        self._recent_menu.clear()
        recents = self._settings.value("recent_files", []) or []
        for p in recents:
            act = QAction(os.path.basename(p), self)
            act.setToolTip(p)
            act.setData(p)
            act.triggered.connect(lambda checked, path=p: self._open_file(path))
            self._recent_menu.addAction(act)
        if not recents:
            empty = QAction("(none)", self)
            empty.setEnabled(False)
            self._recent_menu.addAction(empty)

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def _set_tool_exclusive(self, act: QAction):
        for a in (self._act_pan, self._act_dist, self._act_area):
            a.setChecked(a is act)

    def _use_pan(self):
        self._set_tool_exclusive(self._act_pan)
        self._viewport.set_tool(PanTool())
        self._lbl_tool.setText("Tool: Pan")

    def _use_distance(self):
        self._set_tool_exclusive(self._act_dist)
        self._viewport.set_tool(MeasureDistanceTool())
        self._lbl_tool.setText("Tool: Distance  (dbl-click to finish)")

    def _use_area(self):
        self._set_tool_exclusive(self._act_area)
        self._viewport.set_tool(MeasureAreaTool())
        self._lbl_tool.setText("Tool: Area  (dbl-click to finish)")

    def _toggle_grid(self, on: bool):
        self._viewport.show_grid = on
        self._viewport.update()

    def _toggle_crosshair(self, on: bool):
        self._viewport.show_crosshair = on
        self._viewport.update()

    # ------------------------------------------------------------------
    # Status bar slots
    # ------------------------------------------------------------------

    def _on_coord_changed(self, text: str):
        self._lbl_coord.setText(text)

    def _on_zoom_changed(self, zoom: float):
        self._lbl_zoom.setText(f"Zoom: {zoom * 100:.1f}%")

    # ------------------------------------------------------------------
    # Clipboard
    # ------------------------------------------------------------------

    def _copy_measurement(self):
        measurements = self._viewport.measurements
        if not measurements:
            return
        last = measurements[-1]
        QApplication.clipboard().setText(last.label)
        self.statusBar().showMessage(f"Copied: {last.label}", 3000)

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def _show_about(self):
        QMessageBox.about(self, "About Ortho Viewer",
            "<h3>Ortho Viewer</h3>"
            "<p>A lightweight desktop viewer for georeferenced orthophotos.</p>"
            "<p><b>Supported formats:</b> GeoTIFF, JPEG 2000, PNG/JPEG with world files</p>"
            "<p><b>Libraries:</b> PyQt6 · GDAL · NumPy · pyproj</p>"
            "<hr>"
            "<p><small>Mouse wheel: zoom &nbsp;|&nbsp; Drag: pan &nbsp;|&nbsp; "
            "P/D/A: tool switch &nbsp;|&nbsp; 0: fit &nbsp;|&nbsp; "
            "Dbl-click: finish measurement &nbsp;|&nbsp; Del: clear</small></p>"
        )

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            self._open_file(urls[0].toLocalFile())

    def closeEvent(self, event):
        self._settings.setValue("geometry", self.saveGeometry())
        self._loader.close()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    # High-DPI support
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setApplicationName("OrthoViewer")
    app.setOrganizationName("OrthoViewer")

    _apply_dark_palette(app)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
