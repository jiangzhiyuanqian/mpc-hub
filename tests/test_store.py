from mpc.contracts import Envelope, Platform
from mpc.store import Store


def make_env(mid="m1") -> Envelope:
    return Envelope(
        platform=Platform.LOOPBACK,
        conversation_id="c1",
        sender_id="u1",
        sender_name="用户",
        text="测试消息",
        raw={"k": "v"},
    )


def test_inbound_is_idempotent(tmp_path):
    s = Store(tmp_path / "t.db")
    env = make_env()
    assert s.has_inbound(env.platform.value, env.message_id) is False
    s.record_inbound(env)
    assert s.has_inbound(env.platform.value, env.message_id) is True
    s.record_inbound(env)  # 重复写入不应抛错
    assert len(s.conversation_history(env.platform.value, env.conversation_id)) == 1
    s.close()


def test_ledger_counts_per_day(tmp_path):
    s = Store(tmp_path / "t.db")
    s.bump_ledger("loopback", "read")
    s.bump_ledger("loopback", "read")
    s.bump_ledger("loopback", "write")
    assert s.ledger_count("loopback", "read") == 2
    assert s.ledger_count("loopback", "write") == 1
    assert s.ledger_count("loopback", "read", day="2000-01-01") == 0
    s.close()


def test_audit_is_append_only(tmp_path):
    s = Store(tmp_path / "t.db")
    s.audit("gate", "send", "ok", platform="loopback", conversation_id="c1")
    s.audit("gate", "drop", "too long", platform="loopback", conversation_id="c1")
    rows = s.audit_recent(limit=10)
    assert len(rows) == 2
    assert rows[0]["decision"] == "drop"
    s.close()


def test_profile_and_promise(tmp_path):
    s = Store(tmp_path / "t.db")
    s.upsert_profile("loopback", "u1", display="张三")
    s.bump_profile_turns("loopback", "u1")
    p = s.profile("loopback", "u1")
    assert p["display"] == "张三"
    assert p["turns"] == 1
    pid = s.add_promise("loopback", "c1", "hub", "周五前回复报价")
    assert len(s.open_promises("loopback", "c1")) == 1
    s.close_promise(pid)
    assert len(s.open_promises("loopback", "c1")) == 0
    s.close()
