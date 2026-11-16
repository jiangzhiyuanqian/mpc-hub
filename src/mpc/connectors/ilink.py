"""微信 iLink 连接器（腾讯官方"微信对话"机器人通道，bot_type=3）。

协议细节全部取自本机 `@michengai/dsh-im-connect` 的 `lib/channels/weixin.js`
（与官方插件同源实现），逐条对照如下：

| 用途 | 调用 |
|---|---|
| 取二维码 | `POST /ilink/bot/get_bot_qrcode?bot_type=3` → `{qrcode, qrcode_img_content}` |
| 查扫码状态 | `POST /ilink/bot/get_qrcode_status?qrcode=<id>` → `{status:'confirmed', bot_token, ilink_user_id}` |
| 拉消息 | `POST /ilink/bot/getupdates` body `{get_updates_buf: cursor, base_info:{channel_version:'1.0.0'}}` |
| 发消息 | `POST /ilink/bot/sendmessage` body 见 `send()` |

请求头：`content-type: application/json`、`iLink-App-ClientVersion: 1`、
`X-WECHAT-UIN: <uin>`，带 token 时加
`Authorization: Bearer <bot_token>` 与 `AuthorizationType: ilink_bot_token`。

平台限制（官方同款 README 注释）：**仅私聊、一个账号一个 poller、建议使用专用小号**。
本连接器据此设计：不做群聊、不做媒体上传（媒体要走 CDN 上传接口，另行实现），
并且默认只处理文本。

用法：

```python
conn = ILinkConnector(bot_token="...", on_message=handle)
conn.start()          # 后台线程长轮询
```
"""

from __future__ import annotations

import base64
import random
import threading
import time
import uuid
from typing import Any, Callable

import httpx

from ..contracts import Action, Capability, ConversationKind, Envelope, Platform
from .base import Connector

BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "1.0.0"

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

MESSAGE_TYPE_TEXT = 1


class ILinkError(RuntimeError):
    """iLink 通道错误。"""


class ILinkDeliveryRejected(ILinkError):
    """平台明确拒收（429/503 或 ret=-2）：消息未入队，可以安全重发。"""


class ILinkStaleToken(ILinkError):
    """bot_token 过期（errcode=-14），需要重新扫码。"""


def _pick(data: dict[str, Any], *names: str) -> str:
    for name in names:
        value = data.get(name)
        if isinstance(value, str) and value:
            return value
    return ""


def _pick_any(data: dict[str, Any], *names: str) -> str:
    """取值并转成字符串：数字型 id（官方接口的 message_id 就是 int）也能取到。"""
    for name in names:
        value = data.get(name)
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, float):
            return str(int(value))
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            inner = _pick_any(value, "id", "value", "str")
            if inner:
                return inner
    return ""


def _merge_message(raw: dict[str, Any]) -> dict[str, Any]:
    """官方实现会把嵌套的 `message` 对象平铺到外层，这里对齐。"""
    if not isinstance(raw, dict):
        return {}
    inner = raw.get("message")
    if isinstance(inner, dict):
        merged = dict(raw)
        merged.update(inner)
        return merged
    return raw


