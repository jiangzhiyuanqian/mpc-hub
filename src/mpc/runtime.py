"""运行时装配：把一份配置变成能跑起来的客服后台。

这里是"唯一一处"把配置翻译成对象的地方：
- 配置文件（桌面窗口写的 `~/.mpc-hub/config.json`）
- 用户显式设置的环境变量（`MPC_*`）
两者都算"用户自己写进来的"，除此之外程序不碰系统里其它软件留下的凭据。

CLI、桌面窗口、HTTP 后台三个入口共用这一套装配逻辑，
所以"窗口里改了设置 → 后台按新设置跑"是天然一致的。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable

from .agent.llm import BaseLLM, build_llm
from .connectors import LoopbackConnector, OneBotConnector, Registry, WebhookConnector
from .connectors.ilink import ILinkConnector
from .connectors.plugins import load_custom_connectors
from .contracts import Platform
from .governance import Governance
from .hub import Hub
from .settings import Settings
from .store import Store

OnMessage = Callable[[Any], Any]


# ---------------------------------------------------------------- 环境变量覆盖


def apply_env_overlay(settings: Settings) -> Settings:
    """用 `MPC_*` 环境变量覆盖配置（仅覆盖显式给出的项）。

    环境变量是用户自己设的，算数；但没有设置的环境变量**不会**去读系统里
    其它软件留下的同名变量——只认 `MPC_` 前缀的。
    """
    env = os.environ

    def _set(target: Any, key: str, cast: Callable[[str], Any]) -> None:
        raw = env.get(key)
        if raw is None or raw == "":
            return
        try:
            setattr(target, key, cast(raw))
        except (TypeError, ValueError):
            pass

    llm = settings.llm
    _set(llm, "base_url", str)
    _set(llm, "api_key", str)
    _set(llm, "model", str)
    _set(llm, "fast_model", str)
    _set(llm, "provider", str)
    _set(llm, "temperature", float)
    _set(llm, "max_tokens", int)
    _set(llm, "timeout", float)

    onebot = settings.platforms.onebot
    if env.get("MPC_ONEBOT_URL"):
        onebot.base_url = env["MPC_ONEBOT_URL"]
        onebot.enabled = True
    _set(onebot, "access_token", str)

    hook = settings.platforms.webhook
    if env.get("MPC_WEBHOOK_OUT_URL"):
        hook.outbound_url = env["MPC_WEBHOOK_OUT_URL"]
        hook.enabled = True
    _set(hook, "token", str)

    ilink = settings.platforms.wechat_ilink
    if env.get("MPC_WECHAT_ILINK_TOKEN"):
        ilink.bot_token = env["MPC_WECHAT_ILINK_TOKEN"]
        ilink.enabled = True
    _set(ilink, "uin", str)
    return settings


def load_settings(path: str | os.PathLike[str] | None = None) -> Settings:
    """读配置：文件 → 环境变量覆盖。"""
    from pathlib import Path

    target = Path(path).expanduser() if path else None
    settings = Settings.load(target)
    return apply_env_overlay(settings)


# ---------------------------------------------------------------- 连接器装配


def build_registry(
    settings: Settings,
    *,
    on_message: OnMessage | None = None,
) -> Registry:
    """按配置装配连接器。

    想接一个新平台：写一个 `Connector` 子类，在这里 `registry.register(...)`，
    或者更省事——用「自定义连接器」配置项填模块路径，由 `plugins` 自动加载。
    """
    registry = Registry()
    registry.register(LoopbackConnector(), "loopback")

    onebot = settings.platforms.onebot
    if onebot.enabled and onebot.base_url:
        registry.register(
            OneBotConnector(base_url=onebot.base_url, access_token=onebot.access_token),
            "qq",
        )

    hook = settings.platforms.webhook
    if hook.enabled and hook.outbound_url:
        registry.register(
            WebhookConnector(
                platform=Platform.WEBHOOK,
                outbound_url=hook.outbound_url,
                token=hook.token,
            ),
            "webhook",
        )

    ilink = settings.platforms.wechat_ilink
    if ilink.enabled and ilink.bot_token:
        registry.register(
            ILinkConnector(bot_token=ilink.bot_token, uin=ilink.uin, on_message=on_message),
            "wechat",
        )

    load_custom_connectors(registry, settings.platforms.custom)
    return registry


# ---------------------------------------------------------------- 运行时


@dataclass
class Runtime:
    """一次装配的结果。所有入口共用。"""

    settings: Settings
    store: Store
    governance: Governance
    llm: BaseLLM
    registry: Registry
    hub: Hub

    def close(self) -> None:
        try:
            self.registry.stop_all()
        except Exception:
            pass
        try:
            self.store.close()
        except Exception:
            pass


def build_runtime(
    db_path: str | os.PathLike[str] | None = None,
    settings: Settings | None = None,
) -> Runtime:
    """按配置装配一个完整运行时。"""
    if settings is None:
        settings = load_settings()
    store = Store(str(db_path) if db_path else os.getenv("MPC_DB", "data/mpc.db"))
    governance = Governance(store, settings.governance.to_governance_config())
    llm = build_llm(settings.llm.to_llm_config())

    # 先建 registry，再建 hub —— 微信长轮询的消息回调要用到 hub.ingest。
    holder: dict[str, Hub] = {}

    def _on_message(env: Any) -> Any:
        hub = holder.get("hub")
        if hub is None:
            return None
        return hub.ingest(env)

    registry = build_registry(settings, on_message=_on_message)
    hub = Hub(store=store, governance=governance, llm=llm, registry=registry)
    holder["hub"] = hub
    return Runtime(
        settings=settings,
        store=store,
        governance=governance,
        llm=llm,
        registry=registry,
        hub=hub,
    )
