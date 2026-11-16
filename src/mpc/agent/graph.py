"""Agent 运行时：LangGraph 状态图。

一条入站消息的完整旅程：

    ingest → triage → draft → gate → (send | hold_for_approval | drop)

- ingest：治理层入站闸（白名单/时段/配额/群聊@），
- triage：小模型快判（是否需要回复、意图、置信度），
- draft：生成回复草稿，
- gate：治理层出站闸（承诺/凭据/长度/配额），
- 末端分流：直接发送 / 挂起等人工审批 / 丢弃。

每一步都写审计，事后可完整复盘"这条消息为什么被回/没被回"。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, TypedDict

# LangGraph 单独 import 要 1.3 秒以上，而它只在真的有入站消息时才用得上。
# 所以改成"用到才 import"：桌面端启动不再为它买单，第一条消息进来时再付这笔钱。
_LANGGRAPH: tuple | None = None
_LANGGRAPH_TRIED = False


def _load_langgraph() -> tuple | None:
    """首次调用时才真正 import LangGraph；返回 (END, START, StateGraph) 或 None。"""
    global _LANGGRAPH, _LANGGRAPH_TRIED
    if not _LANGGRAPH_TRIED:
        _LANGGRAPH_TRIED = True
        try:
            from langgraph.graph import END, START, StateGraph

            _LANGGRAPH = (END, START, StateGraph)
        except ImportError:  # pragma: no cover - 取决于运行环境
            _LANGGRAPH = None
    return _LANGGRAPH


def __getattr__(name: str):
    # PEP 562：模块级属性按需计算，保留 LANGGRAPH_AVAILABLE 这个公开名字，
    # 同时不让它变成启动期的一次强制 import。
    if name == "LANGGRAPH_AVAILABLE":
        return _load_langgraph() is not None
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

from ..contracts import Action, ActionType, Decision, Envelope, Verdict
from ..governance import Governance
from .llm import BaseLLM
from .prompts import SYSTEM_DRAFT, SYSTEM_TRIAGE
from ..store import Store

__all__ = [
    "CustomerServiceAgent",
    "SYSTEM_DRAFT",
    "SYSTEM_TRIAGE",
    "LANGGRAPH_AVAILABLE",
]


class AgentState(TypedDict, total=False):
    envelope: dict[str, Any]
    in_verdict: dict[str, Any]
    intent: dict[str, Any]
    draft: str
    out_verdict: dict[str, Any]
    action: dict[str, Any]
    status: str  # sent | held | dropped | no_reply
    reason: str
    trace: list[str]


def _verdict_dict(v: Verdict) -> dict[str, Any]:
    return {"decision": v.decision.value, "reason": v.reason, "rule": v.matched_rule}


class _LinearGraph:
    """降级执行器：节点顺序与状态图一致，LangGraph 缺失时保证链路还能跑。"""

    def __init__(self, nodes: list[Any]) -> None:
        self.nodes = nodes

    def invoke(self, state: dict[str, Any]) -> dict[str, Any]:
        for node in self.nodes:
            state = node(state)
        return state


class CustomerServiceAgent:
    """客服 agent。对外只有一个方法：`handle(envelope)`。"""

    def __init__(self, store: Store, governance: Governance, llm: BaseLLM) -> None:
        self.store = store
        self.gov = governance
        self.llm = llm
        # 图延迟构造：第一条消息进来时才 import LangGraph 并编译图，
        # 桌面端启动因此不用付这 1.3 秒。
        self._graph: Any = None

    @property
    def graph(self) -> Any:
        if self._graph is None:
            self._graph = self._build()
        return self._graph

    # ---------- 图 ----------

    def _build(self):
        lg = _load_langgraph()
        if lg is None:
            return _LinearGraph(
                [self._ingest, self._triage, self._draft, self._gate, self._route]
            )
        END, START, StateGraph = lg
        g = StateGraph(AgentState)
        g.add_node("ingest", self._ingest)
        g.add_node("triage", self._triage)
        g.add_node("draft", self._draft)
        g.add_node("gate", self._gate)
        g.add_node("route", self._route)
        g.add_edge(START, "ingest")
        g.add_edge("ingest", "triage")
        g.add_edge("triage", "draft")
        g.add_edge("draft", "gate")
        g.add_edge("gate", "route")
        g.add_edge("route", END)
        return g.compile()

    # ---------- 节点 ----------

    def _ingest(self, state: AgentState) -> AgentState:
        env = _envelope_from(state["envelope"])
        verdict = self.gov.check_inbound(env)
        state["in_verdict"] = _verdict_dict(verdict)
        state.setdefault("trace", []).append(f"ingest:{verdict.decision.value}")
        if verdict.decision is Decision.SEND:
            self.gov.note_read(env)
            self.store.upsert_profile(
                env.platform.value, env.sender_id, display=env.sender_name
            )
            self.store.bump_profile_turns(env.platform.value, env.sender_id)
        return state

    def _triage(self, state: AgentState) -> AgentState:
        env = _envelope_from(state["envelope"])
        if state["in_verdict"]["decision"] != Decision.SEND.value:
            state["status"] = "dropped"
            state["reason"] = state["in_verdict"]["reason"]
            return state
        try:
            intent = self.llm.json_chat(SYSTEM_TRIAGE, env.text[:1500], fast=True)
        except Exception as exc:  # 快判失败不致命，退回"需要回复"
            intent = {"intention": "unknown", "confidence": 0.0, "need_reply": True, "reason": f"快判失败:{exc}"}
        state["intent"] = intent
        state.setdefault("trace", []).append(f"triage:{intent.get('intention')}")
        if not intent.get("need_reply", True):
            state["status"] = "no_reply"
            state["reason"] = intent.get("reason", "快判无需回复")
        return state

    def _draft(self, state: AgentState) -> AgentState:
        env = _envelope_from(state["envelope"])
        if state.get("status") in {"dropped", "no_reply"}:
            return state
        history = self.store.conversation_history(env.platform.value, env.conversation_id, limit=8)
        ctx_lines = []
        for h in history[:-1]:
            who = "客户" if h["direction"] == "inbound" else "客服"
            ctx_lines.append(f"{who}: {h['text']}")
        ctx = "\n".join(ctx_lines)
        user = f"【近期对话】\n{ctx}\n\n【客户本条消息】\n{env.text}"
        try:
            state["draft"] = self.llm.chat(SYSTEM_DRAFT, user)
        except Exception as exc:
            state["draft"] = ""
            state["status"] = "dropped"
            state["reason"] = f"生成失败:{exc}"
            state.setdefault("trace", []).append("draft:error")
        return state

    def _gate(self, state: AgentState) -> AgentState:
        env = _envelope_from(state["envelope"])
        if state.get("status") in {"dropped", "no_reply"}:
            return state
        verdict = self.gov.check_outbound(env, state.get("draft", ""))
        state["out_verdict"] = _verdict_dict(verdict)
        state.setdefault("trace", []).append(f"gate:{verdict.decision.value}")
        return state

    def _route(self, state: AgentState) -> AgentState:
        env = _envelope_from(state["envelope"])
        out = state.get("out_verdict")
        if not out:
            # 入站闸或快判阶段已经终止（dropped / no_reply），
            # 没有出站闸结论，此时不能覆盖上游给出的状态和原因。
            return state
        decision = out.get("decision", Decision.DROP.value)
        draft = state.get("draft", "")

        if decision == Decision.DROP.value:
            state["status"] = "dropped"
            state["reason"] = out.get("reason", "")
            self.store.audit(
                "gate",
                "drop",
                out.get("reason", ""),
                platform=env.platform.value,
                conversation_id=env.conversation_id,
                sender_id=env.sender_id,
                payload={"trace": state.get("trace", [])},
            )
            return state

        action = Action(
            action_id=uuid.uuid4().hex,
            action_type=ActionType.SEND_TEXT,
            platform=env.platform,
            conversation_id=env.conversation_id,
            conversation_kind=env.conversation_kind,
            text=draft,
            reply_to_message_id=env.message_id,
            idempotency_key=f"{env.platform.value}:{env.conversation_id}:{env.message_id}",
            created_at=time.time(),
            meta={"rule": out.get("rule", ""), "reason": out.get("reason", "")},
        )
        state["action"] = action.model_dump(mode="json")
        state["status"] = "held" if decision == Decision.APPROVE.value else "sent"
        state["reason"] = out.get("reason", "")
        self.store.audit(
            "gate",
            decision,
            out.get("reason", ""),
            platform=env.platform.value,
            conversation_id=env.conversation_id,
            sender_id=env.sender_id,
            payload={"text": draft[:500], "trace": state.get("trace", [])},
        )
        return state

    # ---------- 对外 ----------

    def handle(self, env: Envelope) -> dict[str, Any]:
        """处理一条入站消息，返回处理结果摘要。"""
        if not self.store.has_inbound(env.platform.value, env.message_id):
            self.store.record_inbound(env)
        result = self.graph.invoke({"envelope": env.model_dump(mode="json"), "trace": []})
        return {
            "status": result.get("status", "dropped"),
            "reason": result.get("reason", ""),
            "intent": result.get("intent", {}),
            "draft": result.get("draft", ""),
            "action": result.get("action"),
            "trace": result.get("trace", []),
        }


def _envelope_from(data: dict[str, Any]) -> Envelope:
    return Envelope.model_validate(data)
