"""桌面设置窗口（tkinter）。

这是给主人用的那一屏：**所有 Key 都由用户在这个窗口里自己填**，
程序不扫描、不复用本机其它软件留下的凭据。填完点"保存"，写到
`~/.mpc-hub/config.json`（可用 `MPC_CONFIG` 改路径），权限尽量收紧到仅本人可读。

五个页签：
1. 模型接口   —— 供应商 / Base URL / Key / 模型名，含"拉取模型列表"和"测试连接"
2. 平台接入   —— 微信 iLink 扫码登录、OneBot、Webhook 出站、自定义连接器
3. 治理规则   —— 白名单 / 时段 / 配额 / 字数上限 / 审批开关
4. 对话测试   —— 直接在本窗口里跟客服 agent 说话，验证 Key 和模型是否好用
5. 运行状态   —— 能力矩阵、实时指标、审计流水、微信通道连接/断开

网络请求一律放后台线程，界面不卡死。
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable

from .runtime import Runtime, build_runtime, load_settings
from .settings import PRESETS, Settings

APP_TITLE = "多平台连接中心 · 客服后台设置"


# ---------------------------------------------------------------- 小工具


def _split_items(text: str) -> list[str]:
    """把用户输入的一行（逗号/空格/换行分隔）拆成列表。"""
    raw = (text or "").replace("\n", ",").replace("，", ",")
    parts = [p.strip() for chunk in raw.split(",") for p in chunk.split() if p.strip()]
    return parts


def _fmt_time(ts: float) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(ts))
    except Exception:
        return str(ts)


def _render_qr_png(text: str, path: Path, size: int = 260) -> bool:
    """把一段文本渲染成二维码 PNG。失败返回 False（调用方走浏览器兜底）。"""
    try:
        import qrcode  # type: ignore
        from qrcode.image.pure import PyPNGImage  # type: ignore
    except Exception:
        return False
    try:
        img = qrcode.make(text, image_factory=PyPNGImage)
        # PyPNGImage 的 box_size 只能在 make 时指定，这里重新生成一次
        img = qrcode.make(text, image_factory=PyPNGImage, box_size=8, border=2)
        path.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(path))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 主窗口


class DesktopApp:
    def __init__(self, root: Any, settings: Settings | None = None) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.settings: Settings = settings or load_settings()
        self.config_path = Settings.path()
        self.runtime: Runtime | None = None
        self._bus: "queue.Queue[tuple[str, Any]]" = queue.Queue()
        self._qr_state: dict[str, Any] = {}

        root.title(APP_TITLE)
        root.geometry("860x660")
        root.minsize(760, 560)

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        self.tab_llm = ttk.Frame(self.notebook)
        self.tab_platform = ttk.Frame(self.notebook)
        self.tab_gov = ttk.Frame(self.notebook)
        self.tab_chat = ttk.Frame(self.notebook)
        self.tab_status = ttk.Frame(self.notebook)
        for name, frame, label in (
            ("llm", self.tab_llm, "模型接口"),
            ("platform", self.tab_platform, "平台接入"),
            ("gov", self.tab_gov, "治理规则"),
            ("chat", self.tab_chat, "对话测试"),
            ("status", self.tab_status, "运行状态"),
        ):
            frame.configure(padding=12)
            self.notebook.add(frame, text=label)

        self._build_llm_tab()
        self._build_platform_tab()
        self._build_gov_tab()
        self._build_chat_tab()
        self._build_status_tab()

        bar = ttk.Frame(root)
        bar.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(bar, text=f"配置文件：{self.config_path}").pack(side="left")
        ttk.Button(bar, text="重新读取", command=self.reload_settings).pack(side="right")
        ttk.Button(bar, text="保存配置", command=self.save_settings).pack(side="right", padx=6)

        self.status_var = tk.StringVar(value="就绪。Key 只写进上面的配置文件，不外传。")
        ttk.Label(root, textvariable=self.status_var, anchor="w").pack(
            fill="x", padx=10, pady=(0, 8)
        )

        self.root.after(200, self._drain_bus)

    # ---------- 通用工具 ----------

    def _thread(self, fn: Callable[[], Any]) -> None:
        def runner() -> None:
            try:
                result = fn()
            except Exception as exc:  # 后台线程里的异常不能炸掉界面
                self._bus.put(("error", str(exc)))
                return
            self._bus.put(("done", result))

        threading.Thread(target=runner, daemon=True).start()

    def _thread_to(self, fn: Callable[[], Any], kind: str) -> None:
        """后台线程跑 fn，成功后把结果按指定 kind 投递回界面线程。"""

        def runner() -> None:
            try:
                result = fn()
            except Exception as exc:  # 后台线程里的异常不能炸掉界面
                self._bus.put(("error", str(exc)))
                return
            self._bus.put((kind, result))

        threading.Thread(target=runner, daemon=True).start()

    def _drain_bus(self) -> None:
        try:
            while True:
                kind, payload = self._bus.get_nowait()
                if kind == "error":
                    self.status_var.set(f"出错了：{payload}")
                elif kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "models":
                    self._fill_models(payload)
                elif kind == "test_llm":
                    self._append_chat("系统", f"测试连接成功：{payload}")
                    self.status_var.set("测试连接成功")
                elif kind == "qr":
                    self._on_qr(payload)
                elif kind == "chat_reply":
                    self._append_chat("客服", str(payload))
                elif kind == "audit":
                    self._set_text(self.audit_text, str(payload))
                elif kind == "login_ok":
                    self.ilink_token_var.set(str(payload.get("token", "")))
                    self.ilink_enabled_var.set(True)
                    self._collect_platforms()
                    self.status_var.set(str(payload.get("message", "")))
                elif kind == "wechat_state":
                    self.status_var.set(f"连接器：{payload}")
                elif kind == "metrics":
                    self._fill_metrics(payload)
        except queue.Empty:
            pass
        self.root.after(300, self._drain_bus)

    # ---------- 页签 1：模型接口 ----------

    def _build_llm_tab(self) -> None:
        from tkinter import ttk

        f = self.tab_llm
        llm = self.settings.llm

        self.provider_var = self.tk.StringVar(value=llm.provider)
        self.base_url_var = self.tk.StringVar(value=llm.base_url)
        self.api_key_var = self.tk.StringVar(value=llm.api_key)
        self.model_var = self.tk.StringVar(value=llm.model)
        self.fast_model_var = self.tk.StringVar(value=llm.fast_model)
        self.temperature_var = self.tk.StringVar(value=str(llm.temperature))
        self.max_tokens_var = self.tk.StringVar(value=str(llm.max_tokens))
        self.models_var = self.tk.StringVar(value="")

        ttk.Label(f, text="供应商").grid(row=0, column=0, sticky="w", pady=4)
        combo = ttk.Combobox(
            f,
            textvariable=self.provider_var,
            values=[p.label for p in PRESETS.values()],
            state="readonly",
            width=34,
        )
        combo.grid(row=0, column=1, sticky="w")
        combo.bind("<<ComboboxSelected>>", self._on_provider_change)

        ttk.Label(f, text="Base URL").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.base_url_var, width=60).grid(row=1, column=1, sticky="w")

        ttk.Label(f, text="API Key").grid(row=2, column=0, sticky="w", pady=4)
        key_row = ttk.Frame(f)
        key_row.grid(row=2, column=1, sticky="w")
        self.key_entry = ttk.Entry(key_row, textvariable=self.api_key_var, width=52, show="*")
        self.key_entry.pack(side="left")
        ttk.Button(key_row, text="显示", width=6, command=self._toggle_key).pack(side="left", padx=6)

        ttk.Label(f, text="模型").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.model_var, width=60).grid(row=3, column=1, sticky="w")

        ttk.Label(f, text="快模型").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.fast_model_var, width=60).grid(row=4, column=1, sticky="w")

        ttk.Label(f, text="temperature").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.temperature_var, width=12).grid(row=5, column=1, sticky="w")

        ttk.Label(f, text="max_tokens").grid(row=6, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.max_tokens_var, width=12).grid(row=6, column=1, sticky="w")

        btn_row = ttk.Frame(f)
        btn_row.grid(row=7, column=0, columnspan=2, sticky="w", pady=12)
        ttk.Button(btn_row, text="拉取模型列表", command=self.fetch_models).pack(side="left")
        ttk.Button(btn_row, text="测试连接", command=self.test_llm).pack(side="left", padx=8)
        ttk.Button(btn_row, text="清空 Key", command=self._clear_key).pack(side="left")

        ttk.Label(f, text="可用的模型名（双击填入）").grid(row=8, column=0, sticky="nw", pady=(10, 0))
        self.models_list = self.tk.Listbox(f, height=10, width=60)
        self.models_list.grid(row=8, column=1, sticky="w", pady=(10, 0))
        self.models_list.bind("<Double-Button-1>", self._on_pick_model)

        ttk.Label(
            f,
            text=(
                "提示：Base URL 只填到 /v1 这一层，不要带 /chat/completions。\n"
                "接口优先用云端（DeepSeek / MiniMax 等）；本地模型只作离线备选，建议 ≤7B。"
            ),
            foreground="#666",
        ).grid(row=9, column=0, columnspan=2, sticky="w", pady=(12, 0))

    def _toggle_key(self) -> None:
        self.key_entry.configure(show="" if self.key_entry.cget("show") else "*")

    def _clear_key(self) -> None:
        self.api_key_var.set("")

    def _on_provider_change(self, _event: Any = None) -> None:
        label = self.provider_var.get()
        for preset in PRESETS.values():
            if preset.label == label:
                self.base_url_var.set(preset.base_url)
                self.model_var.set(preset.model)
                self.fast_model_var.set(preset.fast_model)
                self.status_var.set(
                    preset.notes or f"已套用 {preset.label} 的默认地址与模型名，可自行修改。"
                )
                return

    def _collect_llm(self) -> None:
        llm = self.settings.llm
        label = self.provider_var.get()
        for preset in PRESETS.values():
            if preset.label == label:
                llm.provider = preset.key
                break
        llm.base_url = self.base_url_var.get().strip().rstrip("/")
        llm.api_key = self.api_key_var.get().strip()
        llm.model = self.model_var.get().strip()
        llm.fast_model = self.fast_model_var.get().strip()
        try:
            llm.temperature = float(self.temperature_var.get())
        except ValueError:
            pass
        try:
            llm.max_tokens = int(self.max_tokens_var.get())
        except ValueError:
            pass

    def fetch_models(self) -> None:
        self._collect_llm()
        base = self.settings.llm.base_url
        key = self.settings.llm.api_key
        if not base:
            self.status_var.set("先填 Base URL")
            return

        def work() -> list[str]:
            import httpx

            headers = {"Authorization": f"Bearer {key}"} if key else {}
            resp = httpx.get(f"{base}/models", headers=headers, timeout=20.0)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}：{resp.text[:180]}")
            data = resp.json()
            items = data.get("data") if isinstance(data, dict) else None
            names = sorted({str(i.get("id")) for i in (items or []) if isinstance(i, dict)})
            return names or ["（接口返回里没有模型列表）"]

        self.status_var.set("正在拉取模型列表…")
        self._thread_to(work, "models")

    def _fill_models(self, names: list[str]) -> None:
        self.models_list.delete(0, "end")
        for name in names:
            self.models_list.insert("end", name)
        self.models_var.set(", ".join(names[:6]))
        self.status_var.set(f"拿到 {len(names)} 个模型名，双击可填入上面的输入框")

    def _on_pick_model(self, _event: Any = None) -> None:
        sel = self.models_list.curselection()
        if sel:
            self.model_var.set(self.models_list.get(sel[0]))

    def test_llm(self) -> None:
        self._collect_llm()
        from .agent.llm import build_llm

        cfg = self.settings.llm.to_llm_config()
        if not cfg.api_key and self.settings.llm.provider not in {"ollama"}:
            self.status_var.set("还没填 Key —— 现在测的是离线 Mock 回复。")
        self.status_var.set("正在测试连接…")

        def work() -> str:
            llm = build_llm(cfg)
            reply = llm.chat("你是一个连通性测试助手。", "只回复四个字：连接正常")
            return (reply or "").strip()[:120]

        self._thread_to(work, "test_llm")

    # ---------- 页签 2：平台接入 ----------

    def _build_platform_tab(self) -> None:
        from tkinter import ttk

        f = self.tab_platform
        plats = self.settings.platforms

        # --- 微信 iLink ---
        ttk.Label(f, text="微信 iLink（官方机器人通道 · 仅私聊 · 建议专用小号）").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4)
        )
        self.ilink_enabled_var = self.tk.BooleanVar(value=plats.wechat_ilink.enabled)
        ttk.Checkbutton(
            f, text="启用微信通道", variable=self.ilink_enabled_var
        ).grid(row=1, column=0, sticky="w")
        ttk.Label(f, text="bot_token").grid(row=2, column=0, sticky="w", pady=4)
        self.ilink_token_var = self.tk.StringVar(value=plats.wechat_ilink.bot_token)
        ttk.Entry(f, textvariable=self.ilink_token_var, width=52).grid(row=2, column=1, sticky="w")
        ttk.Label(f, text="uin（可留空）").grid(row=3, column=0, sticky="w", pady=4)
        self.ilink_uin_var = self.tk.StringVar(value=plats.wechat_ilink.uin)
        ttk.Entry(f, textvariable=self.ilink_uin_var, width=52).grid(row=3, column=1, sticky="w")

        qr_row = ttk.Frame(f)
        qr_row.grid(row=4, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Button(qr_row, text="扫码登录（取二维码）", command=self.start_qr_login).pack(side="left")
        ttk.Button(qr_row, text="在浏览器打开登录页", command=self._open_qr_browser).pack(
            side="left", padx=8
        )
        self.qr_label = ttk.Label(qr_row, text="（尚未获取二维码）")
        self.qr_label.pack(side="left", padx=10)
        self.qr_status_var = self.tk.StringVar(value="")
        ttk.Label(f, textvariable=self.qr_status_var).grid(
            row=5, column=0, columnspan=3, sticky="w"
        )

        # --- OneBot / Webhook ---
        ttk.Label(f, text="QQ / OneBot v11").grid(row=6, column=0, sticky="w", pady=(14, 4))
        self.onebot_enabled_var = self.tk.BooleanVar(value=plats.onebot.enabled)
        ttk.Checkbutton(f, text="启用", variable=self.onebot_enabled_var).grid(
            row=6, column=1, sticky="w"
        )
        ttk.Label(f, text="OneBot 地址").grid(row=7, column=0, sticky="w")
        self.onebot_url_var = self.tk.StringVar(value=plats.onebot.base_url)
        ttk.Entry(f, textvariable=self.onebot_url_var, width=52).grid(row=7, column=1, sticky="w")
        ttk.Label(f, text="access_token").grid(row=8, column=0, sticky="w")
        self.onebot_token_var = self.tk.StringVar(value=plats.onebot.access_token)
        ttk.Entry(f, textvariable=self.onebot_token_var, width=52).grid(row=8, column=1, sticky="w")

        ttk.Label(f, text="通用 Webhook 出站").grid(row=9, column=0, sticky="w", pady=(14, 4))
        self.webhook_enabled_var = self.tk.BooleanVar(value=plats.webhook.enabled)
        ttk.Checkbutton(f, text="启用", variable=self.webhook_enabled_var).grid(
            row=9, column=1, sticky="w"
        )
        ttk.Label(f, text="出站地址").grid(row=10, column=0, sticky="w")
        self.webhook_url_var = self.tk.StringVar(value=plats.webhook.outbound_url)
        ttk.Entry(f, textvariable=self.webhook_url_var, width=52).grid(row=10, column=1, sticky="w")
        ttk.Label(f, text="token（可选，放 X-MPC-Token 头）").grid(row=11, column=0, sticky="w")
        self.webhook_token_var = self.tk.StringVar(value=plats.webhook.token)
        ttk.Entry(f, textvariable=self.webhook_token_var, width=52).grid(row=11, column=1, sticky="w")

        # --- 自定义连接器 ---
        ttk.Label(f, text="自定义连接器（扩展点：模块路径 + 工厂函数）").grid(
            row=12, column=0, columnspan=3, sticky="w", pady=(14, 4)
        )
        ttk.Label(f, text="模块").grid(row=13, column=0, sticky="w")
        self.custom_module_var = self.tk.StringVar(value="")
        ttk.Entry(f, textvariable=self.custom_module_var, width=52).grid(row=13, column=1, sticky="w")
        ttk.Label(f, text="平台标识").grid(row=14, column=0, sticky="w")
        self.custom_platform_var = self.tk.StringVar(value="")
        ttk.Entry(f, textvariable=self.custom_platform_var, width=52).grid(row=14, column=1, sticky="w")
        ttk.Label(f, text="参数 JSON").grid(row=15, column=0, sticky="w")
        self.custom_options_var = self.tk.StringVar(value="{}")
        ttk.Entry(f, textvariable=self.custom_options_var, width=52).grid(row=15, column=1, sticky="w")

        ttk.Label(
            f,
            text=(
                "扩展点有两种写法：① 这里填「模块路径 + 平台标识」，程序 import 后调用模块里的 create(options)\n"
                "或 create(**options)；② 你的包里注册入口点 [project.entry-points.'mpc.connectors']，"
                "程序启动时自动发现。详见 README 的「加一个新平台」。"
            ),
            foreground="#666",
        ).grid(row=16, column=0, columnspan=3, sticky="w", pady=(10, 0))

    # ---------- 页签 3：治理规则 ----------

    def _build_gov_tab(self) -> None:
        from tkinter import ttk

        f = self.tab_gov
        gov = self.settings.governance

        self.allowlist_var = self.tk.StringVar(value=", ".join(gov.allowlist))
        self.owner_ids_var = self.tk.StringVar(value=", ".join(gov.owner_ids))
        self.blocklist_var = self.tk.StringVar(value=", ".join(gov.blocklist))
        self.hours_var = self.tk.StringVar(
            value=f"{gov.active_hours[0]} - {gov.active_hours[1]}"
        )
        self.read_quota_var = self.tk.StringVar(value=str(gov.daily_read_quota))
        self.write_quota_var = self.tk.StringVar(value=str(gov.daily_write_quota))
        self.max_chars_var = self.tk.StringVar(value=str(gov.max_reply_chars))
        self.req_commit_var = self.tk.BooleanVar(value=gov.require_approval_on_commitment)
        self.req_group_var = self.tk.BooleanVar(value=gov.require_approval_groups)
        self.group_mention_var = self.tk.BooleanVar(value=gov.group_mention_only)
        self.drop_ping_var = self.tk.BooleanVar(value=gov.drop_ping)

        def line(row: int, label: str, var: Any, hint: str = "") -> None:
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(f, textvariable=var, width=52).grid(row=row, column=1, sticky="w")
            if hint:
                ttk.Label(f, text=hint, foreground="#888").grid(row=row, column=2, sticky="w")

        line(0, "白名单（逗号分隔）", self.allowlist_var, "留空 = 不限")
        line(1, "主人 ID", self.owner_ids_var, "主人不受白名单限制")
        line(2, "黑名单", self.blocklist_var, "直接拦掉")
        line(3, "服务时段（起 - 止）", self.hours_var, "本地时间小时，0-24 表示全天")
        line(4, "每日读取上限", self.read_quota_var)
        line(5, "每日发送上限", self.write_quota_var)
        line(6, "单条回复字数上限", self.max_chars_var)

        checks = [
            ("承诺性话术需人工确认（转账/下单/保证）", self.req_commit_var),
            ("群聊消息一律需人工确认", self.req_group_var),
            ("群聊只在被 @ 时回复", self.group_mention_var),
            ("丢弃纯问候/ping", self.drop_ping_var),
        ]
        for i, (text, var) in enumerate(checks):
            ttk.Checkbutton(f, text=text, variable=var).grid(
                row=7 + i, column=0, columnspan=2, sticky="w", pady=2
            )

    # ---------- 页签 4：对话测试 ----------

    def _build_chat_tab(self) -> None:
        from tkinter import ttk

        f = self.tab_chat
        self.chat_log = self.tk.Text(f, height=20, width=80, state="disabled")
        self.chat_log.pack(fill="both", expand=True)
        row = ttk.Frame(f)
        row.pack(fill="x", pady=6)
        self.chat_input = ttk.Entry(row, width=70)
        self.chat_input.pack(side="left")
        self.chat_input.bind("<Return>", lambda _e: self.send_chat())
        ttk.Button(row, text="发送", command=self.send_chat).pack(side="left", padx=6)
        ttk.Button(row, text="清空历史", command=self.clear_chat).pack(side="left")
        self._append_chat("系统", "在这里直接跟客服 agent 说话。先在「模型接口」页填好 Key 并保存。")

    def _append_chat(self, who: str, text: str) -> None:
        self.chat_log.configure(state="normal")
        self.chat_log.insert("end", f"[{_fmt_time(time.time())}] {who}：{text}\n\n")
        self.chat_log.see("end")
        self.chat_log.configure(state="disabled")

    def send_chat(self) -> None:
        text = self.chat_input.get().strip()
        if not text:
            return
        self.chat_input.delete(0, "end")
        self._append_chat("我", text)
        self._collect_llm()
        from .agent.llm import build_llm

        cfg = self.settings.llm.to_llm_config()
        llm = build_llm(cfg)

        def work() -> str:
            runtime = self.runtime
            if runtime is None:
                runtime = build_runtime(db_path=os.getenv("MPC_DB", "data/mpc.db"))
                self.runtime = runtime
            from .agent.chat import ConsoleChat

            chat = ConsoleChat(runtime.store, runtime.governance, llm)
            result = chat.reply(text)
            reply = result.get("reply") or ""
            if not reply:
                reply = f"（未回复：{result.get('reason', result.get('status'))}）"
            return reply

        self._thread_to(work, "chat_reply")

    def clear_chat(self) -> None:
        self.chat_log.configure(state="normal")
        self.chat_log.delete("1.0", "end")
        self.chat_log.configure(state="disabled")

    # ---------- 页签 5：运行状态 ----------

    def _build_status_tab(self) -> None:
        from tkinter import ttk

        f = self.tab_status
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Button(row, text="连接微信通道", command=self.start_wechat).pack(side="left")
        ttk.Button(row, text="断开微信通道", command=self.stop_wechat).pack(side="left", padx=8)
        ttk.Button(row, text="刷新", command=self.refresh_metrics).pack(side="left")
        ttk.Button(row, text="查看审计流水", command=self.show_audit).pack(side="left", padx=8)

        ttk.Label(f, text="能力矩阵").pack(anchor="w", pady=(10, 0))
        self.caps_text = self.tk.Text(f, height=8, width=90, state="disabled")
        self.caps_text.pack(fill="x")

        ttk.Label(f, text="实时指标").pack(anchor="w", pady=(10, 0))
        self.metrics_text = self.tk.Text(f, height=7, width=90, state="disabled")
        self.metrics_text.pack(fill="x")

        ttk.Label(f, text="审计流水").pack(anchor="w", pady=(10, 0))
        self.audit_text = self.tk.Text(f, height=8, width=90, state="disabled")
        self.audit_text.pack(fill="both", expand=True)

        self.root.after(800, self.refresh_metrics)

    def _set_text(self, widget: Any, content: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", content)
        widget.configure(state="disabled")

    def refresh_metrics(self) -> None:
        def work() -> dict[str, Any]:
            runtime = self.runtime
            if runtime is None:
                runtime = build_runtime(db_path=os.getenv("MPC_DB", "data/mpc.db"))
                self.runtime = runtime
            return {
                "caps": runtime.hub.capabilities(),
                "metrics": runtime.hub.metrics(),
            }

        self._thread_to(work, "metrics")

    def _fill_metrics(self, payload: dict[str, Any]) -> None:
        caps = "\n".join(
            f"· {c.get('platform')}: 读={c.get('can_read')} 写={c.get('can_send')} "
            f"媒体={c.get('can_send_media')} 群聊={c.get('supports_group')} "
            f"上限={c.get('max_text_length')} 字  {c.get('notes', '')}"
            for c in payload.get("caps", [])
        )
        self._set_text(self.caps_text, caps or "（没有连接器）")
        metrics = payload.get("metrics", {})
        self._set_text(
            self.metrics_text,
            json.dumps(metrics, ensure_ascii=False, indent=2),
        )

    def show_audit(self) -> None:
        def work() -> str:
            runtime = self.runtime
            if runtime is None:
                runtime = build_runtime(db_path=os.getenv("MPC_DB", "data/mpc.db"))
                self.runtime = runtime
            rows = runtime.store.audit_recent(limit=30)
            return "\n".join(
                f"{_fmt_time(float(r['created_at']))} [{r['platform']}/{r['conversation_id']}] "
                f"{r['stage']} -> {r['decision']}  {r['reason']}"
                for r in rows
            ) or "（还没有审计记录）"

        self._thread_to(work, "audit")

    # ---------- 微信扫码登录 ----------

    def start_qr_login(self) -> None:
        from .connectors.ilink import ILinkConnector

        self.qr_status_var.set("正在获取二维码…")

        def work() -> dict[str, Any]:
            connector = ILinkConnector()
            return connector.login_qr()

        def run() -> None:
            try:
                payload = work()
            except Exception as exc:
                self._bus.put(("status", f"获取二维码失败：{exc}"))
                return
            self._bus.put(("qr", payload))

        threading.Thread(target=run, daemon=True).start()

    def _open_qr_browser(self) -> None:
        url = self._qr_state.get("qr_url") or self._qr_state.get("qrcode_url") or ""
        if not url:
            self.qr_status_var.set("先点「扫码登录」拿到二维码")
            return
        webbrowser.open(url)

    def _on_qr(self, payload: dict[str, Any]) -> None:
        qr_url = payload.get("qr_url") or payload.get("qrcode_url") or ""
        qrcode_id = payload.get("qrcode_id") or payload.get("qrcode") or ""
        self._qr_state = payload
        self.qr_status_var.set(f"已获取二维码，请用微信扫（id={qrcode_id[:12]}…）")

        if qr_url:
            png = Path.home() / ".mpc-hub" / "wechat-login-qr.png"
            if _render_qr_png(qr_url, png):
                try:
                    from tkinter import PhotoImage

                    self._qr_photo = PhotoImage(file=str(png))
                    self.qr_label.configure(image=self._qr_photo, text="")
                except Exception:
                    self.qr_label.configure(text="二维码已存到 " + str(png))
            else:
                self.qr_label.configure(text="（本机没装二维码渲染库，点「在浏览器打开登录页」）")

        if qrcode_id:
            threading.Thread(
                target=self._poll_login, args=(qrcode_id,), daemon=True
            ).start()

    def _poll_login(self, qrcode_id: str) -> None:
        from .connectors.ilink import ILinkConnector

        connector = ILinkConnector()
        deadline = time.time() + 180
        while time.time() < deadline:
            try:
                info = connector.login_status(qrcode_id)
            except Exception as exc:
                self._bus.put(("status", f"查扫码状态出错：{exc}"))
                time.sleep(3)
                continue
            status = str(info.get("status", ""))
            if status == "confirmed":
                token = str(info.get("bot_token", ""))
                user = str(info.get("ilink_user_id", ""))
                # tkinter 控件只能在主线程改，所以把结果丢回总线
                self._bus.put(
                    (
                        "login_ok",
                        {
                            "token": token,
                            "user": user,
                            "message": f"扫码成功，已保存 bot_token（账号 {user}）。请点「保存配置」持久化。",
                        },
                    )
                )
                return
            if status == "expired":
                self._bus.put(("status", "二维码已过期，请重新点「扫码登录」"))
                return
            time.sleep(3)
        self._bus.put(("status", "扫码超时（3 分钟），请重试"))

    # ---------- 保存 / 读取 ----------

    def _collect_platforms(self) -> None:
        plats = self.settings.platforms
        plats.wechat_ilink.enabled = bool(self.ilink_enabled_var.get())
        plats.wechat_ilink.bot_token = self.ilink_token_var.get().strip()
        plats.wechat_ilink.uin = self.ilink_uin_var.get().strip()
        plats.onebot.enabled = bool(self.onebot_enabled_var.get())
        plats.onebot.base_url = self.onebot_url_var.get().strip()
        plats.onebot.access_token = self.onebot_token_var.get().strip()
        plats.webhook.enabled = bool(self.webhook_enabled_var.get())
        plats.webhook.outbound_url = self.webhook_url_var.get().strip()
        plats.webhook.token = self.webhook_token_var.get().strip()
        module = self.custom_module_var.get().strip()
        if module:
            try:
                options = json.loads(self.custom_options_var.get() or "{}")
            except json.JSONDecodeError:
                options = {}
            plats.custom = [
                {
                    "enabled": True,
                    "platform": self.custom_platform_var.get().strip(),
                    "module": module,
                    "attr": "create",
                    "options": options if isinstance(options, dict) else {},
                }
            ]

    def _collect_governance(self) -> None:
        gov = self.settings.governance
        gov.allowlist = _split_items(self.allowlist_var.get())
        gov.owner_ids = _split_items(self.owner_ids_var.get())
        gov.blocklist = _split_items(self.blocklist_var.get())
        hours = self.hours_var.get().replace("：", "-").replace("~", "-").split("-")
        if len(hours) == 2:
            try:
                gov.active_hours = [max(0, min(24, int(hours[0]))), max(0, min(24, int(hours[1])))]
            except ValueError:
                pass
        for var, attr in (
            (self.read_quota_var, "daily_read_quota"),
            (self.write_quota_var, "daily_write_quota"),
            (self.max_chars_var, "max_reply_chars"),
        ):
            try:
                setattr(gov, attr, int(var.get()))
            except ValueError:
                pass
        gov.require_approval_on_commitment = bool(self.req_commit_var.get())
        gov.require_approval_groups = bool(self.req_group_var.get())
        gov.group_mention_only = bool(self.group_mention_var.get())
        gov.drop_ping = bool(self.drop_ping_var.get())

    def save_settings(self) -> None:
        self._collect_llm()
        self._collect_platforms()
        self._collect_governance()
        try:
            path = self.settings.save()
        except OSError as exc:
            self.status_var.set(f"保存失败：{exc}")
            return
        self.status_var.set(f"已保存到 {path}（Key 只在这一个文件里）")
        self.runtime = None  # 让下次调用按新配置重建

    def reload_settings(self) -> None:
        self.settings = load_settings()
        self.status_var.set("已重新读取配置")

    # ---------- 微信通道启停 ----------

    def start_wechat(self) -> None:
        self._collect_platforms()
        token = self.settings.platforms.wechat_ilink.bot_token
        if not token:
            self.status_var.set("还没有 bot_token，先点「扫码登录」")
            return

        def work() -> str:
            runtime = self.runtime
            if runtime is None:
                runtime = build_runtime(db_path=os.getenv("MPC_DB", "data/mpc.db"))
                self.runtime = runtime
            runtime.registry.start_all()
            return ",".join(runtime.registry.names())

        self._thread_to(work, "wechat_state")

    def stop_wechat(self) -> None:
        if self.runtime is None:
            return

        def work() -> str:
            self.runtime.registry.stop_all()
            return "已停止"

        self._thread_to(work, "wechat_state")


def launch(settings: Settings | None = None) -> int:
    """打开桌面窗口。"""
    import tkinter as tk

    try:
        root = tk.Tk()
    except Exception as exc:
        print(f"[mpc] 无法打开桌面窗口：{exc}")
        print("[mpc] 无图形环境时可用 `mpc chat`（终端对话）或 `mpc serve`（HTTP 后台）。")
        return 1
    DesktopApp(root, settings)
    root.mainloop()
    return 0