class ILinkConnector(Connector):
    """微信 iLink 通道适配器。"""

    platform = Platform.WECHAT

    def __init__(
        self,
        *,
        bot_token: str = "",
        uin: str = "",
        base_url: str = BASE_URL,
        poll_timeout_secs: int = 70,
        on_message: Callable[[Envelope], None] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.bot_token = bot_token
        self.uin = uin
        self.base_url = base_url.rstrip("/")
        self.poll_timeout_secs = poll_timeout_secs
        self.on_message = on_message
        self._client = client or httpx.Client(timeout=30.0)
        self._cursor = ""
        self._context_tokens: dict[str, str] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # 真实健康状态（界面据此显示"轮询中 / 出错了"，而不是"已注册"这种假状态）
        self._running = False
        self._last_error = ""
        self._last_poll_at = 0.0
        self._received = 0
        self._consecutive_errors = 0
        self._stale_token = False

    # ---------- 能力 ----------

    def capability(self) -> Capability:
        return Capability(
            platform=self.platform,
            can_read=True,
            can_send=True,
            can_send_media=False,  # 媒体需走 CDN 上传接口，本版未实现
            supports_group=False,  # iLink 仅私聊
            supports_markdown=False,  # 纯文本
            max_text_length=1000,
            requires_owner_approval=False,
            notes="微信 iLink 官方机器人通道；仅私聊，建议专用小号",
        )

    # ---------- HTTP ----------

    def _headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/json",
            "iLink-App-ClientVersion": "1",
            # 官方实现无条件发这个头（没配置时用随机 base64 串占位），照做以免服务端拒收
            "X-WECHAT-UIN": self.uin or self._random_uin(),
        }
        if self.bot_token:
            headers["Authorization"] = f"Bearer {self.bot_token}"
            headers["AuthorizationType"] = "ilink_bot_token"
        return headers

    @staticmethod
    def _random_uin() -> str:
        """官方做法：随机数字的 base64，用作缺省 X-WECHAT-UIN。"""
        return base64.b64encode(str(random.randint(0, 0xFFFFFFFF)).encode()).decode()

    def _request(
        self,
        path: str,
        body: dict[str, Any],
        timeout: float = 20.0,
        tolerate_ret1: bool = False,
    ) -> dict[str, Any]:
        resp = self._client.post(
            f"{self.base_url}{path}",
            json=body,
            headers=self._headers(),
            timeout=timeout,
        )
        if resp.status_code != 200:
            message = f"weixin {path} http {resp.status_code}"
            if path.endswith("/sendmessage") and resp.status_code in (429, 503):
                raise ILinkDeliveryRejected(message)
            raise ILinkError(message)
        data = resp.json()
        ret = int(data.get("ret", 0) or 0)
        errcode = int(data.get("errcode", 0) or 0)
        if tolerate_ret1 and ret == 1 and errcode == 0:
            return data
        if ret != 0 or errcode != 0:
            message = (
                f"weixin {path} ret={ret} errcode={errcode} "
                f"{str(data.get('errmsg', ''))}"
            )
            if path.endswith("/sendmessage") and ret == -2 and errcode == 0:
                raise ILinkDeliveryRejected(message)
            if ret == -14 or errcode == -14:
                raise ILinkStaleToken(message + "（bot_token 过期，请重新扫码登录）")
            raise ILinkError(message)
        return data

    # ---------- 扫码登录 ----------

    def login_qr(self) -> dict[str, str]:
        """取一张登录二维码。

        返回 `{"qrcode_id": ..., "qr_url": ...}`；把 qr_url 给主人扫码即可。
        """
        data = self._request("/ilink/bot/get_bot_qrcode?bot_type=3", {}, timeout=20.0)
        qrcode_id = _pick(data, "qrcode", "qrcode_id")
        qr_url = _pick(data, "qrcode_img_content", "qrcode_url", "url")
        if not qrcode_id or not qr_url:
            raise ILinkError("weixin 二维码字段缺失")
        return {"qrcode_id": qrcode_id, "qr_url": qr_url}

    def login_status(self, qrcode_id: str) -> dict[str, Any]:
        """查扫码状态。`status == "confirmed"` 时带 `bot_token` / `ilink_user_id`。"""
        data = self._request(
            f"/ilink/bot/get_qrcode_status?qrcode={qrcode_id}",
            {},
            timeout=45.0,
            tolerate_ret1=True,
        )
        status = str(data.get("status", ""))
        result: dict[str, Any] = {"status": status}
        token = _pick(data, "bot_token")
        user_id = _pick(data, "ilink_user_id")
        if token:
            result["bot_token"] = token
            self.bot_token = token
        if user_id:
            result["ilink_user_id"] = user_id
        return result

    # ---------- 入站 ----------

    def parse_message(self, payload: dict[str, Any]) -> Envelope | None:
        """把 iLink 的一条 msg 翻译成统一契约。

        对齐官方 `parseInbound`：
        - 平铺嵌套的 `message` 对象；
        - 只处理 `message_type == 1`（文本），其余类型返回 None；
        - 语音转写带 `[语音] ` 前缀；
        - 文本用空串拼接（官方是 `parts.join('')`）。
        """
        raw = payload.get("msg") if isinstance(payload.get("msg"), dict) else payload
        msg = _merge_message(raw)
        if not msg:
            return None

        raw_type = msg.get("message_type")
        if raw_type is not None and int(raw_type) != MESSAGE_TYPE_TEXT:
            return None

        sender_id = _pick_any(msg, "from_user_id", "from_user")
        if not sender_id:
            return None

        parts: list[str] = []
        text = msg.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
        attachments: list[str] = []
        for item in msg.get("item_list") or []:
            if not isinstance(item, dict):
                continue
            kind = int(item.get("type", 0) or 0)
            if kind == ITEM_TEXT:
                body = item.get("text_item") or {}
                value = body.get("text")
                if isinstance(value, str) and value:
                    parts.append(value)
            elif kind == ITEM_VOICE:
                body = item.get("voice_item") or {}
                value = body.get("text")  # 语音已由平台 ASR 转写
                if isinstance(value, str) and value:
                    parts.append("[语音] " + value)
                else:
                    attachments.append("ilink:voice")
            else:
                attachments.append("ilink:item:" + str(kind))

        context_token = _pick_any(msg, "context_token")
        if context_token and sender_id:
            self._context_tokens[sender_id] = context_token

        created_ms = msg.get("create_time_ms")
        if created_ms:
            timestamp = float(created_ms) / 1000.0
        else:
            timestamp = float(msg.get("timestamp") or msg.get("create_time") or time.time())
        message_id = (
            _pick_any(msg, "message_id", "msg_id", "id")
            or sender_id + ":" + str(timestamp)
        )

        return Envelope(
            message_id=message_id,
            platform=self.platform,
            conversation_id=sender_id,  # 仅私聊：会话即对方
            conversation_kind=ConversationKind.PRIVATE,
            sender_id=sender_id,
            sender_name="",
            text="".join(parts),
            is_mention=True,  # 私聊无需 @
            attachments=attachments,
            reply_to_message_id=_pick_any(msg, "reply_to_message_id") or None,
            timestamp=timestamp,
            raw=payload,
        )

    def to_envelope(self, payload: dict[str, Any]) -> Envelope:
        """把 iLink 的 msg 对象翻译成统一契约（非文本消息抛错）。"""
        envelope = self.parse_message(payload)
        if envelope is None:
            raise ValueError("不是可处理的文本消息")
        return envelope

    # ---------- 出站 ----------

    def send(self, action: Action) -> bool:
        to_user_id = action.conversation_id
        if not to_user_id:
            return False
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"mpc-hub:{int(time.time() * 1000)}:{uuid.uuid4().hex[:6]}",
                "message_type": 2,
                "message_state": 2,
                "context_token": self._context_tokens.get(to_user_id, ""),
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": action.text}}],
            },
            "base_info": {"channel_version": CHANNEL_VERSION},
        }
        self._request("/ilink/bot/sendmessage", body, timeout=20.0)
        return True

    # ---------- 长轮询 ----------

    def start(self) -> None:
        """后台线程长轮询拉消息，交给 `on_message` 回调。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="ilink-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3.0)
        self._thread = None

    def _poll_loop(self) -> None:
        """长轮询拉消息。

        对齐官方：游标优先用 `get_updates_buf`，消息列表认 `msgs`/`messages`/`updates`；
        出错不再静默吞掉，而是记进 `_last_error`，界面能看到"为什么没回消息"。
        """
        self._running = True
        while not self._stop.is_set():
            try:
                data = self._request(
                    "/ilink/bot/getupdates",
                    {
                        "get_updates_buf": self._cursor,
                        "base_info": {"channel_version": CHANNEL_VERSION},
                    },
                    timeout=self.poll_timeout_secs + 10.0,
                )
            except ILinkStaleToken as exc:
                self._last_error = str(exc)
                self._consecutive_errors += 1
                time.sleep(3.0)
                continue
            except Exception as exc:  # noqa: BLE001
                self._last_error = "拉取消息失败：" + (str(exc) or type(exc).__name__)
                self._consecutive_errors += 1
                time.sleep(3.0)
                continue

            self._consecutive_errors = 0
            self._last_error = ""
            self._last_poll_at = time.time()
            # 官方取值顺序：get_updates_buf → cursor → sync_buf
            self._cursor = (
                _pick(data, "get_updates_buf", "cursor", "sync_buf", "next_cursor")
                or self._cursor
            )
            raw_list = (
                data.get("msgs")
                or data.get("messages")
                or data.get("updates")
                or data.get("msg_list")
                or []
            )
            for msg in raw_list:
                if not isinstance(msg, dict):
                    continue
                try:
                    envelope = self.parse_message(msg)
                except Exception:
                    continue
                if envelope is None or not envelope.sender_id or not envelope.text:
                    continue
                self._received += 1
                if self.on_message is not None:
                    try:
                        self.on_message(envelope)
                    except Exception:
                        pass
        self._running = False

    def status(self) -> dict[str, Any]:
        """真实健康状态：界面据此显示"轮询中 / 出错了"，而不是"已注册"。"""
        if self._last_error:
            state = "error"
            detail = self._last_error
        elif self._running:
            state = "running"
            detail = "正在监听新消息（已收 " + str(self._received) + " 条）"
        elif self.bot_token:
            state = "idle"
            detail = "已配置，尚未开始监听"
        else:
            state = "idle"
            detail = "还没扫码登录，没有凭据"
        return {
            "state": state,
            "running": self._running,
            "detail": detail,
            "last_error": self._last_error,
            "last_poll_at": self._last_poll_at,
            "received": self._received,
            "consecutive_errors": self._consecutive_errors,
        }

    def close(self) -> None:
        self.stop()
        try:
            self._client.close()
        except Exception:
            pass
