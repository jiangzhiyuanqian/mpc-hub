"""治理层：决定"这条消息要不要回、能不能回、回之前要不要人点头"。

客服后台最容易出事的不是不会回，而是乱回。所以这里用硬规则卡在
agent 前面，而不是指望提示词让模型自己老实。

四道闸：
1. 白名单/黑名单：谁能对 agent 说话；
2. 时段：非工作时段不主动说话（可按平台配置）；
3. 配额：每天每个平台的收/发条数上限；
4. 发送前闸门：承诺性话术、凭据泄漏、超长文本、平台不支持的媒体。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .contracts import Decision, Envelope, Verdict
from .store import Store

# 承诺性/金钱性话术：客服 agent 不该自己拍板的事
COMMITMENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("money", re.compile(r"(?:转账|付款|付钱|打款|充值|退款到账|先付|垫付)")),
    ("purchase", re.compile(r"(?:下单|购买|买一单|帮你买|代下单)")),
    ("guarantee", re.compile(r"(?:保证|承诺一定|百分之百|百分百|包过|包好)")),
    ("legal", re.compile(r"(?:一定赔|索赔|诉讼|起诉|报警)")),
]

# 凭据泄漏
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("api_key", re.compile(r"\b(?:sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,})\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
    ("password", re.compile(r"(?i)\b(?:password|passwd|密码)\s*[:=]\s*\S{4,}")),
]

# 身份越界：agent 不该冒充真人做现实承诺
IDENTITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("real_promise", re.compile(r"(?:我|我们|这边).{0,8}(?:会|马上|立即|帮你|给你|替你).{0,8}(?:购买|下单|付款|付钱|转账|打款)")),
]


@dataclass
class GovernanceConfig:
    """治理策略。可从环境变量或配置文件覆盖。"""

    allowlist: set[str] = field(default_factory=set)
    blocklist: set[str] = field(default_factory=set)
    allow_platforms: set[str] = field(default_factory=set)
    active_hours: tuple[int, int] | None = (0, 24)  # 本地时间小时区间，左闭右开
    daily_read_quota: int = 500
    daily_write_quota: int = 200
    max_reply_chars: int = 1000
    require_approval_on_commitment: bool = True
    require_approval_groups: bool = False
    owner_ids: set[str] = field(default_factory=set)
    group_mention_only: bool = True
    drop_ping: bool = True


class Governance:
    """治理引擎。"""

    def __init__(self, store: Store, config: GovernanceConfig | None = None) -> None:
        self.store = store
        self.cfg = config or GovernanceConfig()

    # ---------- 入站闸 ----------

    def check_inbound(self, env: Envelope) -> Verdict:
        """判断一条入站消息是否值得进入 agent 处理。"""
        pid = env.platform.value

        if self.cfg.allow_platforms and pid not in self.cfg.allow_platforms:
            return Verdict(decision=Decision.DROP, reason=f"平台 {pid} 未在 allow_platforms 内", matched_rule="platform")

        sender = env.sender_id
        if sender in self.cfg.blocklist:
            return Verdict(decision=Decision.DROP, reason="发送者在黑名单", matched_rule="blocklist")

        if self.cfg.allowlist and sender not in self.cfg.allowlist:
            # 白名单为空表示不限；非空则必须命中
            if sender not in self.cfg.owner_ids:
                return Verdict(decision=Decision.DROP, reason="发送者不在白名单", matched_rule="allowlist")

        if env.conversation_kind.value == "group" and self.cfg.group_mention_only and not env.is_mention:
            return Verdict(decision=Decision.DROP, reason="群聊未 @ 机器人", matched_rule="group_mention")

        if self.cfg.drop_ping and self._is_ping(env.text):
            return Verdict(decision=Decision.DROP, reason="纯 ping/表情", matched_rule="ping")

        if not self.cfg.active_hours:
            return Verdict(decision=Decision.DROP, reason="当前不在允许服务时段", matched_rule="active_hours")
        start, end = self.cfg.active_hours
        hour = time.localtime().tm_hour
        in_window = (start <= hour < end) if start <= end else (hour >= start or hour < end)
        if not in_window:
            return Verdict(decision=Decision.DROP, reason=f"当前 {hour} 点不在允许服务时段", matched_rule="active_hours")

        used = self.store.ledger_count(pid, "read")
        if used >= self.cfg.daily_read_quota:
            return Verdict(decision=Decision.DROP, reason=f"已达当日读取配额 {used}/{self.cfg.daily_read_quota}", matched_rule="quota")

        return Verdict(decision=Decision.SEND, reason="通过入站检查", matched_rule="")

    # ---------- 出站闸 ----------

    def check_outbound(self, env: Envelope, text: str, *, enforce_approval_rules: bool = True) -> Verdict:
        """发送前的最后一道闸，返回 SEND / APPROVE / DROP。

        `enforce_approval_rules=False` 时跳过"需人工确认"类规则，只保留硬拦截
        （超长、空、凭据泄漏、配额）。人工放行时用这个模式：主人已经点过头了，
        不该因为原文里带着承诺词就永远发不出去——但密钥泄漏照样拦。
        """
        pid = env.platform.value
        sender = env.sender_id

        if sender in self.cfg.blocklist:
            return Verdict(decision=Decision.DROP, reason="发送者在黑名单", matched_rule="blocklist")

        if len(text) > self.cfg.max_reply_chars:
            return Verdict(decision=Decision.DROP, reason=f"回复超长 {len(text)}/{self.cfg.max_reply_chars}，需分片", matched_rule="max_len")

        if not text.strip():
            return Verdict(decision=Decision.DROP, reason="空回复", matched_rule="empty")

        for name, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                return Verdict(decision=Decision.DROP, reason=f"检测到疑似凭据泄漏（{name}）", matched_rule=f"secret:{name}")

        if self.cfg.require_approval_on_commitment and enforce_approval_rules:
            for name, pattern in COMMITMENT_PATTERNS:
                if pattern.search(text):
                    return Verdict(decision=Decision.APPROVE, reason=f"涉及承诺性内容（{name}），需人工确认", matched_rule=f"commit:{name}")
            for name, pattern in IDENTITY_PATTERNS:
                if pattern.search(text):
                    return Verdict(decision=Decision.APPROVE, reason=f"疑似身份越界承诺（{name}），需人工确认", matched_rule=f"identity:{name}")

        if self.cfg.require_approval_groups and enforce_approval_rules and env.conversation_kind.value == "group":
            return Verdict(decision=Decision.APPROVE, reason="群聊回复需人工确认", matched_rule="group_approval")

        used = self.store.ledger_count(pid, "write")
        if used >= self.cfg.daily_write_quota:
            return Verdict(decision=Decision.DROP, reason=f"已达当日发送配额 {used}/{self.cfg.daily_write_quota}", matched_rule="quota")

        return Verdict(decision=Decision.SEND, reason="通过出站检查", matched_rule="")

    # ---------- 工具 ----------

    @staticmethod
    def _is_ping(text: str) -> bool:
        """整条消息只是招呼/表情/空白的，直接丢弃，不浪费一次模型调用。"""
        stripped = text.strip()
        if not stripped:
            return True
        return bool(
            re.fullmatch(
                r"[\s\W_]*(?:ping|pong|在吗|你好|您好|您好呀|哈喽|hello|hi|？|\?|!|！)[\s\W_]*",
                stripped,
                re.I,
            )
        )

    def note_read(self, env: Envelope) -> int:
        return self.store.bump_ledger(env.platform.value, "read")

    def note_write(self, env: Envelope) -> int:
        return self.store.bump_ledger(env.platform.value, "write")
