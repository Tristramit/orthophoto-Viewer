# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_all, collect_data_files

# ── GDAL / PROJ data directories ─────────────────────────────────────────────
# These env vars are set by conda when the environment is activated.
gdal_data = os.environ.get('GDAL_DATA', '')
proj_lib  = os.environ.get('PROJ_LIB', '')

datas = []
if gdal_data and os.path.isdir(gdal_data):
    datas += [(gdal_data, 'osgeo/data/gdal')]
if proj_lib and os.path.isdir(proj_lib):
    datas += [(proj_lib, 'pyproj/proj_dir/share/proj')]

# ── Collect osgeo (GDAL Python bindings + native DLLs) ───────────────────────
osgeo_datas, osgeo_binaries, osgeo_hiddenimports = collect_all('osgeo')
datas    += osgeo_datas
binaries  = osgeo_binaries

# ── Collect pyproj ────────────────────────────────────────────────────────────
pyproj_datas, pyproj_binaries, pyproj_hiddenimports = collect_all('pyproj')
datas    += pyproj_datas
binaries += pyproj_binaries

# ── Hidden imports ────────────────────────────────────────────────────────────
hidden_imports = (
    osgeo_hiddenimports
    + pyproj_hiddenimports
    + [
        # FastAPI / uvicorn (web_server.py — imported at runtime)
        'fastapi',
        'uvicorn',
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'starlette',
        'starlette.routing',
        'starlette.responses',
        'starlette.staticfiles',
        'anyio',
        'anyio._backends._asyncio',
        'multipart',
        'python_multipart',
        # numpy / PIL
        'numpy',
        'PIL',
        'PIL.Image',
    ]
)

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Drop unused Qt modules to keep size down
        'PyQt6.QtWebEngineWidgets',
        'PyQt6.QtWebEngineCore',
        'PyQt6.Qt3DCore',
        'PyQt6.Qt3DRender',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='OrthoViewer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='OrthoViewer',
)
