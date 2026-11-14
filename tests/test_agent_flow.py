"""端到端：入站 → 快判 → 生成 → 闸门 → 发送 / 挂起审批。

用 MockLLM，不联网、不需要 API Key。
"""

from mpc.agent.llm import MockLLM
from mpc.connectors import LoopbackConnector, Registry
from mpc.contracts import Envelope, Platform
from mpc.governance import Governance, GovernanceConfig
from mpc.hub import Hub
from mpc.store import Store


def build(tmp_path) -> tuple[Hub, Store, LoopbackConnector]:
    store = Store(tmp_path / "t.db")
    loopback = LoopbackConnector()
    registry = Registry()
    registry.register(loopback, "loopback")
    gov = Governance(store, GovernanceConfig())
    hub = Hub(store=store, governance=gov, llm=MockLLM(), registry=registry)
    return hub, store, loopback


def env(text: str, mid: str = "m1", sender: str = "u1") -> Envelope:
    return Envelope(
        message_id=mid,
        platform=Platform.LOOPBACK,
        conversation_id="c1",
        sender_id=sender,
        sender_name="用户",
        text=text,
    )


def test_normal_question_gets_replied(tmp_path):
    hub, store, loopback = build(tmp_path)
    r = hub.ingest(env("这个产品怎么用？"))
    assert r["status"] == "sent"
    assert len(loopback.outbox) == 1
    assert loopback.outbox[0]["text"]
    store.close()


def test_duplicate_message_is_ignored(tmp_path):
    hub, store, loopback = build(tmp_path)
    hub.ingest(env("你好", mid="dup"))
    r = hub.ingest(env("你好", mid="dup"))
    assert r["status"] == "duplicate"
    assert len(loopback.outbox) <= 1
    store.close()


def test_money_commitment_is_held_for_approval(tmp_path):
    hub, store, loopback = build(tmp_path)
    r = hub.ingest(env("你直接转账给我，我先下单", mid="m2"))
    assert r["status"] == "held"
    assert len(loopback.outbox) == 0
    items = hub.pending_approvals()
    assert len(items) == 1
    res = hub.approve(items[0]["approval_id"], approved=True, edited_text="已转人工线下处理。")
    assert res["ok"] is True
    assert len(loopback.outbox) == 1
    store.close()


def test_rejected_approval_sends_nothing(tmp_path):
    hub, store, loopback = build(tmp_path)
    hub.ingest(env("我马上帮你付款下单", mid="m3"))
    items = hub.pending_approvals()
    res = hub.approve(items[0]["approval_id"], approved=False)
    assert res["status"] == "rejected"
    assert len(loopback.outbox) == 0
    store.close()


def test_out_of_quota_is_dropped(tmp_path):
    store = Store(tmp_path / "q.db")
    loopback = LoopbackConnector()
    registry = Registry()
    registry.register(loopback, "loopback")
    gov = Governance(store, GovernanceConfig(daily_read_quota=1))
    hub = Hub(store=store, governance=gov, llm=MockLLM(), registry=registry)
    hub.ingest(env("第一个问题", mid="q1"))
    r = hub.ingest(env("第二个问题", mid="q2"))
    assert r["status"] == "dropped"
    assert r["reason"].startswith("已达当日读取配额")
    store.close()


def test_audit_trail_records_decisions(tmp_path):
    hub, store, loopback = build(tmp_path)
    hub.ingest(env("怎么用？", mid="a1"))
    rows = store.audit_recent(limit=50)
    assert rows, "应当有审计记录"
    assert any(r["stage"] == "gate" for r in rows)
    store.close()
