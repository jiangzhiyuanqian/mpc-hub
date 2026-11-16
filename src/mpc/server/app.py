"""FastAPI 后台服务。

这是"客服服务器后台"的对外门面，只监听本机（默认 127.0.0.1:8760）。
接口分四组：
- 入站：/api/inbound、/api/webhook/{platform}
- 审批：/api/approvals、/api/approvals/{id}
- 查询：/api/capabilities、/api/metrics、/api/audit、/api/sessions/...
- 运维：/healthz
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ..connectors import Registry, WebhookConnector
from ..contracts import Envelope, Platform
from ..hub import Hub
from ..settings import Settings


class InboundBody(BaseModel):
    platform: str = "loopback"
    message_id: str | None = None
    conversation_id: str = "default"
    conversation_kind: str = "private"
    sender_id: str = "unknown"
    sender_name: str = ""
    text: str = ""
    is_mention: bool = False
    attachments: list[str] = Field(default_factory=list)
    reply_to_message_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ApprovalBody(BaseModel):
    approved: bool = True
    edited_text: str | None = None
    decided_by: str = "owner"


def build_registry(hub: Hub | None = None) -> Registry:
    """按配置装配连接器（配置文件 + `MPC_*` 环境变量）。

    想接一个平台，只需要在这里注册一个连接器；
    不接任何真实平台时只留 loopback，服务照样能跑。
    """
    from ..runtime import build_registry as _build
    from ..runtime import load_settings

    settings = load_settings()
    on_message = (lambda env: hub.ingest(env)) if hub is not None else None
    return _build(settings, on_message=on_message)


def create_app(hub: Hub | None = None, settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # 长轮询类连接器（微信 iLink）在后台线程里跑，消息直接进枢纽
        try:
            app.state.hub.registry.start_all()
        except Exception:
            pass
        yield
        try:
            app.state.hub.registry.stop_all()
        except Exception:
            pass

    app = FastAPI(
        title="多平台连接中心 · 客服后台",
        version="0.5.0",
        description="独立运行的客服 agent 后台：不依赖也不修改 DSH。",
        lifespan=lifespan,
    )
    if hub is None:
        from ..runtime import build_runtime

        runtime = build_runtime(settings=settings)
        hub = runtime.hub
        hub.registry = build_registry(hub)
        app.state.runtime = runtime
    else:
        app.state.runtime = None
    app.state.hub = hub
    app.state.settings = settings or Settings.load()

    def get_hub() -> Hub:
        return app.state.hub

    # ---------- 运维 ----------

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "service": "mpc-hub", "version": app.version}

    # ---------- 入站 ----------

    @app.post("/api/inbound")
    async def inbound(body: InboundBody) -> dict[str, Any]:
        hub = get_hub()
        try:
            platform = Platform(body.platform)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"未知平台：{body.platform}") from exc
        env = Envelope(
            platform=platform,
            conversation_id=body.conversation_id,
            sender_id=body.sender_id,
            sender_name=body.sender_name,
            text=body.text,
            is_mention=body.is_mention,
            attachments=body.attachments,
            reply_to_message_id=body.reply_to_message_id,
            raw=body.raw,
            **({"message_id": body.message_id} if body.message_id else {}),
        )
        result = await run_in_threadpool(hub.ingest, env)
        return result

    @app.post("/api/webhook/{platform}")
    async def webhook(platform: str, payload: dict[str, Any]) -> dict[str, Any]:
        hub = get_hub()
        connector = hub.registry.get(platform)
        if connector is None:
            # 未知平台也允许接入：自动用通用 Webhook 解析
            connector = WebhookConnector(platform=platform)
        env = connector.to_envelope(payload)
        if not env.message_id:
            env = env.model_copy(update={"message_id": payload.get("message_id", "") or str(int(env.timestamp * 1000))})
        result = await run_in_threadpool(hub.ingest, env)
        return result

    # ---------- 审批 ----------

    @app.get("/api/approvals")
    async def approvals() -> dict[str, Any]:
        return {"items": get_hub().pending_approvals()}

    @app.post("/api/approvals/{approval_id}")
    async def approve(approval_id: str, body: ApprovalBody) -> dict[str, Any]:
        result = await run_in_threadpool(
            get_hub().approve, approval_id, body.approved, body.edited_text, body.decided_by
        )
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("reason", "审批失败"))
        return result

    # ---------- 查询 ----------

    @app.get("/api/capabilities")
    async def capabilities() -> dict[str, Any]:
        return {"items": get_hub().capabilities()}

    @app.get("/api/metrics")
    async def metrics() -> dict[str, Any]:
        return get_hub().metrics()

    @app.get("/api/audit")
    async def audit(limit: int = 50) -> dict[str, Any]:
        return {"items": get_hub().store.audit_recent(limit=limit)}

    @app.get("/api/sessions/{platform}/{conversation_id}")
    async def sessions(platform: str, conversation_id: str, limit: int = 30) -> dict[str, Any]:
        hub = get_hub()
        return {
            "history": hub.store.conversation_history(platform, conversation_id, limit=limit),
            "watermark": hub.store.watermark(platform, conversation_id),
            "promises": hub.store.open_promises(platform, conversation_id),
        }

    # ---------- 配置（只读掩码；写回本机配置文件） ----------

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        """返回脱敏后的配置。Key 只给掩码，不回显明文。"""
        cfg: Settings = app.state.settings
        data = cfg.public_dict()
        data["config_path"] = str(Settings.path())
        return data

    @app.post("/api/settings")
    async def post_settings(body: dict[str, Any]) -> dict[str, Any]:
        """把窗口里改的项写回配置文件。

        只接受 `llm` / `governance` / `platforms` 三块里的已知字段；
        `api_key` 若传空串表示"保持原样"，传新值才覆盖。
        """
        cfg: Settings = app.state.settings
        payload = body if isinstance(body, dict) else {}

        llm_in = payload.get("llm")
        if isinstance(llm_in, dict):
            for key in ("provider", "base_url", "model", "fast_model"):
                if isinstance(llm_in.get(key), str):
                    setattr(cfg.llm, key, llm_in[key])
            if isinstance(llm_in.get("api_key"), str) and llm_in["api_key"]:
                cfg.llm.api_key = llm_in["api_key"]
            for key in ("temperature", "timeout"):
                if isinstance(llm_in.get(key), (int, float)):
                    setattr(cfg.llm, key, float(llm_in[key]))
            if isinstance(llm_in.get("max_tokens"), int):
                cfg.llm.max_tokens = int(llm_in["max_tokens"])

        gov_in = payload.get("governance")
        if isinstance(gov_in, dict):
            for key in ("allowlist", "owner_ids", "blocklist"):
                if isinstance(gov_in.get(key), list):
                    setattr(cfg.governance, key, [str(v) for v in gov_in[key]])
            hours = gov_in.get("active_hours")
            if isinstance(hours, list) and len(hours) == 2:
                cfg.governance.active_hours = [int(hours[0]), int(hours[1])]
            for key in ("daily_read_quota", "daily_write_quota", "max_reply_chars"):
                if isinstance(gov_in.get(key), int):
                    setattr(cfg.governance, key, int(gov_in[key]))
            for key in (
                "require_approval_on_commitment",
                "require_approval_groups",
                "group_mention_only",
                "drop_ping",
            ):
                if isinstance(gov_in.get(key), bool):
                    setattr(cfg.governance, key, bool(gov_in[key]))

        path = cfg.save()
        return {"ok": True, "config_path": str(path), "settings": cfg.public_dict()}

    return app


app = create_app()
