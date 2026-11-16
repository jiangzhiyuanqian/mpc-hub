"""启动期不该加载的重依赖（回归测试）。

桌面端启动要快，而 LangGraph / httpx 这类库只有在真有消息进来、
真要发请求时才用得上。模块顶层 import 会让每次启动白等 1 秒以上，
所以这里锁死"延迟导入"这个行为。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SRC = str(Path(__file__).resolve().parents[1] / "src")


def _run(code: str) -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.strip().splitlines()[-1]


def test_langgraph_not_imported_until_first_message() -> None:
    """import mpc.agent.graph 不该顺带把 langgraph 拖进来。"""
    assert _run("import sys, mpc.agent.graph; print('langgraph' in sys.modules)") == "False"


def test_langgraph_available_flag_still_works() -> None:
    """公开名字 LANGGRAPH_AVAILABLE 仍然可用（PEP 562 按需计算）。"""
    out = _run("from mpc.agent.graph import LANGGRAPH_AVAILABLE; print(LANGGRAPH_AVAILABLE)")
    assert out in {"True", "False"}


def test_agent_graph_is_built_lazily() -> None:
    """造 agent 不该立刻编译图（那一步会 import langgraph）。"""
    code = (
        "import sys;"
        "from mpc.agent.graph import CustomerServiceAgent;"
        "a = CustomerServiceAgent(None, None, None);"
        "print('langgraph' in sys.modules)"
    )
    assert _run(code) == "False"
