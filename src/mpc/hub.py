"""枢纽层（Hub）：把连接器、治理层、agent 运行时和存储缝在一起。

对上层（HTTP API / CLI / 未来的定时任务）只暴露业务动作：
- `ingest(envelope)`  处理一条入站消息；
- `approve(...)`     处理一次人工审批；
- `capabilities()`   对外公布能力矩阵。

Hub 不认识任何具体平台，平台差异全部在连接器里。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

from .agent.graph import CustomerServiceAgent
from .agent.llm import BaseLLM, LLMConfig, build_llm
from .connectors import Registry
from .contracts import Action, ApprovalRequest, ConversationKind, Envelope, Platform
from .governance import Governance, GovernanceConfig
from .store import Store


class Hub:
    def __init__(
        self,
        store: Store | None = None,
        governance: Governance | None = None,
        llm: BaseLLM | None = None,
        registry: Registry | None = None,
    ) -> None:
        self.store = store or Store()
        self.registry = registry or Registry()
        self.gov = governance or Governance(self.store, GovernanceConfig())
        self.llm = llm or build_llm(LLMConfig())
        self.agent = CustomerServiceAgent(self.store, self.gov, self.llm)
        self._pending: dict[str, ApprovalRequest] = {}
        self._lock = threading.RLock()
        self.counters: dict[str, int] = {
            "inbound": 0,
            "sent": 0,
            "held": 0,
            "dropped": 0,
            "no_reply": 0,
            "send_failed": 0,
            "duplicate": 0,
        }

    # ---------- 入站 ----------

    def ingest(self, env: Envelope, auto_send: bool = True) -> dict[str, Any]:
        """处理一条入站消息。幂等：同一条消息重复投递只会处理一次。

        `auto_send=False` 是「手动接管」模式：agent 仍然起草回复，
        但**不投递**到平台，只把草稿交给主人（桌面端显示成建议卡片）。
        """
        with self._lock:
            if self.store.has_inbound(env.platform.value, env.message_id):
                self.counters["duplicate"] += 1
                self.store.audit(
                    "ingest",
                    "duplicate",
                    "重复消息，已忽略",
                    platform=env.platform.value,
                    conversation_id=env.conversation_id,
                    sender_id=env.sender_id,
                    payload={"message_id": env.message_id},
                )
                return {"status": "duplicate", "reason": "重复消息", "message_id": env.message_id}

            self.counters["inbound"] += 1
            result = self.agent.handle(env)
            status = result.get("status", "dropped")

            if status == "sent" and result.get("action"):
                action = Action.model_validate(result["action"])
                if not auto_send:
                    # 手动接管：只给建议，不投递。
                    self.counters["suggested"] = self.counters.get("suggested", 0) + 1
                    self.store.audit(
                        "suggest",
                        "held_for_owner",
                        "手动接管模式：仅生成建议，未发送",
                        platform=env.platform.value,
                        conversation_id=env.conversation_id,
                        sender_id=env.sender_id,
                        payload={"draft": (result.get("draft") or "")[:500]},
                    )
                    return {
                        "status": "suggested",
                        "reason": "手动接管模式，未自动发送",
                        "draft": result.get("draft", ""),
                        "intent": result.get("intent", {}),
                        "trace": result.get("trace", []),
                        "message_id": env.message_id,
                    }
                ok = self._deliver(action)
                if not ok:
                    self.counters["send_failed"] += 1
                    status = "send_failed"
                else:
                    self.counters["sent"] += 1
            elif status == "held" and result.get("action"):
                req = ApprovalRequest(
                    approval_id=uuid.uuid4().hex,
                    envelope=env,
                    draft_text=result.get("draft", ""),
                    reason=result.get("reason", ""),
                    created_at=time.time(),
                )
                with self._lock:
                    self._pending[req.approval_id] = req
                self.counters["held"] += 1
                self.store.audit(
                    "approval",
                    "pending",
                    result.get("reason", ""),
                    platform=env.platform.value,
                    conversation_id=env.conversation_id,
                    sender_id=env.sender_id,
                    payload={"approval_id": req.approval_id, "draft": req.draft_text},
                )
            elif status == "dropped":
                self.counters["dropped"] += 1
            elif status == "no_reply":
                self.counters["no_reply"] += 1

            return {
                "status": status,
                "reason": result.get("reason", ""),
                "draft": result.get("draft", ""),
                "intent": result.get("intent", {}),
                "trace": result.get("trace", []),
                "message_id": env.message_id,
            }

    # ---------- 审批 ----------

    def pending_approvals(self) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._pending.values())
        return [
            {
                "approval_id": r.approval_id,
                "platform": r.envelope.platform.value,
                "conversation_id": r.envelope.conversation_id,
                "sender_id": r.envelope.sender_id,
                "draft_text": r.draft_text,
                "reason": r.reason,
                "created_at": r.created_at,
            }
            for r in items
        ]

    def approve(
        self, approval_id: str, approved: bool, edited_text: str | None = None, decided_by: str = "owner"
    ) -> dict[str, Any]:
        with self._lock:
            req = self._pending.pop(approval_id, None)
        if req is None:
            return {"ok": False, "reason": "审批单不存在或已处理"}
        if not approved:
            self.store.audit(
                "approval",
                "rejected",
                req.reason,
                platform=req.envelope.platform.value,
                conversation_id=req.envelope.conversation_id,
                sender_id=req.envelope.sender_id,
                payload={"approval_id": approval_id, "decided_by": decided_by},
            )
            return {"ok": True, "status": "rejected", "approval_id": approval_id}

        text = edited_text if edited_text is not None else req.draft_text
        # 人工放行前再过一次出站闸（人可能改写出了敏感内容）。
        # 承诺类规则已跳过——主人点过头了；但硬拦截（密钥泄漏/超长/空）照样生效。
        verdict = self.gov.check_outbound(req.envelope, text, enforce_approval_rules=False)
        if verdict.decision.value != "send":
            self.store.audit(
                "approval",
                "blocked",
                verdict.reason,
                platform=req.envelope.platform.value,
                conversation_id=req.envelope.conversation_id,
                sender_id=req.envelope.sender_id,
                payload={"approval_id": approval_id, "rule": verdict.matched_rule},
            )
            return {"ok": False, "status": "blocked", "reason": verdict.reason, "approval_id": approval_id}

        action = Action(
            action_type="send_text",
            platform=req.envelope.platform,
            conversation_id=req.envelope.conversation_id,
            conversation_kind=req.envelope.conversation_kind,
            text=text,
            reply_to_message_id=req.envelope.message_id,
            idempotency_key=f"approved:{approval_id}",
            created_at=time.time(),
            meta={"approved_by": decided_by},
        )
        ok = self._deliver(action)
        self.store.audit(
            "approval",
            "approved" if ok else "send_failed",
            req.reason,
            platform=req.envelope.platform.value,
            conversation_id=req.envelope.conversation_id,
            sender_id=req.envelope.sender_id,
            payload={"approval_id": approval_id, "decided_by": decided_by, "text": text[:500]},
        )
        return {"ok": ok, "status": "sent" if ok else "send_failed", "approval_id": approval_id}

    # ---------- 出站 ----------

    def _deliver(self, action: Action) -> bool:
        connector = self.registry.for_platform(action.platform)
        if connector is None:
            self.store.audit(
                "send",
                "no_connector",
                f"平台 {action.platform.value} 未注册连接器",
                platform=action.platform.value,
                conversation_id=action.conversation_id,
                payload={"text": action.text[:300]},
            )
            return False
        ok = False
        try:
            ok = connector.send(action)
        except Exception as exc:  # 连接器异常不能拖垮后台
            self.store.audit(
                "send",
                "error",
                str(exc),
                platform=action.platform.value,
                conversation_id=action.conversation_id,
                payload={"text": action.text[:300]},
            )
            return False
        if ok:
            self.store.record_outbound(action)
            self.gov.note_write(
                Envelope(
                    platform=action.platform,
                    conversation_id=action.conversation_id,
                    conversation_kind=action.conversation_kind,
                    sender_id="hub",
                    message_id=action.idempotency_key,
                )
            )
        return ok

    # ---------- 手动接管（主人自己发） ----------

    def send_manual(
        self,
        platform: str | Platform,
        conversation_id: str,
        text: str,
        *,
        conversation_kind: str = "private",
        sender_id: str = "owner",
    ) -> dict[str, Any]:
        """主人在桌面端自己敲一条，替他发到平台上。

        依然要过出站闸：硬拦截（密钥泄漏 / 超长 / 空 / 超配额）照样生效；
        承诺类规则跳过——是主人自己确认过的。
        """
        text = (text or "").strip()
        if not text:
            return {"ok": False, "status": "blocked", "reason": "空消息"}
        try:
            plat = platform if isinstance(platform, Platform) else Platform(platform)
        except ValueError:
            return {"ok": False, "status": "blocked", "reason": f"未知平台 {platform}"}

        env = Envelope(
            message_id=f"manual:{uuid.uuid4().hex}",
            platform=plat,
            conversation_id=conversation_id,
            conversation_kind=ConversationKind(conversation_kind),
            sender_id=sender_id,
            sender_name="主人",
            text=text,
        )
        verdict = self.gov.check_outbound(env, text, enforce_approval_rules=False)
        if verdict.decision.value != "send":
            self.store.audit(
                "send",
                "blocked",
                verdict.reason,
                platform=plat.value,
                conversation_id=conversation_id,
                payload={"text": text[:300]},
            )
            return {"ok": False, "status": "blocked", "reason": verdict.reason}

        action = Action(
            action_type="send_text",
            platform=plat,
            conversation_id=conversation_id,
            conversation_kind=ConversationKind(conversation_kind),
            text=text,
            idempotency_key=f"manual:{uuid.uuid4().hex}",
            created_at=time.time(),
            meta={"mode": "manual"},
        )
        ok = self._deliver(action)
        self.store.audit(
            "send",
            "manual_sent" if ok else "send_failed",
            "主人手动发送",
            platform=plat.value,
            conversation_id=conversation_id,
            payload={"text": text[:300]},
        )
        return {"ok": ok, "status": "sent" if ok else "send_failed", "reason": ""}

    # ---------- 查询 ----------

    def capabilities(self) -> list[dict[str, Any]]:
        return [c.model_dump(mode="json") for c in self.registry.capabilities()]

    def metrics(self) -> dict[str, Any]:
        return {
            "counters": dict(self.counters),
            "ledger": self.store.ledger_snapshot(),
            "pending_approvals": len(self._pending),
            "connectors": self.registry.names(),
        }
