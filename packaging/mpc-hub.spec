# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：出两个 exe，合成一个 zip 给别人下载。

- MPC-Hub.exe ：图形版，双击直接开桌面端（无黑框）
- mpc.exe     ：命令行版（mpc chat / mpc serve / mpc selftest）

打包：
    pip install -e ".[gui,dev]"
    python -m PyInstaller --noconfirm --clean packaging/mpc-hub.spec
产物在 dist/。
"""

from pathlib import Path

ROOT = Path(SPECPATH).parent          # packaging/ 的上一级 = 工程根
SRC = str(ROOT / "src")
PACK = str(ROOT / "packaging")

# 用不到的 Qt 模块一律剔掉，exe 能小一大截
EXCLUDES = [
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DExtras",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQml",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtTest",
    "PySide6.QtSql",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtBluetooth",
    "PySide6.QtNfc",
    "PySide6.QtPositioning",
    "PySide6.QtSensors",
    "PySide6.QtSerialPort",
    "PySide6.QtSerialBus",
    "PySide6.QtSpatialAudio",
    "PySide6.QtTextToSpeech",
    "PySide6.QtWebChannel",
    "PySide6.QtWebSockets",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtUiTools",
    "PySide6.QtHttpServer",
    "tkinter",
    "unittest",
    "pydoc_data",
    "test",
]

HIDDEN = [
    "mpc",
    "mpc.cli",
    "mpc.desktop_qt",
    "mpc.desktop",
    "mpc.runtime",
    "mpc.connectors",
    "mpc.agent",
    "mpc.server",
    "qrcode",
    "qrcode.image.pure",
    "pypng",
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]


def _analysis(script: str):
    return Analysis(  # noqa: F821 - PyInstaller 注入的全局名
        [script],
        pathex=[SRC],
        binaries=[],
        datas=[],
        hiddenimports=HIDDEN,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=EXCLUDES,
        noarchive=False,
        optimize=0,
    )


gui = _analysis(str(Path(PACK) / "gui_launcher.py"))
pyz_gui = PYZ(gui.pure)  # noqa: F821
exe_gui = EXE(  # noqa: F821
    pyz_gui,
    gui.scripts,
    gui.binaries,
    gui.datas,
    [],
    name="MPC-Hub",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

cli = _analysis(str(Path(PACK) / "cli_launcher.py"))
pyz_cli = PYZ(cli.pure)  # noqa: F821
exe_cli = EXE(  # noqa: F821
    pyz_cli,
    cli.scripts,
    cli.binaries,
    cli.datas,
    [],
    name="mpc",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
