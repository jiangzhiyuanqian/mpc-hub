"""本地回环连接器：不接任何真实平台，用来跑通链路与自检。

它在进程内维护一个"假收件箱"，任何人往里 POST 一条消息，
就当是某个平台来的一条入站消息。
"""

from __future__ import annotations

import time
from typing import Any

from ..contracts import Action, Capability, Platform
from .base import Connector


class LoopbackConnector(Connector):
    platform = Platform.LOOPBACK

    def __init__(self) -> None:
        self.outbox: list[dict[str, Any]] = []

    def capability(self) -> Capability:
        return Capability(
            platform=self.platform,
            can_read=True,
            can_send=True,
            can_send_media=False,
            supports_group=True,
            supports_markdown=False,
            max_text_length=1000,
            requires_owner_approval=False,
            notes="本地回环，仅用于自检与联调",
        )

    def send(self, action: Action) -> bool:
        self.outbox.append(
            {
                "conversation_id": action.conversation_id,
                "text": action.text,
                "at": time.time(),
                "reply_to": action.reply_to_message_id,
            }
        )
        return True
