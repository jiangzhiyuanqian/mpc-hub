import pytest

from mpc.contracts import ConversationKind, Envelope, Platform
from mpc.governance import Governance, GovernanceConfig
from mpc.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def env(text="你好", **kw) -> Envelope:
    base = dict(
        platform=Platform.LOOPBACK,
        conversation_id="c1",
        sender_id="u1",
        sender_name="用户",
        text=text,
    )
    base.update(kw)
    return Envelope(**base)


def test_inbound_passes_normal_message(store):
    gov = Governance(store, GovernanceConfig())
    v = gov.check_inbound(env("这个东西怎么用"))
    assert v.decision.value == "send"


def test_inbound_drops_ping(store):
    gov = Governance(store, GovernanceConfig())
    assert gov.check_inbound(env("你好")).decision.value == "drop"


def test_inbound_drops_group_without_mention(store):
    gov = Governance(store, GovernanceConfig())
    v = gov.check_inbound(env("随便聊聊", conversation_kind=ConversationKind.GROUP, is_mention=False))
    assert v.decision.value == "drop"
    assert v.matched_rule == "group_mention"


def test_inbound_drops_not_in_allowlist(store):
    gov = Governance(store, GovernanceConfig(allowlist={"boss"}))
    assert gov.check_inbound(env("在吗", sender_id="stranger")).decision.value == "drop"


def test_read_quota_blocks(store):
    gov = Governance(store, GovernanceConfig(daily_read_quota=2))
    for _ in range(2):
        gov.note_read(env())
    v = gov.check_inbound(env("继续聊"))
    assert v.decision.value == "drop"
    assert v.matched_rule == "quota"


def test_outbound_blocks_secret(store):
    gov = Governance(store, GovernanceConfig())
    v = gov.check_outbound(env(), "这是你的 key sk-abcdefghijklmnopqrstuvwxyz")
    assert v.decision.value == "drop"
    assert v.matched_rule.startswith("secret")


def test_outbound_requires_approval_for_commitment(store):
    gov = Governance(store, GovernanceConfig())
    v = gov.check_outbound(env(), "好的，我现在就帮你下单并转账")
    assert v.decision.value == "approve"


def test_outbound_drops_too_long(store):
    gov = Governance(store, GovernanceConfig(max_reply_chars=100))
    v = gov.check_outbound(env(), "啊" * 200)
    assert v.decision.value == "drop"
    assert v.matched_rule == "max_len"


def test_write_quota_blocks(store):
    gov = Governance(store, GovernanceConfig(daily_write_quota=1))
    gov.note_write(env())
    v = gov.check_outbound(env(), "好的")
    assert v.decision.value == "drop"
    assert v.matched_rule == "quota"
