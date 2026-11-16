# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：onedir 布局，两个 exe 共享同一份依赖。

为什么是 onedir（而不是 onefile）：
    onefile 每次启动都要把 50MB 依赖解压到 %TEMP%，实测多花 2.8 秒；
    onedir 直接用旁边的 _internal/ 文件夹，启动即用。

产物：
    dist/MPC-Hub/
        MPC-Hub.exe   图形版，双击直接开桌面端（无黑框）
        mpc.exe       命令行版（mpc chat / mpc serve / mpc selftest）
        _internal/    两个 exe 共享的依赖（约 50MB，只存一份）

打包：
    pip install -e ".[gui,dev]"
    python -m PyInstaller --noconfirm --clean packaging/mpc-hub.spec
"""

from pathlib import Path

ROOT = Path(SPECPATH).parent          # packaging/ 的上一级 = 工程根
SRC = str(ROOT / "src")
PACK = str(ROOT / "packaging")

# 用不到的 Qt 模块一律剔掉，体积能小一大截
QT_EXCLUDES = [
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
]

COMMON_EXCLUDES = [
    "tkinter",
    "unittest",
    "pydoc_data",
    "test",
]

# 通用隐藏依赖
BASE_HIDDEN = [
    "mpc",
    "mpc.cli",
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


def _analysis(script: str, hidden: list[str], excludes: list[str]):
    return Analysis(  # noqa: F821 - PyInstaller 注入的全局名
        [script],
        pathex=[SRC],
        binaries=[],
        datas=[],
        hiddenimports=hidden,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=excludes,
        noarchive=False,
        optimize=0,
    )


# ---------- 图形版：带 Qt ----------
gui = _analysis(
    str(Path(PACK) / "gui_launcher.py"),
    BASE_HIDDEN + ["mpc.desktop_qt", "mpc.desktop"],
    QT_EXCLUDES + COMMON_EXCLUDES,
)
pyz_gui = PYZ(gui.pure)  # noqa: F821
exe_gui = EXE(  # noqa: F821
    pyz_gui,
    gui.scripts,
    [],
    exclude_binaries=True,
    name="MPC-Hub",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# ---------- 命令行版：不含 Qt，省 40MB ----------
cli = _analysis(
    str(Path(PACK) / "cli_launcher.py"),
    BASE_HIDDEN,
    ["PySide6"] + QT_EXCLUDES + COMMON_EXCLUDES,
)
pyz_cli = PYZ(cli.pure)  # noqa: F821
exe_cli = EXE(  # noqa: F821
    pyz_cli,
    cli.scripts,
    [],
    exclude_binaries=True,
    name="mpc",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# ---------- 两个 exe 合进一个目录，依赖只存一份 ----------
# 两个 Analysis 会算出同一批二进制（python dll、stdlib .pyd 等），
# 按目标名去重，保留图形版那份，避免 COLLECT 报同名冲突。
_gui_names = {entry[0] for entry in gui.binaries}
_gui_data_names = {entry[0] for entry in gui.datas}
cli_binaries = [entry for entry in cli.binaries if entry[0] not in _gui_names]
cli_datas = [entry for entry in cli.datas if entry[0] not in _gui_data_names]

coll = COLLECT(  # noqa: F821
    exe_gui,
    exe_cli,
    gui.binaries,
    gui.datas,
    cli_binaries,
    cli_datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MPC-Hub",
)
