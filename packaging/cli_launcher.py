"""exe 启动器（命令行版）：mpc chat / mpc serve / mpc selftest …"""

from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    multiprocessing.freeze_support()
    from mpc.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    sys.exit(main())
