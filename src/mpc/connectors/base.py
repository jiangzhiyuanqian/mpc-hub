"""连接器抽象。

一个连接器 = 一个平台的"出入口"。它只负责三件事：
1. 声明自己能干什么（Capability）；
2. 把外部消息翻译成统一的 `Envelope`；
3. 把统一的 `Action` 翻译成这个平台的发送动作。

Agent 与治理层完全不知道底下是微信 iLink、OneBot、还是 Webhook。
"""

from __future__ import annotations

import abc
import time
from typing import Any

from ..contracts import Action, Capability, ConversationKind, Envelope, Platform


class Connector(abc.ABC):
    """所有平台适配器的基类。"""

    platform: Platform = Platform.WEBHOOK

    @abc.abstractmethod
    def capability(self) -> Capability:
        """声明本连接器的能力上限。"""

    @abc.abstractmethod
    def send(self, action: Action) -> bool:
        """真正把消息发出去。返回是否成功。"""

    def to_envelope(self, payload: dict[str, Any]) -> Envelope:
        """把平台原始 payload 翻译成统一契约。默认实现按通用字段名解析。"""
        return Envelope(
            message_id=str(payload.get("message_id") or payload.get("id") or ""),
            platform=self.platform,
            conversation_id=str(payload.get("conversation_id") or payload.get("chat_id") or ""),
            conversation_kind=ConversationKind(payload.get("kind", "private")),
            sender_id=str(payload.get("sender_id") or payload.get("user_id") or ""),
            sender_name=str(payload.get("sender_name") or payload.get("nickname") or ""),
            text=str(payload.get("text") or payload.get("content") or ""),
            is_mention=bool(payload.get("is_mention", False)),
            attachments=list(payload.get("attachments") or []),
            reply_to_message_id=payload.get("reply_to_message_id"),
            timestamp=float(payload.get("timestamp") or time.time()),
            raw=payload,
        )

    def start(self) -> None:
        """启动轮询/长连（如需要）。默认无操作。"""

    def stop(self) -> None:
        """停止轮询/长连（如需要）。默认无操作。"""
