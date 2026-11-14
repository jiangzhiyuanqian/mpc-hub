"""exe 启动器（图形版）：双击直接打开桌面端。

打包后不需要先装 Python——PyInstaller 会把运行时和依赖一起塞进 exe。
"""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    multiprocessing.freeze_support()
    try:
        from mpc.desktop_qt import launch
    except Exception as exc:  # pragma: no cover
        try:
            from mpc.desktop import launch  # type: ignore
        except Exception:
            print(f"启动失败：{exc}", file=sys.stderr)
            return 1
        return launch()
    return launch()


if __name__ == "__main__":
    sys.exit(main())
