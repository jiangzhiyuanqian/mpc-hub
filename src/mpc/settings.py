"""本地配置中心：让用户在自己机器上填 Key、填平台凭据。

原则（这三条是硬约束，不是建议）：

1. **只认用户自己写进来的配置** —— 本文件、桌面窗口里填的，以及用户显式设置的
   `MPC_*` 环境变量。程序不扫描、不读取、不复用这台机器上其它软件留下的凭据
   （例如别的 AI 客户端写进系统环境变量的 Key）。做软件不是偷环境变量。
2. **配置只落在本机** —— 默认 `~/.mpc-hub/config.json`，可用 `MPC_CONFIG` 改路径。
   该目录在 `.gitignore` 里，密钥不会被提交到仓库。
3. **Key 不外泄** —— 对外接口只回显掩码（`sk-a…wxyz`），日志里也不打印明文。

配置分四块：
- `llm`       模型接口（供应商 / Base URL / Key / 模型名）
- `platforms` 平台接入凭据（微信 iLink / OneBot / 通用 Webhook / 自定义连接器）
- `governance` 治理规则（白名单、时段、配额、字数上限、审批开关）
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .agent.llm import LLMConfig
from .governance import GovernanceConfig

# ---------------------------------------------------------------- 供应商预设

# 预设只提供"常见值"，Base URL 与模型名在窗口里都能改。
# MiniMax / DeepSeek 的开放平台地址若变动，直接在窗口里改即可，不用等发版。
@dataclass(frozen=True)
class ProviderPreset:
    key: str
    label: str
    base_url: str
    model: str
    fast_model: str
    key_hint: str = ""
    key_required: bool = True
    notes: str = ""


PRESETS: dict[str, ProviderPreset] = {
    "deepseek": ProviderPreset(
        key="deepseek",
        label="DeepSeek（官方接口）",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        fast_model="deepseek-chat",
        key_hint="在 platform.deepseek.com 申请，形如 sk-xxxxxxxx",
        notes="OpenAI 兼容；base_url 不要加 /chat/completions。",
    ),
    "minimax": ProviderPreset(
        key="minimax",
        label="MiniMax（开放平台接口）",
        base_url="https://api.minimax.chat/v1",
        model="MiniMax-M2.7",
        fast_model="MiniMax-M2.7",
        key_hint="在 platform.minimaxi.com 申请",
        notes="地址与模型名可能随版本变动，窗口里可改；点「拉取模型列表」最稳。",
    ),
    "openai": ProviderPreset(
        key="openai",
        label="OpenAI（官方接口）",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        fast_model="gpt-4o-mini",
        key_hint="形如 sk-xxxxxxxx",
    ),
    "ollama": ProviderPreset(
        key="ollama",
        label="本地 Ollama（备选，≤7B）",
        base_url="http://127.0.0.1:11434/v1",
        model="qwen2.5:7b",
        fast_model="qwen2.5:0.5b",
        key_hint="本地模型一般留空即可",
        key_required=False,
        notes="接口优先用云端；本地仅作离线备选，建议 ≤7B。",
    ),
    "custom": ProviderPreset(
        key="custom",
        label="自定义（任意 OpenAI 兼容端点）",
        base_url="",
        model="",
        fast_model="",
        key_hint="按对方要求填写",
        notes="vLLM / LM Studio / 各类中转站都属这一类。",
    ),
}


def preset(key: str) -> ProviderPreset:
    return PRESETS.get(key, PRESETS["custom"])


# ---------------------------------------------------------------- 配置模型


@dataclass
class LLMSettings:
    provider: str = "deepseek"
    base_url: str = PRESETS["deepseek"].base_url
    api_key: str = ""
    model: str = PRESETS["deepseek"].model
    fast_model: str = PRESETS["deepseek"].fast_model
    temperature: float = 0.3
    max_tokens: int = 800
    timeout: float = 30.0

    def to_llm_config(self) -> LLMConfig:
        return LLMConfig(
            base_url=self.base_url,
            api_key=self.api_key,
            model=self.model,
            fast_model=self.fast_model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            timeout=self.timeout,
        )

    @property
    def api_key_masked(self) -> str:
        """Key 的掩码形式，只给界面显示，永远不返回明文。"""
        return Settings.mask_key(self.api_key)

    @property
    def api_key_set(self) -> bool:
        return bool(self.api_key)


@dataclass
class WechatILinkSettings:
    enabled: bool = False
    bot_token: str = ""
    uin: str = ""


@dataclass
class OneBotSettings:
    enabled: bool = False
    base_url: str = ""
    access_token: str = ""


@dataclass
class WebhookSettings:
    enabled: bool = False
    outbound_url: str = ""
    token: str = ""


@dataclass
class CustomConnectorSettings:
    """第三方连接器：模块路径 + 工厂函数名（扩展点）。"""

    enabled: bool = True
    platform: str = ""
    module: str = ""
    attr: str = "create"
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlatformSettings:
    wechat_ilink: WechatILinkSettings = field(default_factory=WechatILinkSettings)
    onebot: OneBotSettings = field(default_factory=OneBotSettings)
    webhook: WebhookSettings = field(default_factory=WebhookSettings)
    custom: list[CustomConnectorSettings] = field(default_factory=list)


@dataclass
class GovernanceSettings:
    allowlist: list[str] = field(default_factory=list)
    owner_ids: list[str] = field(default_factory=list)
    blocklist: list[str] = field(default_factory=list)
    active_hours: list[int] = field(default_factory=lambda: [0, 24])
    daily_read_quota: int = 500
    daily_write_quota: int = 200
    max_reply_chars: int = 1000
    require_approval_on_commitment: bool = True
    require_approval_groups: bool = False
    group_mention_only: bool = True
    drop_ping: bool = True

    def to_governance_config(self) -> GovernanceConfig:
        hours = self.active_hours if len(self.active_hours) == 2 else [0, 24]
        return GovernanceConfig(
            allowlist=set(self.allowlist),
            blocklist=set(self.blocklist),
            owner_ids=set(self.owner_ids),
            active_hours=(int(hours[0]), int(hours[1])),
            daily_read_quota=int(self.daily_read_quota),
            daily_write_quota=int(self.daily_write_quota),
            max_reply_chars=int(self.max_reply_chars),
            require_approval_on_commitment=bool(self.require_approval_on_commitment),
            require_approval_groups=bool(self.require_approval_groups),
            group_mention_only=bool(self.group_mention_only),
            drop_ping=bool(self.drop_ping),
        )


@dataclass
class Settings:
    llm: LLMSettings = field(default_factory=LLMSettings)
    platforms: PlatformSettings = field(default_factory=PlatformSettings)
    governance: GovernanceSettings = field(default_factory=GovernanceSettings)

    # ---------- 读写 ----------

    @staticmethod
    def path() -> Path:
        env = os.getenv("MPC_CONFIG", "").strip()
        if env:
            return Path(env).expanduser()
        return Path.home() / ".mpc-hub" / "config.json"

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        target = path or cls.path()
        if not target.exists():
            return cls()
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        return cls.from_dict(raw)

    def save(self, path: Path | None = None) -> Path:
        target = path or self.path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._restrict_permissions(target)
        return target

    @staticmethod
    def _restrict_permissions(target: Path) -> None:
        """尽量让配置文件只有本人能读。

        - POSIX：chmod 600。
        - Windows：chmod 只管只读标志，所以再用 icacls 去掉继承的其它用户权限。
          失败不报错——配置仍可用，只是提示用户自行收紧权限。
        """
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        if os.name != "nt":
            return
        import subprocess

        try:
            subprocess.run(
                [
                    "icacls",
                    str(target),
                    "/inheritance:r",
                    "/grant:r",
                    f"{os.environ.get('USERNAME', '')}:(R,W)",
                ],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    # ---------- dict 互转 ----------

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Settings":
        s = cls()
        llm = raw.get("llm") or {}
        if isinstance(llm, dict):
            for key in (
                "provider",
                "base_url",
                "api_key",
                "model",
                "fast_model",
            ):
                if isinstance(llm.get(key), str):
                    setattr(s.llm, key, llm[key])
            for key in ("temperature", "timeout"):
                if isinstance(llm.get(key), (int, float)):
                    setattr(s.llm, key, float(llm[key]))
            if isinstance(llm.get("max_tokens"), int):
                s.llm.max_tokens = int(llm["max_tokens"])

        plats = raw.get("platforms") or {}
        if isinstance(plats, dict):
            w = plats.get("wechat_ilink") or {}
            if isinstance(w, dict):
                s.platforms.wechat_ilink = WechatILinkSettings(
                    enabled=bool(w.get("enabled", False)),
                    bot_token=str(w.get("bot_token") or ""),
                    uin=str(w.get("uin") or ""),
                )
            o = plats.get("onebot") or {}
            if isinstance(o, dict):
                s.platforms.onebot = OneBotSettings(
                    enabled=bool(o.get("enabled", False)),
                    base_url=str(o.get("base_url") or ""),
                    access_token=str(o.get("access_token") or ""),
                )
            h = plats.get("webhook") or {}
            if isinstance(h, dict):
                s.platforms.webhook = WebhookSettings(
                    enabled=bool(h.get("enabled", False)),
                    outbound_url=str(h.get("outbound_url") or ""),
                    token=str(h.get("token") or ""),
                )
            custom = plats.get("custom") or []
            if isinstance(custom, list):
                for item in custom:
                    if isinstance(item, dict) and item.get("module"):
                        s.platforms.custom.append(
                            CustomConnectorSettings(
                                enabled=bool(item.get("enabled", True)),
                                platform=str(item.get("platform") or ""),
                                module=str(item.get("module") or ""),
                                attr=str(item.get("attr") or "create"),
                                options=item.get("options") or {},
                            )
                        )

        gov = raw.get("governance") or {}
        if isinstance(gov, dict):
            for key in ("allowlist", "owner_ids", "blocklist"):
                value = gov.get(key)
                if isinstance(value, list):
                    setattr(s.governance, key, [str(v) for v in value])
            hours = gov.get("active_hours")
            if isinstance(hours, list) and len(hours) == 2:
                s.governance.active_hours = [int(hours[0]), int(hours[1])]
            for key in ("daily_read_quota", "daily_write_quota", "max_reply_chars"):
                if isinstance(gov.get(key), int):
                    setattr(s.governance, key, int(gov[key]))
            for key in (
                "require_approval_on_commitment",
                "require_approval_groups",
                "group_mention_only",
                "drop_ping",
            ):
                if isinstance(gov.get(key), bool):
                    setattr(s.governance, key, bool(gov[key]))
        return s

    def to_dict(self) -> dict[str, Any]:
        return {
            "llm": asdict(self.llm),
            "platforms": {
                "wechat_ilink": asdict(self.platforms.wechat_ilink),
                "onebot": asdict(self.platforms.onebot),
                "webhook": asdict(self.platforms.webhook),
                "custom": [asdict(c) for c in self.platforms.custom],
            },
            "governance": asdict(self.governance),
        }

    # ---------- 对外展示（脱敏） ----------

    @staticmethod
    def mask_key(key: str) -> str:
        key = (key or "").strip()
        if not key:
            return ""
        if len(key) <= 10:
            return "*" * len(key)
        return f"{key[:4]}…{key[-4:]}（{len(key)} 字符）"

    def public_dict(self) -> dict[str, Any]:
        """给界面/接口看的版本：Key 只给掩码。"""
        data = self.to_dict()
        key = data["llm"].get("api_key") or ""
        data["llm"]["api_key"] = ""
        data["llm"]["api_key_masked"] = self.mask_key(key)
        data["llm"]["api_key_set"] = bool(key)
        return data
