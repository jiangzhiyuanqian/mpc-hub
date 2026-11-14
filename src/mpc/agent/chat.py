"""控制台对话会话。

给两处用：
- 命令行 `mpc chat`（终端里直接跟客服 agent 说话）；
- 桌面窗口的「对话」页。

和入站消息走的是同一套治理闸：承诺性话术不会被直接说出去，而是提示需要人工确认，
凭据泄漏照拦。区别只在于这里不投递到任何社交平台，只回显给主人。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from ..contracts import Action, ConversationKind, Envelope, Platform
from ..governance import Governance
from ..store import Store
from .llm import BaseLLM
from .prompts import SYSTEM_CHAT, build_chat_prompt

CONSOLE_PLATFORM = Platform.CONSOLE.value


class ConsoleChat:
    """一个连续会话。历史存在 SQLite 里，重开软件还在。"""

    def __init__(
        self,
        store: Store,
        governance: Governance,
        llm: BaseLLM,
        *,
        conversation_id: str = "console",
        system: str = SYSTEM_CHAT,
    ) -> None:
        self.store = store
        self.gov = governance
        self.llm = llm
        self.conversation_id = conversation_id
        self.system = system

    # ---------- 对外 ----------

    def reply(self, text: str) -> dict[str, Any]:
        """处理一条主人输入，返回 {status, reply, reason, trace}。"""
        text = (text or "").strip()
        if not text:
            return {"status": "empty", "reply": "", "reason": "空消息", "trace": []}

        env = Envelope(
            message_id=uuid.uuid4().hex,
            platform=Platform.CONSOLE,
            conversation_id=self.conversation_id,
            conversation_kind=ConversationKind.PRIVATE,
            sender_id="owner",
            sender_name="主人",
            text=text,
        )
        self.store.record_inbound(env)
        self.gov.note_read(env)

        history = self.store.conversation_history(
            CONSOLE_PLATFORM, self.conversation_id, limit=8
        )
        prompt = build_chat_prompt(text, history)

        trace: list[str] = []
        try:
            draft = self.llm.chat(self.system, prompt)
        except Exception as exc:
            self.store.audit(
                "console", "error", str(exc), conversation_id=self.conversation_id
            )
            return {
                "status": "error",
                "reply": "",
                "reason": f"模型调用失败：{exc}",
                "trace": ["llm:error"],
            }
        trace.append("llm:ok")

        verdict = self.gov.check_outbound(env, draft)
        trace.append(f"gate:{verdict.decision.value}")

        if verdict.decision.value == "drop":
            self.store.audit(
                "console", "drop", verdict.reason, conversation_id=self.conversation_id
            )
            return {
                "status": "dropped",
                "reply": "",
                "reason": verdict.reason,
                "trace": trace,
            }

        if verdict.decision.value == "approve":
            self.store.audit(
                "console",
                "pending",
                verdict.reason,
                conversation_id=self.conversation_id,
                payload={"draft": draft[:500]},
            )
            return {
                "status": "needs_approval",
                "reply": draft,
                "reason": verdict.reason,
                "trace": trace,
            }

        action = Action(
            action_type="send_text",
            platform=Platform.CONSOLE,
            conversation_id=self.conversation_id,
            conversation_kind=ConversationKind.PRIVATE,
            text=draft,
            idempotency_key=f"console:{uuid.uuid4().hex}",
            created_at=time.time(),
        )
        self.store.record_outbound(action)
        self.gov.note_write(env)
        return {"status": "ok", "reply": draft, "reason": verdict.reason, "trace": trace}

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.store.conversation_history(
            CONSOLE_PLATFORM, self.conversation_id, limit=limit
        )

    def clear(self) -> None:
        """清掉当前会话的历史（只删记忆，不动审计）。"""
        self.store.kv_set(f"console.clear:{self.conversation_id}", str(time.time()))
