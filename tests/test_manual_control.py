"""手动接管 / 建议模式 / 会话列表的测试。

覆盖桌面端新加的三件事：
1. 主人自己敲一条 → 能投递到平台，并落库；
2. 主人的消息也要过出站闸（密钥泄漏照样拦）；
3. 手动接管模式下 agent 只给建议，不投递。
"""

from __future__ import annotations

import time
from pathlib import Path

from mpc.agent.llm import MockLLM
from mpc.connectors import LoopbackConnector, Registry
from mpc.contracts import ConversationKind, Envelope, Platform
from mpc.governance import Governance, GovernanceConfig
from mpc.hub import Hub
from mpc.store import Store


def _hub(tmp_path: Path, **gov_kwargs) -> Hub:
    store = Store(str(tmp_path / "manual.db"))
    gov = Governance(store, GovernanceConfig(**gov_kwargs))
    registry = Registry()
    registry.register(LoopbackConnector(), "loopback")
    return Hub(store=store, governance=gov, llm=MockLLM(), registry=registry)


def _env(text: str, message_id: str = "m1") -> Envelope:
    return Envelope(
        message_id=message_id,
        platform=Platform.LOOPBACK,
        conversation_id="c1",
        conversation_kind=ConversationKind.PRIVATE,
        sender_id="customer-1",
        sender_name="客户甲",
        text=text,
        timestamp=time.time(),
    )


def test_manual_send_is_delivered_and_recorded(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    res = hub.send_manual("loopback", "c1", "您好，这边人工回复您")
    assert res["ok"] is True, res
    rows = hub.store.conversation_history("loopback", "c1", limit=10)
    outbound = [r for r in rows if r["direction"] == "outbound"]
    assert outbound, "手动发送没有落库"
    assert outbound[-1]["text"] == "您好，这边人工回复您"


def test_manual_send_is_blocked_when_text_leaks_a_key(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    res = hub.send_manual("loopback", "c1", "这是 sk-abcdefghijklmnop123456 给您")
    assert res["ok"] is False
    assert res["status"] == "blocked"
    rows = hub.store.conversation_history("loopback", "c1", limit=10)
    assert not [r for r in rows if r["direction"] == "outbound"], "被拦下的消息不该发出去"


def test_empty_manual_message_is_rejected(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    assert hub.send_manual("loopback", "c1", "   ")["ok"] is False


def test_suggest_mode_does_not_deliver(tmp_path: Path) -> None:
    """手动接管：agent 起草但**不发送**，只把草稿交给主人。"""
    hub = _hub(tmp_path, require_approval_on_commitment=False)
    res = hub.ingest(_env("我的订单什么时候发货？"), auto_send=False)
    assert res["status"] == "suggested", res
    rows = hub.store.conversation_history("loopback", "c1", limit=10)
    assert not [r for r in rows if r["direction"] == "outbound"], "手动接管模式下不该自动发送"


def test_auto_mode_still_delivers(tmp_path: Path) -> None:
    hub = _hub(tmp_path, require_approval_on_commitment=False)
    res = hub.ingest(_env("我的订单什么时候发货？"))
    assert res["status"] == "sent", res
    rows = hub.store.conversation_history("loopback", "c1", limit=10)
    assert [r for r in rows if r["direction"] == "outbound"], "自动模式下应该已经发出去了"


def test_list_conversations_orders_by_recent(tmp_path: Path) -> None:
    hub = _hub(tmp_path)
    hub.store.record_inbound(_env("第一条", "a1"))
    hub.send_manual("loopback", "c2", "第二条会话")
    rows = hub.store.list_conversations(platform="loopback")
    ids = [r["conversation_id"] for r in rows]
    assert "c1" in ids and "c2" in ids
    assert rows[0]["conversation_id"] == "c2", "最近的会话应该排最前"
    assert rows[0]["message_count"] >= 1
    assert rows[0]["last_text"]
