"""iLink 协议层测试：对照官方 `parseInbound` / `pollLoop` 的行为。

回归背景：桌面端曾经"显示已连接但手机端不回消息"，原因是
(1) 从没调用 `registry.start_all()`，长轮询根本没跑；
(2) 游标取值顺序错（服务端给 `get_updates_buf`，我们却回传 `sync_buf`）；
(3) 消息列表字段、message_type 过滤、嵌套 message 合并等细节与官方不一致。
这些细节一旦再退回去，界面不会报错，只会"安静地不回消息"。
"""

from __future__ import annotations

import pytest

from mpc.connectors.ilink import (
    ITEM_TEXT,
    ITEM_VOICE,
    ILinkConnector,
    ILinkStaleToken,
)


def _connector(**kwargs) -> ILinkConnector:
    kwargs.setdefault("bot_token", "test-token")
    kwargs.setdefault("uin", "")
    return ILinkConnector(**kwargs)


# ---------- 入站解析 ----------


def test_parse_message_reads_text_from_item_list() -> None:
    """实测 iLink 返回体没有顶层 text，文本在 item_list[type=1].text_item.text。"""
    conn = _connector()
    env = conn.parse_message(
        {
            "from_user_id": "user-1",
            "message_type": 1,
            "message_id": 123456789,
            "create_time_ms": 1700000000000,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "在吗，客服"}}],
        }
    )
    assert env is not None
    assert env.text == "在吗，客服"
    assert env.sender_id == "user-1"
    assert env.conversation_id == "user-1"
    # message_id 服务端是数字，要能取到
    assert env.message_id == "123456789"
    assert env.timestamp == pytest.approx(1700000000.0)


def test_parse_message_skips_non_text_types() -> None:
    """官方只处理 message_type == 1。"""
    conn = _connector()
    assert (
        conn.parse_message(
            {
                "from_user_id": "user-1",
                "message_type": 2,
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "hi"}}],
            }
        )
        is None
    )


def test_parse_message_flattens_nested_message_object() -> None:
    """官方会把嵌套的 message 对象平铺到外层。"""
    conn = _connector()
    env = conn.parse_message(
        {
            "msg": {
                "message": {
                    "from_user_id": "user-2",
                    "message_type": 1,
                    "text": "嵌套文本",
                }
            }
        }
    )
    assert env is not None
    assert env.text == "嵌套文本"
    assert env.sender_id == "user-2"


def test_parse_message_marks_voice_transcript() -> None:
    conn = _connector()
    env = conn.parse_message(
        {
            "from_user_id": "user-3",
            "message_type": 1,
            "item_list": [{"type": ITEM_VOICE, "voice_item": {"text": "我要退款"}}],
        }
    )
    assert env is not None
    assert env.text == "[语音] 我要退款"


def test_to_envelope_rejects_non_text() -> None:
    conn = _connector()
    with pytest.raises(ValueError):
        conn.to_envelope({"from_user_id": "u", "message_type": 9})


# ---------- 轮询 ----------


def test_poll_loop_prefers_get_updates_buf_cursor(monkeypatch) -> None:
    """服务端同时给 get_updates_buf 与 sync_buf 时，必须回传 get_updates_buf。"""
    conn = _connector()
    bodies: list[dict] = []

    def fake_request(path, body, timeout=20.0, tolerate_ret1=False):
        bodies.append(body)
        conn._stop.set()
        return {"get_updates_buf": "CURSOR-A", "sync_buf": "SYNC-B", "msgs": []}

    monkeypatch.setattr(conn, "_request", fake_request)
    conn._poll_loop()
    assert conn._cursor == "CURSOR-A"
    assert bodies[0]["get_updates_buf"] == ""


def test_poll_loop_accepts_alternative_message_list_fields(monkeypatch) -> None:
    """msgs / messages / updates / msg_list 都要认。"""
    for field in ("msgs", "messages", "updates", "msg_list"):
        conn = _connector()
        got: list[str] = []

        def fake_request(path, body, timeout=20.0, tolerate_ret1=False, _f=field):
            conn._stop.set()
            return {
                "get_updates_buf": "C",
                _f: [
                    {
                        "from_user_id": "u",
                        "message_type": 1,
                        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "你好"}}],
                    }
                ],
            }

        monkeypatch.setattr(conn, "_request", fake_request)
        conn.on_message = lambda env: got.append(env.text)
        conn._poll_loop()
        assert got == ["你好"], f"字段 {field} 没被识别"


def test_poll_loop_records_error_instead_of_swallowing(monkeypatch) -> None:
    """出错要留下原因，界面才看得到"为什么没回消息"。"""

    class _Boom(Exception):
        pass

    conn = _connector()

    def fake_request(path, body, timeout=20.0, tolerate_ret1=False):
        conn._stop.set()
        raise _Boom("连接被重置")

    monkeypatch.setattr(conn, "_request", fake_request)
    monkeypatch.setattr("time.sleep", lambda *_a, **_k: None)
    conn._poll_loop()
    assert "拉取消息失败" in conn.status()["last_error"]
    assert conn.status()["state"] == "error"


# ---------- 请求头与凭据 ----------


def test_headers_always_contain_uin_placeholder() -> None:
    """官方无条件发 X-WECHAT-UIN，没配就用随机 base64 占位。"""
    conn = _connector()
    assert conn._headers()["X-WECHAT-UIN"]
    conn2 = _connector(uin="real-uin")
    assert conn2._headers()["X-WECHAT-UIN"] == "real-uin"


def test_stale_token_is_detected() -> None:
    """ret=-14 表示 bot_token 过期，要抛专门的异常让界面提示重扫。"""

    class _Resp:
        status_code = 200

        def json(self):
            return {"ret": -14, "errcode": 0, "errmsg": "token expired"}

    class _Client:
        def post(self, *_a, **_k):
            return _Resp()

    conn = ILinkConnector(bot_token="t", client=_Client())
    with pytest.raises(ILinkStaleToken):
        conn._request("/ilink/bot/getupdates", {})


def test_status_reflects_running_state() -> None:
    conn = _connector()
    assert conn.status()["state"] == "idle"
    assert "尚未开始监听" in conn.status()["detail"]
    conn._running = True
    assert conn.status()["state"] == "running"
    assert "正在监听" in conn.status()["detail"]


def test_status_without_credentials_says_not_scanned() -> None:
    conn = ILinkConnector(bot_token="", uin="")
    assert conn.status()["state"] == "idle"
    assert "还没扫码" in conn.status()["detail"]
