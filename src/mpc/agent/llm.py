"""LLM 接入层。

对外只暴露一个 `chat()` 接口，内部对接任何 OpenAI 兼容端点
（DeepSeek / MiniMax / 通义 / vLLM / Ollama 都可以）。

关键设计：**没有 Key 也能跑通全链路**。配置缺失时自动切到
`MockLLM`，它按场景返回确定性的答复。这样整条链路（入站→快判→
生成→闸门→发送）可以在离线机器上端到端验证。

真实端点这边提供：
- 指数退避重试（限流 / 5xx / 网络抖动），
- 可读的报错（401 说 Key，402 说额度，404 说模型名或 base_url），
- 流式输出（终端里逐字打印），
- 结构化输出优先用 response_format，不支持时自动回退到正则抠 JSON。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx


@dataclass
class LLMConfig:
    base_url: str = field(default_factory=lambda: os.getenv("MPC_LLM_BASE_URL", "https://api.deepseek.com/v1"))
    api_key: str = field(default_factory=lambda: os.getenv("MPC_LLM_API_KEY", ""))
    model: str = field(default_factory=lambda: os.getenv("MPC_LLM_MODEL", "deepseek-chat"))
    fast_model: str = field(default_factory=lambda: os.getenv("MPC_LLM_FAST_MODEL", "deepseek-chat"))
    temperature: float = float(os.getenv("MPC_LLM_TEMPERATURE", "0.3"))
    timeout: float = float(os.getenv("MPC_LLM_TIMEOUT", "30"))
    max_tokens: int = int(os.getenv("MPC_LLM_MAX_TOKENS", "800"))
    max_retries: int = int(os.getenv("MPC_LLM_MAX_RETRIES", "2"))
    json_mode: bool = os.getenv("MPC_LLM_JSON_MODE", "1") not in {"0", "false", "False"}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


class LLMError(RuntimeError):
    """调用大模型失败。消息里已经写明"人该做什么"。"""


_RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


def _explain(status: int, body: str) -> str:
    """把状态码翻译成人能直接照做的建议。"""
    head = body[:300].strip()
    if status == 401:
        return "API Key 无效或没有权限：检查 MPC_LLM_API_KEY（别把引号和空格一起复制进来）"
    if status == 402:
        return "账户额度不足：去充值或换一个有余额的 Key"
    if status == 404:
        return "接口或模型不存在：检查 MPC_LLM_BASE_URL（是否带 /v1）和 MPC_LLM_MODEL 的模型名"
    if status == 413:
        return "请求过大：调小 MPC_LLM_MAX_TOKENS，或缩短上下文"
    if status == 429:
        return "被限流：稍后重试，或换用更便宜的模型/更大的配额"
    if status >= 500:
        return f"服务端错误 {status}：对方服务暂时不可用，已自动重试过"
    return f"HTTP {status}：{head}"


class BaseLLM:
    def chat(
        self,
        system: str,
        user: str,
        *,
        fast: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        raise NotImplementedError

    def stream_chat(
        self,
        system: str,
        user: str,
        *,
        fast: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        """流式输出。默认退化成一次性返回。"""
        text = self.chat(system, user, fast=fast, temperature=temperature, max_tokens=max_tokens)
        if text:
            yield text

    def json_chat(self, system: str, user: str, *, fast: bool = True) -> dict[str, Any]:
        """要求模型返回 JSON；失败时抛 LLMError。"""
        raw = self.chat(system + "\n\n只输出 JSON，不要任何额外文字或代码块标记。", user, fast=fast)
        return parse_json_object(raw)


def parse_json_object(raw: str) -> dict[str, Any]:
    """从模型输出里抠出一个 JSON 对象（容忍 ```json 包裹和前后废话）。"""
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        text = fenced.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise LLMError(f"模型未返回 JSON：{raw[:200]}")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise LLMError(f"模型返回的 JSON 无法解析：{raw[:200]}") from exc


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _is_local_endpoint(base_url: str) -> bool:
    """判断 base_url 是否指向本机。

    本机端点（如 Ollama 的 127.0.0.1:11434）绝不能走系统代理：
    Windows 上 httpx 的 trust_env 会去读注册表里的 WinINET 代理设置，
    于是本机请求被甩给代理，代理连不到 127.0.0.1，直接返回 502 空响应。
    """
    try:
        host = (urlparse(base_url).hostname or "").lower()
    except ValueError:
        return False
    return host in _LOCAL_HOSTS or host.startswith("127.")


class OpenAICompatLLM(BaseLLM):
    """对接任何 OpenAI 兼容 /chat/completions 端点。"""
    def __init__(self, cfg: LLMConfig | None = None, client: httpx.Client | None = None) -> None:
        self.cfg = cfg or LLMConfig()
        self._client = client
        self._owned = client is None
        self.last_usage: dict[str, Any] = {}

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.cfg.timeout,
                trust_env=not _is_local_endpoint(self.cfg.base_url),
            )
        return self._client

    def close(self) -> None:
        if self._owned and self._client is not None:
            self._client.close()
            self._client = None

    # ---------- 内部 ----------

    def _model(self, fast: bool) -> str:
        return self.cfg.fast_model if fast else self.cfg.model

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }

    def _payload(
        self,
        system: str,
        user: str,
        *,
        fast: bool,
        temperature: float | None,
        max_tokens: int | None,
        stream: bool,
        json_mode: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._model(fast),
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "stream": stream,
        }
        if json_mode and self.cfg.json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        last_error: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                resp = self.client.post(url, json=payload, headers=self._headers())
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt < self.cfg.max_retries:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise LLMError(f"请求 {url} 失败（已重试 {attempt} 次）：{exc}") from exc
            if resp.status_code in _RETRYABLE_STATUS and attempt < self.cfg.max_retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return resp
        raise LLMError(f"请求 {url} 失败：{last_error}")

    # ---------- 对外 ----------

    def chat(
        self,
        system: str,
        user: str,
        *,
        fast: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        payload = self._payload(
            system, user, fast=fast, temperature=temperature, max_tokens=max_tokens, stream=False
        )
        resp = self._post(payload)
        if resp.status_code >= 400:
            raise LLMError(_explain(resp.status_code, resp.text))
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(f"响应不是合法 JSON：{resp.text[:200]}") from exc
        self.last_usage = data.get("usage") or {}
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"响应格式异常：{resp.text[:200]}") from exc
        return str(content or "").strip()

    def json_chat(self, system: str, user: str, *, fast: bool = True) -> dict[str, Any]:
        """优先让服务端保证 JSON；服务端不认 response_format 时自动回退。"""
        sys_text = system + "\n\n只输出 JSON，不要任何额外文字或代码块标记。"
        payload = self._payload(sys_text, user, fast=fast, temperature=None, max_tokens=None, stream=False, json_mode=True)
        resp = self._post(payload)
        if resp.status_code < 400:
            try:
                data = resp.json()
                self.last_usage = data.get("usage") or {}
                raw = data["choices"][0]["message"]["content"]
                return parse_json_object(str(raw or ""))
            except (KeyError, IndexError, TypeError, ValueError):
                pass  # 落到下面的普通模式再试一次
        elif resp.status_code not in (400, 404, 422):
            raise LLMError(_explain(resp.status_code, resp.text))
        raw = self.chat(sys_text, user, fast=fast)
        return parse_json_object(raw)

    def stream_chat(
        self,
        system: str,
        user: str,
        *,
        fast: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        payload = self._payload(
            system, user, fast=fast, temperature=temperature, max_tokens=max_tokens, stream=True
        )
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        try:
            with self.client.stream(
                "POST", url, json=payload, headers=self._headers()
            ) as resp:
                if resp.status_code >= 400:
                    body = resp.read().decode("utf-8", "replace")
                    raise LLMError(_explain(resp.status_code, body))
                for line in resp.iter_lines():
                    if not line:
                        continue
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    for choice in chunk.get("choices") or []:
                        piece = (choice.get("delta") or {}).get("content")
                        if piece:
                            yield str(piece)
        except httpx.HTTPError as exc:
            raise LLMError(f"流式请求 {url} 失败：{exc}") from exc


class MockLLM(BaseLLM):
    """离线替身：按内容给出确定性回复，让全链路可测。"""

    def chat(
        self,
        system: str,
        user: str,
        *,
        fast: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        if "JSON" in system or "json" in system.lower():
            return self._mock_json(user)
        text = self._customer_text(user)
        if any(k in text for k in ("多少钱", "价格", "报价", "费用")):
            return "您好，价格方案与具体规格相关，方便说明一下您的使用场景吗？我这边帮您确认准确报价。"
        if any(k in text for k in ("怎么用", "使用方法", "教程", "文档")):
            return "您好，把具体场景告诉我，我给您一步步说明操作流程。"
        if any(k in text for k in ("你好", "您好", "hi", "hello", "在吗")):
            return "您好，我是客服助手，请问有什么可以帮您？"
        return f"收到您说的「{text[:40]}」，我先记录下来，稍后给您准确答复。"

    @staticmethod
    def _customer_text(user: str) -> str:
        """从提示词里取出"客户本条消息"。

        生成阶段的提示词前面还带着【近期对话】，只看第一行会把客户原话漏掉，
        导致判断与草稿都跑偏。这里按标记截取，没有标记时退回整段文本。
        """
        text = user.strip()
        marker = "【客户本条消息】"
        if marker in text:
            text = text.split(marker, 1)[1].strip()
        return text

    @staticmethod
    def _mock_json(user: str) -> str:
        """模拟快判输出：招呼类不回复，其余按提问处理。"""
        text = user.strip()
        if any(k in text for k in ("你好", "您好", "在吗", "hi", "hello", "哈喽")) and len(text) <= 12:
            return '{"intention": "chitchat", "confidence": 0.95, "need_reply": false, "reason": "纯打招呼"}'
        if any(k in text for k in ("投诉", "退货", "退款", "差评")):
            return '{"intention": "complaint", "confidence": 0.88, "need_reply": true, "reason": "投诉类必须回复"}'
        if any(k in text for k in ("多少钱", "价格", "报价", "费用")):
            return '{"intention": "pricing", "confidence": 0.8, "need_reply": true, "reason": "价格咨询"}'
        return '{"intention": "question", "confidence": 0.75, "need_reply": true, "reason": "一般提问"}'


def build_llm(cfg: LLMConfig | None = None) -> BaseLLM:
    """按配置选真实客户端或离线替身。"""
    cfg = cfg or LLMConfig()
    if cfg.enabled:
        return OpenAICompatLLM(cfg)
    return MockLLM()
