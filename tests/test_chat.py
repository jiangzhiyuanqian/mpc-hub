"""控制台对话测试：历史、治理、平台字段一致性。"""

from __future__ import annotations

from pathlib import Path

from mpc.agent.chat import CONSOLE_PLATFORM, ConsoleChat
from mpc.agent.llm import MockLLM
from mpc.governance import Governance, GovernanceConfig
from mpc.store import Store


def _chat(tmp_path: Path) -> ConsoleChat:
    store = Store(str(tmp_path / "chat.db"))
    gov = Governance(store, GovernanceConfig())
    return ConsoleChat(store, gov, MockLLM())


def test_reply_records_both_sides(tmp_path: Path) -> None:
    chat = _chat(tmp_path)
    result = chat.reply("我要退款")
    assert result["status"] in {"ok", "dropped", "needs_approval"}
    history = chat.history(limit=10)
    assert len(history) >= 1
    directions = {row.get("direction") for row in history}
    assert "inbound" in directions


def test_platform_field_matches_history_lookup(tmp_path: Path) -> None:
    """入站落库的 platform 必须与查历史用的 platform 一致，否则历史查不到。"""
    chat = _chat(tmp_path)
    chat.reply("你好")
    rows = chat.store.conversation_history(CONSOLE_PLATFORM, chat.conversation_id, limit=5)
    assert rows, "按 CONSOLE_PLATFORM 查不到历史"


def test_owner_greeting_still_gets_a_reply(tmp_path: Path) -> None:
    """主人在控制台主动输入就该有回音。

    注意跟入站客户消息的区别：客户群发"你好"会被 governance 的 drop_ping 拦掉
    （那是 check_inbound 的事）；这里走的是 check_outbound，主人主动问了就得答。
    """
    chat = _chat(tmp_path)
    result = chat.reply("你好")
    assert result["status"] == "ok"
    assert result.get("reply")


def test_empty_input_is_rejected(tmp_path: Path) -> None:
    chat = _chat(tmp_path)
    assert chat.reply("   ")["status"] == "empty"
