"""OneBot v11 连接器（HTTP 正向 API）。

OneBot v11 是 QQ 生态最通用的事实标准接口：上游实现（LLOneBot /
Lagrange.Milky / NapCat 等）把消息转成 OneBot 事件，下游（本连接器）
用 `send_private_msg` / `send_group_msg` 发回去。

上游负责"接"，本项目负责"判"和"答"，两边通过 OneBot 事件解耦。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..contracts import Action, Capability, ConversationKind, Platform
from .base import Connector


class OneBotConnector(Connector):
    platform = Platform.QQ

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:3000",
        access_token: str = "",
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.timeout = timeout
        self.sent: list[dict[str, Any]] = []

    def capability(self) -> Capability:
        return Capability(
            platform=Platform.QQ,
            can_read=True,
            can_send=True,
            can_send_media=True,
            supports_group=True,
            supports_markdown=False,
            max_text_length=1500,
            requires_owner_approval=False,
            notes=f"OneBot v11 正向 HTTP：{self.base_url}",
        )

    def to_envelope(self, payload: dict[str, Any]) -> Any:
        """解析 OneBot 事件结构（post_type=message）。"""
        kind = "group" if payload.get("message_type") == "group" else "private"
        raw_text = payload.get("raw_message") or payload.get("message") or ""
        if isinstance(raw_text, list):  # CQ 码数组
            raw_text = "".join(
                seg.get("data", {}).get("text", "") if isinstance(seg, dict) else str(seg)
                for seg in raw_text
            )
        mention = "@" in str(raw_text)
        from ..contracts import Envelope

        return Envelope(
            message_id=str(payload.get("message_id") or ""),
            platform=Platform.QQ,
            conversation_id=str(payload.get("group_id") or payload.get("user_id") or ""),
            conversation_kind=ConversationKind(kind),
            sender_id=str(payload.get("user_id") or ""),
            sender_name=str(payload.get("sender", {}).get("card") or payload.get("sender", {}).get("nickname") or ""),
            text=str(raw_text),
            is_mention=mention,
            timestamp=float(payload.get("time") or time.time()),
            raw=payload,
        )

    def send(self, action: Action) -> bool:
        if action.conversation_kind == ConversationKind.GROUP:
            path, payload = "/send_group_msg", {"group_id": int(action.conversation_id), "message": action.text}
        else:
            path, payload = "/send_private_msg", {"user_id": int(action.conversation_id), "message": action.text}
        headers = {"Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(self.base_url + path, json=payload, headers=headers)
        except (httpx.HTTPError, ValueError) as exc:
            self.sent.append({"error": str(exc), "text": action.text, "at": time.time()})
            return False
        ok = resp.status_code < 400
        self.sent.append({"status": resp.status_code, "ok": ok, "text": action.text, "at": time.time()})
        return ok
