"""微信 iLink 连接器测试（全部离线，用假 HTTP 客户端）。"""

from __future__ import annotations

import pytest

from mpc.connectors.ilink import ILinkConnector, ILinkDeliveryRejected
from mpc.contracts import Action, ActionType, Platform


class FakeResponse:
    def __init__(self, data: dict, status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code

    def json(self) -> dict:
        return self._data


class FakeClient:
    """按路径返回预置响应，并记录请求。"""

    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict, dict]] = []

    def post(self, url: str, json=None, headers=None, timeout=None) -> FakeResponse:
        self.calls.append((url, json or {}, headers or {}))
        key = url.split("/ilink/bot/", 1)[-1].split("?")[0]
        resp = self.responses.get(key)
        if resp is None:
            raise AssertionError(f"未预置的请求路径：{url}")
        return resp

    def close(self) -> None:
        pass


def _connector(responses: dict[str, FakeResponse]) -> tuple[ILinkConnector, FakeClient]:
    client = FakeClient(responses)
    conn = ILinkConnector(bot_token="tok-123", uin="wxuin", client=client)
    return conn, client


def test_to_envelope_parses_text_item() -> None:
    conn, _ = _connector({})
    payload = {
        "msg": {
            "from_user_id": "user-9",
            "context_token": "ctx-1",
            "item_list": [{"type": 1, "text_item": {"text": "这个怎么用"}}],
        }
    }
    env = conn.to_envelope(payload)
    assert env.platform is Platform.WECHAT
    assert env.sender_id == "user-9"
    assert env.conversation_id == "user-9"
    assert env.text == "这个怎么用"
    assert env.conversation_kind.value == "private"
    # context_token 缓存下来，后面发消息要用
    assert conn._context_tokens["user-9"] == "ctx-1"


def test_to_envelope_merges_text_and_items() -> None:
    conn, _ = _connector({})
    payload = {
        "msg": {
            "from_user_id": "u1",
            "text": "第一段",
            "item_list": [
                {"type": 1, "text_item": {"text": "第二段"}},
                {"type": 3, "voice_item": {"text": "语音转写"}},
            ],
        }
    }
    env = conn.to_envelope(payload)
    assert "第一段" in env.text and "第二段" in env.text and "语音转写" in env.text


def test_send_posts_expected_body() -> None:
    conn, client = _connector(
        {"sendmessage": FakeResponse({"ret": 0, "errcode": 0})}
    )
    action = Action(
        action_id="a1",
        action_type=ActionType.SEND_TEXT,
        platform=Platform.WECHAT,
        conversation_id="user-9",
        text="收到，这就给您安排。",
    )
    assert conn.send(action) is True
    url, body, headers = client.calls[0]
    assert url.endswith("/ilink/bot/sendmessage")
    assert headers["Authorization"] == "Bearer tok-123"
    assert headers["AuthorizationType"] == "ilink_bot_token"
    assert headers["X-WECHAT-UIN"] == "wxuin"
    msg = body["msg"]
    assert msg["to_user_id"] == "user-9"
    assert msg["item_list"][0] == {"type": 1, "text_item": {"text": "收到，这就给您安排。"}}
    assert body["base_info"]["channel_version"] == "1.0.0"


def test_send_raises_delivery_rejected_on_429() -> None:
    conn, _ = _connector({"sendmessage": FakeResponse({"ret": 0}, status_code=429)})
    action = Action(
        action_id="a1",
        action_type=ActionType.SEND_TEXT,
        platform=Platform.WECHAT,
        conversation_id="user-9",
        text="hi",
    )
    with pytest.raises(ILinkDeliveryRejected):
        conn.send(action)


def test_login_qr_and_status() -> None:
    conn, client = _connector(
        {
            "get_bot_qrcode": FakeResponse(
                {"ret": 0, "qrcode": "qr-1", "qrcode_img_content": "https://qr/1"}
            ),
            "get_qrcode_status": FakeResponse(
                {"ret": 0, "status": "confirmed", "bot_token": "tok-new", "ilink_user_id": "bot-1"}
            ),
        }
    )
    qr = conn.login_qr()
    assert qr["qrcode_id"] == "qr-1"
    assert qr["qr_url"] == "https://qr/1"

    st = conn.login_status("qr-1")
    assert st["status"] == "confirmed"
    assert st["bot_token"] == "tok-new"
    assert conn.bot_token == "tok-new"
    # 状态查询带 qrcode 参数
    assert "qrcode=qr-1" in client.calls[-1][0]


def test_capability_declares_private_only() -> None:
    conn, _ = _connector({})
    cap = conn.capability()
    assert cap.platform is Platform.WECHAT
    assert cap.can_read and cap.can_send
    assert cap.supports_group is False
    assert cap.can_send_media is False
