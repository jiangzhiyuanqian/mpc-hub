"""通用 Webhook 连接器。

适配所有"能给本服务推一条 POST JSON、也能接受一条 POST JSON"的对接方：
企业内部系统、SaaS 客服工单、自建网关、测试桩。

- 入站：外部 POST /api/webhook/{platform}，字段按通用契约解析；
- 出站：POST {outbound_url}，带 X-MPC-Token 头（若配置了 token）。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..contracts import Action, Capability, Platform
from .base import Connector


class WebhookConnector(Connector):
    platform = Platform.WEBHOOK

    def __init__(
        self,
        platform: Platform | str = Platform.WEBHOOK,
        outbound_url: str = "",
        token: str = "",
        timeout: float = 15.0,
    ) -> None:
        self.platform = Platform(platform) if not isinstance(platform, Platform) else platform
        self.outbound_url = outbound_url
        self.token = token
        self.timeout = timeout
        self.sent: list[dict[str, Any]] = []

    def capability(self) -> Capability:
        return Capability(
            platform=self.platform,
            can_read=True,
            can_send=bool(self.outbound_url),
            can_send_media=False,
            supports_group=True,
            supports_markdown=False,
            max_text_length=2000,
            requires_owner_approval=False,
            notes=f"通用 Webhook，出站地址：{self.outbound_url or '（未配置，只收不发）'}",
        )

    def send(self, action: Action) -> bool:
        if not self.outbound_url:
            self.sent.append({"skipped": True, "reason": "未配置出站地址", "text": action.text})
            return False
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-MPC-Token"] = self.token
        body = {
            "platform": action.platform.value,
            "conversation_id": action.conversation_id,
            "text": action.text,
            "reply_to_message_id": action.reply_to_message_id,
            "idempotency_key": action.idempotency_key,
            "created_at": action.created_at,
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(self.outbound_url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            self.sent.append({"error": str(exc), "text": action.text, "at": time.time()})
            return False
        ok = resp.status_code < 400
        self.sent.append({"status": resp.status_code, "ok": ok, "text": action.text, "at": time.time()})
        return ok
