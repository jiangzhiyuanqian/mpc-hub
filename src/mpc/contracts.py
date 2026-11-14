"""统一契约层。

所有平台适配器（连接器）都把外部世界的消息翻译成 `Envelope`，
所有出站动作都表达成 `Action`。agent 运行时只认这两个结构，
平台差异全部收敛在连接器里。
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Platform(str, Enum):
    """已接入（或计划接入）的平台标识。"""

    WECHAT = "wechat"
    WECHAT_MP = "wechat_mp"
    QQ = "qq"
    DOUYIN = "douyin"
    XIAOHONGSHU = "xiaohongshu"
    DINGTALK = "dingtalk"
    FEISHU = "feishu"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    WEBHOOK = "webhook"
    LOOPBACK = "loopback"
    CONSOLE = "console"  # 终端/桌面窗口里的对话，不投递到任何社交平台


class ConversationKind(str, Enum):
    PRIVATE = "private"
    GROUP = "group"


class MessageDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class Envelope(BaseModel):
    """一条标准化后的入站消息。

    字段刻意保持"最小但够用"：够 agent 读、能定位来源、能回话。
    平台私有的东西一律塞进 `raw`，不让它污染核心逻辑。
    """

    message_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    platform: Platform
    conversation_id: str
    conversation_kind: ConversationKind = ConversationKind.PRIVATE
    sender_id: str
    sender_name: str = ""
    text: str = ""
    is_mention: bool = False
    attachments: list[str] = Field(default_factory=list)
    reply_to_message_id: str | None = None
    timestamp: float = Field(default_factory=time.time)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def session_key(self) -> str:
        """跨平台唯一会话键：平台 + 会话 + 发送者。"""
        return f"{self.platform.value}:{self.conversation_id}:{self.sender_id}"


class ActionType(str, Enum):
    SEND_TEXT = "send_text"
    SEND_IMAGE = "send_image"
    SEND_FILE = "send_file"
    NOOP = "noop"


class Action(BaseModel):
    """一条标准化后的出站动作。"""

    action_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    action_type: ActionType = ActionType.SEND_TEXT
    platform: Platform
    conversation_id: str
    conversation_kind: ConversationKind = ConversationKind.PRIVATE
    text: str = ""
    media_refs: list[str] = Field(default_factory=list)
    reply_to_message_id: str | None = None
    idempotency_key: str = ""
    created_at: float = Field(default_factory=time.time)
    meta: dict[str, Any] = Field(default_factory=dict)


class Capability(BaseModel):
    """连接器能力声明。

    枢纽在接入连接器时先协商能力，再决定启用哪些动作。
    """

    platform: Platform
    can_read: bool = False
    can_send: bool = False
    can_send_media: bool = False
    supports_group: bool = False
    supports_markdown: bool = False
    max_text_length: int = 1000
    requires_owner_approval: bool = False
    notes: str = ""


class Decision(str, Enum):
    """治理层对一次回复的裁决。"""

    SEND = "send"
    APPROVE = "approve"
    DROP = "drop"


class Verdict(BaseModel):
    """治理裁决的结果，带原因，便于审计。"""

    decision: Decision
    reason: str = ""
    matched_rule: str | None = None


class InboundRequest(BaseModel):
    """连接器或外部系统投递到枢纽的入站请求。"""

    envelope: Envelope
    connector: str | None = None


class ApprovalRequest(BaseModel):
    """待主人确认的一次回复。"""

    approval_id: str
    envelope: Envelope
    draft_text: str
    reason: str = ""
    created_at: float = Field(default_factory=time.time)


class ApprovalDecision(BaseModel):
    approval_id: str
    approved: bool
    edited_text: str | None = None
    decided_by: str = "owner"
    decided_at: float = Field(default_factory=time.time)


class Metric(BaseModel):
    """一段运行期的计数。"""

    name: str
    value: int = 0
    labels: dict[str, str] = Field(default_factory=dict)


LogLevel = Literal["debug", "info", "warn", "error"]
