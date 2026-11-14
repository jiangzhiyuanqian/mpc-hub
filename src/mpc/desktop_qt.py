"""现代桌面端（PySide6 / Qt6）。

这是给主人看的「控制台」，布局照着 IM 客户端来：

    ┌────────────┬──────────────┬──────────────────────────┐
    │ 平台选择层  │   会话列表    │        聊天窗             │
    │ (左)       │   (中)       │        (右)              │
    └────────────┴──────────────┴──────────────────────────┘

- 左：每个社交平台一张卡（图标 / 名称 / 状态灯 / 能力摘要）。
  真实接上的平台能点，没接的明确标「规划中」，不假装能用。
- 中：该平台下的会话，最近消息在上。
- 右：嵌入式聊天窗。能看 agent 自动回的，也能自己接管着发
  （手动接管模式下，agent 只给建议卡片，主人点头才发）。

另外还有一个设置对话框（模型接口 / 平台接入 / 治理规则）和审批窗口，
Key 只写进本机配置文件，不外传。
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .contracts import ConversationKind, Envelope, Platform
from .runtime import build_runtime

try:  # PySide6 是可选依赖：没装就回落到老版 tkinter 窗口
    from PySide6.QtCore import QSize, Qt, QTimer
    from PySide6.QtGui import QColor, QFont, QPixmap
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QDoubleSpinBox,
        QFormLayout,
        QFrame,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSpinBox,
        QSplitter,
        QTabWidget,
        QVBoxLayout,
        QWidget,
    )

    QT_AVAILABLE = True
except Exception:  # pragma: no cover - 仅在没装 PySide6 时触发
    QT_AVAILABLE = False


# ---------------------------------------------------------------- 平台清单

# state: live = 已经有连接器实现；planned = 只有路线，没实现
PLATFORM_CATALOG: list[dict[str, str]] = [
    {
        "key": "wechat",
        "label": "微信 · iLink",
        "icon": "💬",
        "note": "扫码即用 · 仅私聊",
        "state": "live",
    },
    {
        "key": "qq",
        "label": "QQ · OneBot 小号",
        "icon": "🐧",
        "note": "自备 OneBot 实现器",
        "state": "live",
    },
    {
        "key": "qq_desktop",
        "label": "QQ · 桌面客户端",
        "icon": "🪟",
        "note": "UI 自动化 · 未验证",
        "state": "planned",
    },
    {
        "key": "douyin",
        "label": "抖音 · 网页版",
        "icon": "🎵",
        "note": "浏览器自动化 · 规划中",
        "state": "planned",
    },
    {
        "key": "webhook",
        "label": "Webhook · 内部系统",
        "icon": "🔌",
        "note": "已实现",
        "state": "live",
    },
    {
        "key": "loopback",
        "label": "本地回环 · 自测",
        "icon": "🧪",
        "note": "已实现",
        "state": "live",
    },
]

PLANNED_NOTES = {
    "qq_desktop": (
        "「控制本机已登录的 QQ 客户端」这条路线还没做。原因和微信 4.x 一个道理："
        "QQNT 主界面是内嵌渲染，UI 自动化多半只能看到空壳窗口。"
        "目前可靠的是上面那条「OneBot 小号」——用一个小号跑协议，不碰你的主号。\n"
        "要做桌面端控制，我建议单独开一轮可行性实测（只读探测，不写 QQ 数据）。"
    ),
    "douyin": (
        "抖音走浏览器自动化（本机已登录的浏览器），是本机另一个插件在做的那条路。"
        "本项目还没接连接器，接的话直接复用它导出的事件流即可。"
    ),
}


# ---------------------------------------------------------------- 主题

QSS = """
* {
    font-family: "Microsoft YaHei UI", "Segoe UI", "PingFang SC", sans-serif;
    font-size: 13px;
    color: #E6E8EF;
}
QMainWindow, QWidget#Root {
    background: #0E1016;
}
QWidget#Sidebar {
    background: #14171F;
    border-right: 1px solid #232734;
}
QWidget#Topbar {
    background: #14171F;
    border-bottom: 1px solid #232734;
}
QLabel#AppTitle {
    font-size: 16px;
    font-weight: 600;
    color: #F2F4FA;
}
QLabel#AppSub {
    font-size: 11px;
    color: #8A93A6;
}
QLabel#SectionTitle {
    font-size: 11px;
    color: #7C8598;
    letter-spacing: 1px;
}
QLabel#PanelTitle {
    font-size: 15px;
    font-weight: 600;
}
QLabel#Hint {
    font-size: 11px;
    color: #7C8598;
}
QLabel#Chip {
    background: #1D212C;
    border: 1px solid #2B3140;
    border-radius: 9px;
    padding: 2px 10px;
    color: #9AA3B8;
    font-size: 11px;
}
QLabel#ChipOn {
    background: #12341F;
    border: 1px solid #1F6B3A;
    border-radius: 9px;
    padding: 2px 10px;
    color: #6EE7A0;
    font-size: 11px;
}
QLabel#ChipWarn {
    background: #3A2A0C;
    border: 1px solid #7A5614;
    border-radius: 9px;
    padding: 2px 10px;
    color: #F5C26B;
    font-size: 11px;
}
QListWidget {
    background: transparent;
    border: none;
    outline: none;
}
QListWidget::item {
    background: transparent;
    border: none;
    padding: 0px;
    margin: 0px;
}
QFrame#PlatformCard {
    background: #1A1E28;
    border: 1px solid #242938;
    border-radius: 12px;
}
QFrame#PlatformCard:hover {
    background: #20253180;
    border: 1px solid #34405A;
}
QFrame#PlatformCardActive {
    background: #1B2334;
    border: 1px solid #3D6BD6;
    border-radius: 12px;
}
QFrame#ConvCard {
    background: #171B24;
    border: 1px solid #232734;
    border-radius: 10px;
}
QFrame#ConvCard:hover { background: #1D2230; }
QFrame#BubbleIn {
    background: #1D212C;
    border: 1px solid #262B38;
    border-radius: 14px;
}
QFrame#BubbleOut {
    background: #2A4EA8;
    border: 1px solid #3A62C6;
    border-radius: 14px;
}
QFrame#Card {
    background: #191E29;
    border: 1px solid #2A3143;
    border-radius: 12px;
}
QFrame#CardWarn {
    background: #241F14;
    border: 1px solid #6B5320;
    border-radius: 12px;
}
QLabel#Meta { color: #7C8598; font-size: 11px; }
QLabel#SysMsg { color: #6E7789; font-size: 11px; }
QLabel#BubbleText { color: #E9ECF4; }
QLabel#BubbleTextOut { color: #FFFFFF; }
QPushButton {
    background: #232937;
    border: 1px solid #2E3546;
    border-radius: 8px;
    padding: 6px 14px;
    color: #D6DAE6;
}
QPushButton:hover { background: #2B3243; border-color: #3C465C; }
QPushButton:pressed { background: #1E2431; }
QPushButton#Primary {
    background: #3D6BD6;
    border: 1px solid #4E7BE6;
    color: #FFFFFF;
    font-weight: 600;
}
QPushButton#Primary:hover { background: #4A78E3; }
QPushButton#Danger {
    background: #3A1F24;
    border: 1px solid #6B3039;
    color: #F0A0A8;
}
QPushButton#Ghost {
    background: transparent;
    border: 1px solid #2E3546;
    color: #99A2B6;
}
QLineEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: #161A23;
    border: 1px solid #262C39;
    border-radius: 8px;
    padding: 6px 10px;
    selection-background-color: #3D6BD6;
}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus,
QDoubleSpinBox:focus, QComboBox:focus {
    border-color: #3D6BD6;
}
QComboBox::drop-down { border: none; width: 18px; }
QTabWidget::pane {
    border: 1px solid #232734;
    border-radius: 10px;
    background: #161A23;
    top: -1px;
}
QTabBar::tab {
    background: transparent;
    color: #8A93A6;
    padding: 8px 18px;
    border-bottom: 2px solid transparent;
}
QTabBar::tab:selected {
    color: #EAF0FF;
    border-bottom: 2px solid #3D6BD6;
}
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #2A3040; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0px; }
QCheckBox { spacing: 8px; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #39415380;
    border-radius: 4px;
    background: #161A23;
}
QCheckBox::indicator:checked { background: #3D6BD6; border-color: #3D6BD6; }
QStatusBar { background: #14171F; color: #7C8598; }
QStatusBar::item { border: none; }
QMenu { background: #1A1E28; border: 1px solid #2A3140; border-radius: 8px; padding: 6px; }
QMenu::item { padding: 6px 24px; border-radius: 6px; }
QMenu::item:selected { background: #2A3348; }
"""


# ---------------------------------------------------------------- 小工具


def _fmt_ts(ts: float) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except Exception:
        return ""


def _fmt_day(ts: float) -> str:
    try:
        return time.strftime("%m-%d", time.localtime(float(ts)))
    except Exception:
        return ""


def _split_csv(text: str) -> list[str]:
    return [x.strip() for x in (text or "").replace("\n", ",").split(",") if x.strip()]


def _render_qr_png(text: str, path: str) -> str:
    """把文本渲染成二维码 PNG（本地画，不访问微信的落地页）。"""
    import qrcode
    from qrcode.image.pure import PyPNGImage

    img = qrcode.make(text, image_factory=PyPNGImage, box_size=8, border=2)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


if QT_AVAILABLE:

    # ---------------------------------------------------------------- 卡片

    class PlatformCard(QWidget):
        """左侧平台选择层里的一张卡。"""

        def __init__(self, entry: dict[str, str], connected: bool, active: bool) -> None:
            super().__init__()
            self.setObjectName("PlatformCardActive" if active else "PlatformCard")
            self.setFixedHeight(64)

            lay = QHBoxLayout(self)
            lay.setContentsMargins(12, 8, 12, 8)
            lay.setSpacing(10)

            icon = QLabel(entry["icon"])
            icon.setFixedSize(30, 30)
            icon.setAlignment(Qt.AlignCenter)
            icon.setStyleSheet("font-size:17px;")
            lay.addWidget(icon)

            col = QVBoxLayout()
            col.setSpacing(2)
            title = QLabel(entry["label"])
            title.setStyleSheet("font-weight:600; color:#E9ECF4;")
            note = QLabel(entry["note"])
            note.setObjectName("Meta")
            col.addWidget(title)
            col.addWidget(note)
            lay.addLayout(col, 1)

            if entry["state"] == "planned":
                dot = QLabel("规划")
                dot.setObjectName("ChipWarn")
            elif connected:
                dot = QLabel("已连接")
                dot.setObjectName("ChipOn")
            else:
                dot = QLabel("未连接")
                dot.setObjectName("Chip")
            lay.addWidget(dot)

    class ConvCard(QWidget):
        """中间会话列表里的一项。"""

        def __init__(self, title: str, preview: str, when: str, active: bool) -> None:
            super().__init__()
            self.setObjectName("ConvCard")
            self.setFixedHeight(58)
            lay = QVBoxLayout(self)
            lay.setContentsMargins(10, 7, 10, 7)
            lay.setSpacing(3)

            top = QHBoxLayout()
            name = QLabel(title)
            name.setStyleSheet("font-weight:600; color:#E9ECF4;")
            time_lbl = QLabel(when)
            time_lbl.setObjectName("Meta")
            top.addWidget(name, 1)
            top.addWidget(time_lbl)
            lay.addLayout(top)

            prev = QLabel(preview)
            prev.setObjectName("Meta")
            prev.setTextInteractionFlags(Qt.TextSelectableByMouse)
            lay.addWidget(prev)

    # ---------------------------------------------------------------- 聊天窗里的卡片

    class SuggestionCard(QFrame):
        """agent 给的建议（手动接管模式）或待审批草稿。

        主人点「发送」才真的发出去——这就是「控制这个软件聊天」。
        """

        def __init__(
            self,
            parent: QWidget,
            *,
            title: str,
            reason: str,
            on_send: Callable[[str], None],
            on_rewrite: Callable[[], None] | None = None,
            on_discard: Callable[[], None] | None = None,
        ) -> None:
            super().__init__(parent)
            self.setObjectName("CardWarn")
            lay = QVBoxLayout(self)
            lay.setContentsMargins(14, 12, 14, 12)
            lay.setSpacing(8)

            head = QHBoxLayout()
            t = QLabel(title)
            t.setStyleSheet("font-weight:600; color:#F5C26B;")
            head.addWidget(t)
            head.addStretch(1)
            r = QLabel(reason)
            r.setObjectName("Meta")
            r.setWordWrap(True)
            head.addWidget(r, 1)
            lay.addLayout(head)

            self.body = QPlainTextEdit()
            self.body.setReadOnly(True)
            self.body.setMinimumHeight(56)
            self.body.setMaximumHeight(140)
            self.body.setPlainText("")
            lay.addWidget(self.body)

            btns = QHBoxLayout()
            btns.setSpacing(8)
            send = QPushButton("发送")
            send.setObjectName("Primary")
            send.clicked.connect(lambda: on_send(self.body.toPlainText()))
            btns.addWidget(send)
            if on_rewrite is not None:
                again = QPushButton("换一句")
                again.clicked.connect(on_rewrite)
                btns.addWidget(again)
            if on_discard is not None:
                drop = QPushButton("丢弃")
                drop.setObjectName("Danger")
                drop.clicked.connect(on_discard)
                btns.addWidget(drop)
            btns.addStretch(1)
            lay.addLayout(btns)

        def set_text(self, text: str) -> None:
            self.body.setPlainText(text)

    # ---------------------------------------------------------------- 设置对话框

    class SettingsDialog(QDialog):
        """模型接口 / 平台接入 / 治理规则。Key 只写本机配置文件。"""

        def __init__(self, parent: QWidget, settings: Any) -> None:
            super().__init__(parent)
            self.setWindowTitle("设置 · 多平台连接中心")
            self.resize(720, 560)
            self.settings = settings

            root = QVBoxLayout(self)
            root.setContentsMargins(18, 18, 18, 18)
            root.setSpacing(12)

            tabs = QTabWidget()
            root.addWidget(tabs, 1)
            tabs.addTab(self._build_llm_tab(), "模型接口")
            tabs.addTab(self._build_platform_tab(), "平台接入")
            tabs.addTab(self._build_gov_tab(), "治理规则")

            path_lbl = QLabel(f"配置文件：{settings.path()}")
            path_lbl.setObjectName("Hint")
            root.addWidget(path_lbl)

            box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            box.button(QDialogButtonBox.Ok).setText("保存")
            box.button(QDialogButtonBox.Cancel).setText("取消")
            box.accepted.connect(self._on_save)
            box.rejected.connect(self.reject)
            root.addWidget(box)

        # ---- 模型接口

        def _build_llm_tab(self) -> QWidget:
            from .settings import PRESETS

            w = QWidget()
            lay = QVBoxLayout(w)
            lay.setContentsMargins(16, 16, 16, 16)
            lay.setSpacing(10)
            form = QFormLayout()
            form.setSpacing(10)
            lay.addLayout(form)

            self.provider = QComboBox()
            for key, preset in PRESETS.items():
                self.provider.addItem(preset.label, key)
            self.provider.currentIndexChanged.connect(self._apply_preset)
            form.addRow("供应商", self.provider)

            self.base_url = QLineEdit(self.settings.llm.base_url)
            form.addRow("Base URL", self.base_url)
            self.base_url.setToolTip("只填到 /v1 这一层，不要带 /chat/completions")

            key_row = QHBoxLayout()
            self.api_key = QLineEdit(self.settings.llm.api_key)
            self.api_key.setEchoMode(QLineEdit.Password)
            self.api_key.setPlaceholderText("粘贴你自己的 Key")
            key_row.addWidget(self.api_key, 1)
            show = QPushButton("显示")
            show.setObjectName("Ghost")

            def _toggle() -> None:
                self.api_key.setEchoMode(
                    QLineEdit.Normal if self.api_key.echoMode() == QLineEdit.Password else QLineEdit.Password
                )
                show.setText("隐藏" if self.api_key.echoMode() == QLineEdit.Normal else "显示")

            show.clicked.connect(_toggle)
            key_row.addWidget(show)
            clear = QPushButton("清空")
            clear.setObjectName("Ghost")
            clear.clicked.connect(lambda: self.api_key.setText(""))
            key_row.addWidget(clear)
            form.addRow("API Key", key_row)

            self.model = QLineEdit(self.settings.llm.model)
            form.addRow("模型", self.model)
            self.fast_model = QLineEdit(self.settings.llm.fast_model)
            form.addRow("快模型", self.fast_model)

            self.temperature = QDoubleSpinBox()
            self.temperature.setRange(0.0, 2.0)
            self.temperature.setSingleStep(0.1)
            self.temperature.setValue(float(self.settings.llm.temperature))
            form.addRow("temperature", self.temperature)

            self.max_tokens = QSpinBox()
            self.max_tokens.setRange(64, 32768)
            self.max_tokens.setValue(int(self.settings.llm.max_tokens))
            form.addRow("max_tokens", self.max_tokens)

            row = QHBoxLayout()
            fetch = QPushButton("拉取模型列表")
            row.addWidget(fetch)
            test = QPushButton("测试连接")
            test.setObjectName("Primary")
            row.addWidget(test)
            row.addStretch(1)
            lay.addLayout(row)

            self.llm_out = QPlainTextEdit()
            self.llm_out.setReadOnly(True)
            self.llm_out.setMinimumHeight(120)
            lay.addWidget(self.llm_out, 1)

            self.fetch_btn = fetch
            self.test_btn = test
            fetch.clicked.connect(self._do_fetch_models)
            test.clicked.connect(self._do_test_llm)

            cur = self.settings.llm.provider
            idx = self.provider.findData(cur)
            # 先关信号再设当前项：打开窗口时**不要**用预设覆盖用户已经填好的
            # base_url / model（只有用户主动换供应商才套预设）。
            self.provider.blockSignals(True)
            if idx >= 0:
                self.provider.setCurrentIndex(idx)
            else:
                self.provider.setCurrentIndex(0)
            self.provider.blockSignals(False)
            return w

        def _apply_preset(self) -> None:
            from .settings import PRESETS

            key = self.provider.currentData()
            preset = PRESETS.get(key)
            if not preset:
                return
            self.base_url.setText(preset.base_url)
            self.model.setText(preset.model)
            self.fast_model.setText(preset.fast_model)
            self.llm_out.appendPlainText(f"→ 已套用预设：{preset.label}（{preset.base_url}）")

        def _log(self, line: str) -> None:
            self.llm_out.appendPlainText(line)

        def _do_fetch_models(self) -> None:
            import httpx

            base = self.base_url.text().strip().rstrip("/")
            key = self.api_key.text().strip()
            if not base:
                self._log("× 先填 Base URL")
                return
            try:
                headers = {"Authorization": f"Bearer {key}"} if key else {}
                r = httpx.get(f"{base}/models", headers=headers, timeout=20)
                if r.status_code >= 400:
                    self._log(f"× HTTP {r.status_code}: {r.text[:300]}")
                    return
                data = r.json()
                items = data.get("data", []) if isinstance(data, dict) else []
                names = [i.get("id", "") for i in items][:60]
                self._log(f"✓ {len(names)} 个模型：" + ", ".join(names))
            except Exception as exc:
                self._log(f"× 请求失败：{exc}")

        def _do_test_llm(self) -> None:
            from .agent.llm import LLMConfig, OpenAICompatLLM

            cfg = LLMConfig(
                base_url=self.base_url.text().strip(),
                api_key=self.api_key.text().strip(),
                model=self.model.text().strip(),
                timeout=30,
            )
            if not cfg.api_key:
                self._log("× 没填 Key。先填自己的 Key，或用「本地回环」自测。")
                return
            try:
                llm = OpenAICompatLLM(cfg)
                out = llm.chat("你是客服助手", "用一句话介绍你自己。")
                self._log("✓ 模型回话：" + out)
            except Exception as exc:
                self._log(f"× 调用失败：{exc}")

        # ---- 平台接入

        def _build_platform_tab(self) -> QWidget:
            w = QWidget()
            lay = QVBoxLayout(w)
            lay.setContentsMargins(16, 16, 16, 16)
            lay.setSpacing(12)

            # 微信 iLink
            grp = QGroupBox("微信 · iLink（扫码登录）")
            glay = QGridLayout(grp)
            glay.setSpacing(8)
            self.wx_enabled = QCheckBox("启用")
            self.wx_enabled.setChecked(self.settings.platforms.wechat_ilink.enabled)
            glay.addWidget(self.wx_enabled, 0, 0)
            self.wx_token = QLineEdit(self.settings.platforms.wechat_ilink.bot_token)
            self.wx_token.setEchoMode(QLineEdit.Password)
            self.wx_token.setPlaceholderText("扫码后自动填，也可以手贴")
            glay.addWidget(self.wx_token, 0, 1, 1, 3)
            glay.addWidget(QLabel("UIN"), 1, 0)
            self.wx_uin = QLineEdit(self.settings.platforms.wechat_ilink.uin)
            glay.addWidget(self.wx_uin, 1, 1)
            qr_btn = QPushButton("扫码登录")
            glay.addWidget(qr_btn, 1, 2)
            self.qr_label = QLabel("")
            self.qr_label.setAlignment(Qt.AlignCenter)
            glay.addWidget(self.qr_label, 2, 0, 1, 4)
            self.wx_status = QLabel("")
            self.wx_status.setObjectName("Hint")
            glay.addWidget(self.wx_status, 3, 0, 1, 4)
            lay.addWidget(grp)
            qr_btn.clicked.connect(self._do_qr_login)

            # QQ OneBot
            grp2 = QGroupBox("QQ · OneBot（推荐：用一个专用小号）")
            g2 = QGridLayout(grp2)
            g2.setSpacing(8)
            self.qq_enabled = QCheckBox("启用")
            self.qq_enabled.setChecked(self.settings.platforms.onebot.enabled)
            g2.addWidget(self.qq_enabled, 0, 0)
            self.qq_url = QLineEdit(self.settings.platforms.onebot.base_url)
            self.qq_url.setPlaceholderText("http://127.0.0.1:5700")
            g2.addWidget(self.qq_url, 0, 1, 1, 2)
            g2.addWidget(QLabel("Access Token"), 1, 0)
            self.qq_token = QLineEdit(self.settings.platforms.onebot.access_token)
            self.qq_token.setEchoMode(QLineEdit.Password)
            g2.addWidget(self.qq_token, 1, 1, 1, 2)
            lay.addWidget(grp2)

            # Webhook
            grp3 = QGroupBox("Webhook · 内部系统")
            g3 = QGridLayout(grp3)
            g3.setSpacing(8)
            self.hook_enabled = QCheckBox("启用")
            self.hook_enabled.setChecked(self.settings.platforms.webhook.enabled)
            g3.addWidget(self.hook_enabled, 0, 0)
            self.hook_url = QLineEdit(self.settings.platforms.webhook.outbound_url)
            g3.addWidget(self.hook_url, 0, 1, 1, 2)
            g3.addWidget(QLabel("Token"), 1, 0)
            self.hook_token = QLineEdit(self.settings.platforms.webhook.token)
            self.hook_token.setEchoMode(QLineEdit.Password)
            g3.addWidget(self.hook_token, 1, 1, 1, 2)
            lay.addWidget(grp3)

            lay.addStretch(1)
            return w

        def _do_qr_login(self) -> None:
            from .connectors.ilink import ILinkConnector

            self.qr_status = "正在取二维码…"
            self.wx_status.setText(self.qr_status)
            try:
                conn = ILinkConnector(bot_token="", uin=self.wx_uin.text().strip())
                qr = conn.login_qr()
            except Exception as exc:
                self.wx_status.setText(f"× 取码失败：{exc}")
                return
            path = Path.home() / ".mpc-hub" / "wechat-login-qr.png"
            try:
                _render_qr_png(qr["qr_url"], str(path))
                pix = QPixmap(str(path))
                self.qr_label.setPixmap(
                    pix.scaled(
                        QSize(190, 190),
                        Qt.KeepAspectRatio,
                        Qt.SmoothTransformation,
                    )
                )
            except Exception as exc:
                self.qr_label.setText(f"二维码渲染失败：{exc}")

            self.wx_status.setText("请用微信扫上面的码…")
            deadline = time.time() + 120
            while time.time() < deadline:
                try:
                    st = conn.login_status(qr["qrcode_id"])
                except Exception as exc:
                    self.wx_status.setText(f"× 查询状态失败：{exc}")
                    return
                status = (st.get("status") or "").lower()
                if status == "confirmed":
                    self.wx_token.setText(st.get("bot_token") or "")
                    self.wx_enabled.setChecked(True)
                    self.wx_status.setText(
                        f"✓ 已登录（账号 {st.get('ilink_user_id', '?')}），Token 已填好，保存即可"
                    )
                    return
                if status == "expired":
                    self.wx_status.setText("× 二维码过期，请再点一次扫码登录")
                    return
                time.sleep(2)

        # ---- 治理规则

        def _build_gov_tab(self) -> QWidget:
            g = self.settings.governance
            w = QWidget()
            lay = QVBoxLayout(w)
            lay.setContentsMargins(16, 16, 16, 16)
            lay.setSpacing(10)
            form = QFormLayout()
            form.setSpacing(10)
            lay.addLayout(form)

            self.g_allow = QLineEdit(", ".join(g.allowlist))
            form.addRow("入站白名单", self.g_allow)
            self.g_owner = QLineEdit(", ".join(g.owner_ids))
            form.addRow("主人 ID", self.g_owner)
            self.g_block = QLineEdit(", ".join(g.blocklist))
            form.addRow("黑名单", self.g_block)

            hours = QHBoxLayout()
            self.g_h1 = QSpinBox()
            self.g_h1.setRange(0, 24)
            self.g_h1.setValue(int(g.active_hours[0]))
            self.g_h2 = QSpinBox()
            self.g_h2.setRange(0, 24)
            self.g_h2.setValue(int(g.active_hours[1]))
            hours.addWidget(self.g_h1)
            hours.addWidget(QLabel("到"))
            hours.addWidget(self.g_h2)
            hours.addStretch(1)
            form.addRow("允许时段（小时）", hours)

            self.g_read = QSpinBox()
            self.g_read.setRange(1, 100000)
            self.g_read.setValue(int(g.daily_read_quota))
            form.addRow("每日读配额", self.g_read)
            self.g_write = QSpinBox()
            self.g_write.setRange(1, 100000)
            self.g_write.setValue(int(g.daily_write_quota))
            form.addRow("每日写配额", self.g_write)
            self.g_chars = QSpinBox()
            self.g_chars.setRange(50, 5000)
            self.g_chars.setValue(int(g.max_reply_chars))
            form.addRow("单条回复字数上限", self.g_chars)

            self.g_commit = QCheckBox("承诺性话术转人工")
            self.g_commit.setChecked(bool(g.require_approval_on_commitment))
            form.addRow("闸门", self.g_commit)
            self.g_groups = QCheckBox("群聊回复转人工")
            self.g_groups.setChecked(bool(g.require_approval_groups))
            form.addRow("", self.g_groups)
            self.g_mention = QCheckBox("群聊只在被 @ 时回复")
            self.g_mention.setChecked(bool(g.group_mention_only))
            form.addRow("", self.g_mention)
            self.g_ping = QCheckBox("丢弃纯招呼语（你好/在吗）")
            self.g_ping.setChecked(bool(g.drop_ping))
            form.addRow("", self.g_ping)

            lay.addStretch(1)
            return w

        # ---- 保存

        def _on_save(self) -> None:
            s = self.settings
            s.llm.provider = self.provider.currentData() or s.llm.provider
            s.llm.base_url = self.base_url.text().strip()
            s.llm.api_key = self.api_key.text().strip()
            s.llm.model = self.model.text().strip()
            s.llm.fast_model = self.fast_model.text().strip()
            s.llm.temperature = float(self.temperature.value())
            s.llm.max_tokens = int(self.max_tokens.value())

            s.platforms.wechat_ilink.enabled = self.wx_enabled.isChecked()
            s.platforms.wechat_ilink.bot_token = self.wx_token.text().strip()
            s.platforms.wechat_ilink.uin = self.wx_uin.text().strip()
            s.platforms.onebot.enabled = self.qq_enabled.isChecked()
            s.platforms.onebot.base_url = self.qq_url.text().strip()
            s.platforms.onebot.access_token = self.qq_token.text().strip()
            s.platforms.webhook.enabled = self.hook_enabled.isChecked()
            s.platforms.webhook.outbound_url = self.hook_url.text().strip()
            s.platforms.webhook.token = self.hook_token.text().strip()

            g = s.governance
            g.allowlist = _split_csv(self.g_allow.text())
            g.owner_ids = _split_csv(self.g_owner.text())
            g.blocklist = _split_csv(self.g_block.text())
            g.active_hours = [int(self.g_h1.value()), int(self.g_h2.value())]
            g.daily_read_quota = int(self.g_read.value())
            g.daily_write_quota = int(self.g_write.value())
            g.max_reply_chars = int(self.g_chars.value())
            g.require_approval_on_commitment = self.g_commit.isChecked()
            g.require_approval_groups = self.g_groups.isChecked()
            g.group_mention_only = self.g_mention.isChecked()
            g.drop_ping = self.g_ping.isChecked()

            try:
                s.save()
            except Exception as exc:
                QMessageBox.warning(self, "保存失败", str(exc))
                return
            self.accept()

    # ---------------------------------------------------------------- 主窗口

    class MainWindow(QMainWindow):
        def __init__(self, db_path: str | None = None) -> None:
            super().__init__()
            self.setWindowTitle("多平台连接中心 · 客服后台")
            self.resize(1280, 820)
            self.setStyleSheet(QSS)

            self.runtime = build_runtime(db_path)
            self.hub = self.runtime.hub
            self.store = self.runtime.store
            self.settings = self.runtime.settings
            self._db_path = db_path

            self._queue: "queue.Queue[tuple[Callable[[Any], None], Any]]" = queue.Queue()
            self._current_platform: str | None = None
            self._current_conversation: str | None = None
            self._mode = "auto"  # auto | manual

            self._build_ui()
            self._reload_platforms()
            self._poll_timer = QTimer(self)
            self._poll_timer.timeout.connect(self._drain)
            self._poll_timer.start(200)

        # ---------- 布局

        def _build_ui(self) -> None:
            root = QWidget()
            root.setObjectName("Root")
            self.setCentralWidget(root)
            outer = QHBoxLayout(root)
            outer.setContentsMargins(0, 0, 0, 0)
            outer.setSpacing(0)

            outer.addWidget(self._build_sidebar())

            split = QSplitter(Qt.Horizontal)
            split.setHandleWidth(1)
            split.setStyleSheet("QSplitter::handle { background:#232734; }")
            split.addWidget(self._build_conv_panel())
            split.addWidget(self._build_chat_panel())
            split.setStretchFactor(0, 0)
            split.setStretchFactor(1, 1)
            split.setSizes([300, 980])
            outer.addWidget(split, 1)

            self.statusBar().showMessage("就绪")

        def _build_sidebar(self) -> QWidget:
            side = QWidget()
            side.setObjectName("Sidebar")
            side.setFixedWidth(300)
            lay = QVBoxLayout(side)
            lay.setContentsMargins(16, 18, 16, 14)
            lay.setSpacing(12)

            title = QLabel("多平台连接中心")
            title.setObjectName("AppTitle")
            lay.addWidget(title)
            sub = QLabel("客服后台 · 独立运行")
            sub.setObjectName("AppSub")
            lay.addWidget(sub)

            self.model_chip = QLabel("模型：—")
            self.model_chip.setObjectName("Chip")
            self.model_chip.setToolTip("")
            # 让它可以比文字窄（否则长模型名会把侧栏撑破、文字被裁掉）
            self.model_chip.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self._model_chip_full = "模型：—"
            lay.addWidget(self.model_chip)

            sec = QLabel("平台")
            sec.setObjectName("SectionTitle")
            lay.addWidget(sec)

            self.platform_list = QListWidget()
            self.platform_list.setSpacing(4)
            self.platform_list.currentRowChanged.connect(self._on_platform_selected)
            lay.addWidget(self.platform_list, 1)

            self.approval_btn = QPushButton("审批队列（0）")
            self.approval_btn.clicked.connect(self._open_approvals)
            lay.addWidget(self.approval_btn)

            row = QHBoxLayout()
            set_btn = QPushButton("设置")
            set_btn.clicked.connect(self._open_settings)
            row.addWidget(set_btn)
            metric_btn = QPushButton("指标")
            metric_btn.setObjectName("Ghost")
            metric_btn.clicked.connect(self._open_metrics)
            row.addWidget(metric_btn)
            lay.addLayout(row)

            foot = QLabel(f"数据：{self.runtime.store.path if hasattr(self.runtime.store, 'path') else 'data/mpc.db'}")
            foot.setObjectName("Hint")
            foot.setWordWrap(True)
            lay.addWidget(foot)
            return side

        def _build_conv_panel(self) -> QWidget:
            w = QWidget()
            w.setStyleSheet("background:#0E1016;")
            lay = QVBoxLayout(w)
            lay.setContentsMargins(12, 16, 12, 12)
            lay.setSpacing(10)

            head = QLabel("会话")
            head.setObjectName("SectionTitle")
            lay.addWidget(head)

            self.conv_hint = QLabel("先在左边选一个平台")
            self.conv_hint.setObjectName("Hint")
            self.conv_hint.setWordWrap(True)
            lay.addWidget(self.conv_hint)

            self.conv_list = QListWidget()
            self.conv_list.setSpacing(4)
            self.conv_list.currentRowChanged.connect(self._on_conversation_selected)
            lay.addWidget(self.conv_list, 1)
            return w

        def _build_chat_panel(self) -> QWidget:
            w = QWidget()
            w.setStyleSheet("background:#0E1016;")
            lay = QVBoxLayout(w)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(0)

            top = QWidget()
            top.setObjectName("Topbar")
            top.setFixedHeight(64)
            tl = QHBoxLayout(top)
            tl.setContentsMargins(18, 10, 18, 10)
            tl.setSpacing(12)

            self.chat_title = QLabel("未选择平台")
            self.chat_title.setObjectName("PanelTitle")
            tl.addWidget(self.chat_title)
            self.chat_chip = QLabel("—")
            self.chat_chip.setObjectName("Chip")
            tl.addWidget(self.chat_chip)
            tl.addStretch(1)

            tl.addWidget(QLabel("模式"))
            self.mode_box = QComboBox()
            self.mode_box.addItem("自动回复", "auto")
            self.mode_box.addItem("手动接管", "manual")
            self.mode_box.setToolTip("自动回复：agent 直接回。手动接管：agent 只给建议，主人点头才发。")
            self.mode_box.currentIndexChanged.connect(self._on_mode_changed)
            tl.addWidget(self.mode_box)

            refresh = QPushButton("刷新")
            refresh.setObjectName("Ghost")
            refresh.clicked.connect(self._reload_conversations)
            tl.addWidget(refresh)
            lay.addWidget(top)

            self.scroll = QScrollArea()
            self.scroll.setWidgetResizable(True)
            self.scroll.setStyleSheet("background:#0E1016;")
            self.msg_host = QWidget()
            self.msg_host.setStyleSheet("background:#0E1016;")
            self.msg_lay = QVBoxLayout(self.msg_host)
            self.msg_lay.setContentsMargins(18, 16, 18, 16)
            self.msg_lay.setSpacing(10)
            self.msg_lay.addStretch(1)
            self.scroll.setWidget(self.msg_host)
            lay.addWidget(self.scroll, 1)

            # 底部输入区
            bottom = QWidget()
            bottom.setStyleSheet("background:#0E1016;")
            bl = QVBoxLayout(bottom)
            bl.setContentsMargins(18, 8, 18, 14)
            bl.setSpacing(8)

            self.input = QPlainTextEdit()
            self.input.setPlaceholderText("在这里输入，回车发送（Ctrl+Enter 换行）")
            self.input.setMaximumHeight(110)
            bl.addWidget(self.input)

            brow = QHBoxLayout()
            self.mode_hint = QLabel("自动回复模式：你发的话会作为「客服发言」投递到平台。")
            self.mode_hint.setObjectName("Hint")
            brow.addWidget(self.mode_hint, 1)
            self.draft_btn = QPushButton("✍ 拟一条")
            self.draft_btn.setToolTip("让 AI 针对最近一条客户消息起草回复，你确认后才发")
            self.draft_btn.clicked.connect(self._on_draft)
            brow.addWidget(self.draft_btn)
            send = QPushButton("发送")
            send.setObjectName("Primary")
            send.clicked.connect(self._on_send)
            brow.addWidget(send)
            bl.addLayout(brow)
            lay.addWidget(bottom)
            return w

        # ---------- 数据刷新

        def _reload_platforms(self) -> None:
            self.platform_list.blockSignals(True)
            self.platform_list.clear()
            registered = set(self.runtime.registry.names())
            for entry in PLATFORM_CATALOG:
                connected = entry["key"] in registered
                card = PlatformCard(entry, connected, self._current_platform == entry["key"])
                item = QListWidgetItem()
                item.setSizeHint(QSize(240, 68))
                item.setData(Qt.UserRole, entry["key"])
                self.platform_list.addItem(item)
                self.platform_list.setItemWidget(item, card)
            self.platform_list.blockSignals(False)

            # 重建后把选中项摆回去（重建过程中信号是关的，不会递归）
            if self._current_platform:
                self.platform_list.blockSignals(True)
                for i in range(self.platform_list.count()):
                    if self.platform_list.item(i).data(Qt.UserRole) == self._current_platform:
                        self.platform_list.setCurrentRow(i)
                        break
                self.platform_list.blockSignals(False)

            llm = self.runtime.llm
            name = type(llm).__name__.replace("LLM", "")
            self._set_model_chip(f"模型：{self.settings.llm.provider} · {self.settings.llm.model}（{name}）")
            self._refresh_approval_badge()

        # ---------- 模型胶囊：宽度不够就省略号，别把字裁掉

        def _set_model_chip(self, text: str) -> None:
            self._model_chip_full = text
            self.model_chip.setToolTip(text)
            self._elide_model_chip()

        def _elide_model_chip(self) -> None:
            full = self._model_chip_full
            width = self.model_chip.width()
            if width <= 40:
                width = 268  # 侧栏 300 - 边距
            fm = self.model_chip.fontMetrics()
            self.model_chip.setText(fm.elidedText(full, Qt.ElideRight, max(40, width - 26)))

        def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
            super().resizeEvent(event)
            self._elide_model_chip()

        def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt 命名
            super().showEvent(event)
            # 布局这会儿才最终算完宽度，重新省略一次
            self._elide_model_chip()

        def _on_platform_selected(self, row: int) -> None:
            if row < 0:
                return
            item = self.platform_list.item(row)
            key = item.data(Qt.UserRole)
            self._current_platform = key
            entry = next((e for e in PLATFORM_CATALOG if e["key"] == key), None)
            if entry is None:
                return
            self.chat_title.setText(entry["label"])
            if entry["state"] == "planned":
                self.chat_chip.setText("规划中")
                self.chat_chip.setObjectName("ChipWarn")
                self.chat_chip.style().unpolish(self.chat_chip)
                self.chat_chip.style().polish(self.chat_chip)
                self._show_notice(entry["label"], PLANNED_NOTES.get(key, "这条路线还没实现。"))
                self.conv_list.clear()
                return
            connector = self.runtime.registry.for_platform(key)
            if connector is None:
                self.chat_chip.setText("未连接")
                self.chat_chip.setObjectName("Chip")
                self.chat_chip.style().unpolish(self.chat_chip)
                self.chat_chip.style().polish(self.chat_chip)
                self._show_notice(
                    entry["label"],
                    "这个平台还没连上。点左下角「设置」→「平台接入」填参数，保存后自动接上。",
                )
                self.conv_list.clear()
                return
            self.chat_chip.setText("已连接")
            self.chat_chip.setObjectName("ChipOn")
            self.chat_chip.style().unpolish(self.chat_chip)
            self.chat_chip.style().polish(self.chat_chip)
            self._reload_conversations()

        def _reload_conversations(self) -> None:
            key = self._current_platform
            self.conv_list.blockSignals(True)
            self.conv_list.clear()
            if not key:
                self.conv_list.blockSignals(False)
                return
            rows = self.store.list_conversations(platform=key, limit=100)
            if not rows:
                self.conv_hint.setText("还没有会话。等平台来消息，或在右边手动发一条。")
                self.conv_list.blockSignals(False)
                return
            self.conv_hint.setText(f"{len(rows)} 个会话")
            for r in rows:
                title = r.get("title") or r["conversation_id"]
                preview = (r.get("last_text") or "").replace("\n", " ")
                if len(preview) > 34:
                    preview = preview[:34] + "…"
                when = _fmt_ts(r.get("last_at") or 0)
                card = ConvCard(title, preview, when, self._current_conversation == r["conversation_id"])
                item = QListWidgetItem()
                item.setSizeHint(QSize(260, 62))
                item.setData(Qt.UserRole, r["conversation_id"])
                self.conv_list.addItem(item)
                self.conv_list.setItemWidget(item, card)
            self.conv_list.blockSignals(False)
            if rows:
                self.conv_list.setCurrentRow(0)

        def _on_conversation_selected(self, row: int) -> None:
            if row < 0:
                return
            item = self.conv_list.item(row)
            self._current_conversation = item.data(Qt.UserRole)
            self._render_thread()

        # ---------- 聊天窗渲染

        def _clear_msgs(self) -> None:
            while self.msg_lay.count() > 1:
                item = self.msg_lay.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.deleteLater()

        def _show_notice(self, title: str, text: str) -> None:
            self._clear_msgs()
            card = QFrame()
            card.setObjectName("Card")
            lay = QVBoxLayout(card)
            lay.setContentsMargins(18, 16, 18, 16)
            t = QLabel(title)
            t.setStyleSheet("font-size:15px; font-weight:600;")
            lay.addWidget(t)
            body = QLabel(text)
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextSelectableByMouse)
            body.setStyleSheet("color:#AEB6C8;")
            lay.addWidget(body)
            self.msg_lay.insertWidget(self.msg_lay.count() - 1, card)

        def _add_bubble(self, direction: str, name: str, text: str, ts: float) -> None:
            row = QHBoxLayout()
            row.setSpacing(8)
            if direction == "outbound":
                row.addStretch(1)
            bubble = QFrame()
            bubble.setObjectName("BubbleOut" if direction == "outbound" else "BubbleIn")
            bubble.setMaximumWidth(620)
            bl = QVBoxLayout(bubble)
            bl.setContentsMargins(14, 9, 14, 9)
            bl.setSpacing(4)

            head = QHBoxLayout()
            head.setSpacing(6)
            who = QLabel(name)
            who.setStyleSheet("font-size:11px; color:#9AA3B8;")
            when = QLabel(_fmt_day(ts) + " " + _fmt_ts(ts))
            when.setStyleSheet("font-size:11px; color:#6E7789;")
            head.addWidget(who)
            head.addStretch(1)
            head.addWidget(when)
            bl.addLayout(head)

            body = QLabel(text)
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextSelectableByMouse)
            body.setObjectName("BubbleTextOut" if direction == "outbound" else "BubbleText")
            bl.addWidget(body)

            row.addWidget(bubble)
            if direction != "outbound":
                row.addStretch(1)
            holder = QWidget()
            holder.setLayout(row)
            self.msg_lay.insertWidget(self.msg_lay.count() - 1, holder)

        def _add_sys(self, text: str) -> None:
            lbl = QLabel(text)
            lbl.setObjectName("SysMsg")
            lbl.setAlignment(Qt.AlignCenter)
            holder = QWidget()
            lay = QHBoxLayout(holder)
            lay.setContentsMargins(0, 4, 0, 4)
            lay.addStretch(1)
            lay.addWidget(lbl)
            lay.addStretch(1)
            self.msg_lay.insertWidget(self.msg_lay.count() - 1, holder)

        def _render_thread(self) -> None:
            self._clear_msgs()
            key = self._current_platform
            conv = self._current_conversation
            if not key or not conv:
                self._add_sys("选一个会话看看")
                return
            rows = self.store.conversation_history(key, conv, limit=100)
            if not rows:
                self._add_sys("这个会话还没有消息")
                return
            for r in rows:
                direction = r.get("direction") or "inbound"
                name = "客服" if direction == "outbound" else (r.get("sender_name") or r.get("sender_id") or "客户")
                self._add_bubble(direction, name, r.get("text") or "", r.get("created_at") or 0)

        # ---------- 动作

        def _on_mode_changed(self) -> None:
            self._mode = self.mode_box.currentData() or "auto"
            if self._mode == "manual":
                self.mode_hint.setText("手动接管：agent 只给建议，你点「发送」才真的发出去。")
                self.draft_btn.setVisible(True)
            else:
                self.mode_hint.setText("自动回复：入站消息由 agent 直接处理。")
                self.draft_btn.setVisible(True)

        def _on_send(self) -> None:
            text = self.input.toPlainText().strip()
            if not text:
                return
            key = self._current_platform
            conv = self._current_conversation or "console"
            if not key:
                return
            self.input.clear()
            self._async(
                lambda: self.hub.send_manual(key, conv, text),
                lambda res: self._after_manual(res, text),
            )

        def _after_manual(self, res: dict[str, Any], text: str) -> None:
            if res.get("ok"):
                self._add_bubble("outbound", "我", text, time.time())
                self.statusBar().showMessage("已发送", 4000)
            else:
                self._add_sys(f"× 发送被拦下：{res.get('reason') or res.get('status')}")
            self._reload_conversations()

        def _on_draft(self) -> None:
            """让 AI 针对最近一条客户消息起草回复（不直接发）。"""
            key = self._current_platform
            conv = self._current_conversation
            if not key or not conv:
                self._add_sys("先选一个会话")
                return
            history = self.store.conversation_history(key, conv, limit=8)
            last_in = next((h for h in reversed(history) if h.get("direction") == "inbound"), None)
            if not last_in:
                self._add_sys("这个会话还没有客户消息")
                return
            self._async(
                lambda: self._make_draft(history, last_in),
                lambda res: self._show_suggestion(res),
            )

        def _make_draft(self, history: list[dict[str, Any]], last_in: dict[str, Any]) -> dict[str, Any]:
            from .agent.prompts import SYSTEM_DRAFT, build_chat_prompt

            prompt = build_chat_prompt(last_in.get("text") or "", history)
            try:
                text = self.runtime.llm.chat(SYSTEM_DRAFT, prompt)
            except Exception as exc:
                return {"ok": False, "reason": f"模型调用失败：{exc}", "text": ""}
            env = Envelope(
                message_id="draft",
                platform=Platform(self._current_platform) if self._current_platform in {p.value for p in Platform} else Platform.CONSOLE,
                conversation_id=self._current_conversation or "console",
                conversation_kind=ConversationKind.PRIVATE,
                sender_id="owner",
                text=text,
            )
            verdict = self.runtime.governance.check_outbound(env, text)
            return {"ok": True, "text": text, "verdict": verdict.decision.value, "reason": verdict.reason}

        def _show_suggestion(self, res: dict[str, Any]) -> None:
            if not res.get("ok"):
                self._add_sys(f"× {res.get('reason')}")
                return
            text = res.get("text") or ""
            verdict = res.get("verdict")
            title = "AI 建议的回复"
            reason = res.get("reason") or ""
            if verdict == "approve":
                title = "AI 建议（按规则需要人工确认）"
            card = SuggestionCard(
                self.msg_host,
                title=title,
                reason=reason,
                on_send=self._send_suggestion,
                on_rewrite=self._on_draft,
                on_discard=self._discard_suggestion,
            )
            card.set_text(text)
            self.msg_lay.insertWidget(self.msg_lay.count() - 1, card)

        def _send_suggestion(self, text: str) -> None:
            key = self._current_platform
            conv = self._current_conversation or "console"
            if not key:
                return
            self._async(
                lambda: self.hub.send_manual(key, conv, text),
                lambda res: self._after_manual(res, text),
            )

        def _discard_suggestion(self) -> None:
            self._add_sys("已丢弃这条建议")
            self._render_thread()

        # ---------- 审批 / 设置 / 指标

        def _refresh_approval_badge(self) -> None:
            n = len(self.hub.pending_approvals())
            self.approval_btn.setText(f"审批队列（{n}）")

        def _open_approvals(self) -> None:
            items = self.hub.pending_approvals()
            dlg = QDialog(self)
            dlg.setWindowTitle("审批队列")
            dlg.resize(640, 480)
            lay = QVBoxLayout(dlg)
            lay.setContentsMargins(16, 16, 16, 16)
            lay.setSpacing(10)

            if not items:
                lay.addWidget(QLabel("没有待审批的消息。"))
            for it in items:
                card = QFrame()
                card.setObjectName("Card")
                cl = QVBoxLayout(card)
                cl.setContentsMargins(14, 10, 14, 10)
                head = QLabel(f"{it['platform']} · {it['conversation_id']} · {it['sender_id']}")
                head.setStyleSheet("font-weight:600;")
                cl.addWidget(head)
                reason = QLabel(it.get("reason") or "")
                reason.setObjectName("Meta")
                reason.setWordWrap(True)
                cl.addWidget(reason)
                draft = QLabel(it.get("draft_text") or "")
                draft.setWordWrap(True)
                cl.addWidget(draft)
                row = QHBoxLayout()
                ok_btn = QPushButton("放行发送")
                ok_btn.setObjectName("Primary")
                no_btn = QPushButton("拒绝")
                no_btn.setObjectName("Danger")
                row.addWidget(ok_btn)
                row.addWidget(no_btn)
                row.addStretch(1)
                cl.addLayout(row)
                lay.addWidget(card)

                def _approve(aid: str = it["approval_id"]) -> None:
                    r = self.hub.approve(aid, True)
                    self.statusBar().showMessage(
                        "已放行" if r.get("ok") else f"被拦下：{r.get('reason')}", 5000
                    )
                    self._refresh_approval_badge()
                    dlg.close()

                def _reject(aid: str = it["approval_id"]) -> None:
                    self.hub.approve(aid, False)
                    self._refresh_approval_badge()
                    dlg.close()

                ok_btn.clicked.connect(_approve)
                no_btn.clicked.connect(_reject)

            lay.addStretch(1)
            dlg.exec()
            self._refresh_approval_badge()
            self._render_thread()

        def _open_settings(self) -> None:
            dlg = SettingsDialog(self, self.settings)
            if dlg.exec() == QDialog.Accepted:
                try:
                    self.runtime.close()
                except Exception:
                    pass
                self.runtime = build_runtime(self._db_path, settings=self.settings)
                self.hub = self.runtime.hub
                self.store = self.runtime.store
                self._reload_platforms()
                self._reload_conversations()
                self.statusBar().showMessage("设置已保存并生效", 4000)

        def _open_metrics(self) -> None:
            data = self.hub.metrics()
            dlg = QDialog(self)
            dlg.setWindowTitle("运行指标")
            dlg.resize(560, 420)
            lay = QVBoxLayout(dlg)
            view = QPlainTextEdit()
            view.setReadOnly(True)
            view.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))
            lay.addWidget(view)
            dlg.exec()

        # ---------- 线程

        def _async(self, fn: Callable[[], Any], cb: Callable[[Any], None]) -> None:
            def worker() -> None:
                try:
                    res = fn()
                except Exception as exc:  # 线程里的异常不能炸 UI
                    res = {"ok": False, "reason": str(exc)}
                self._queue.put((cb, res))

            threading.Thread(target=worker, daemon=True).start()

        def _drain(self) -> None:
            while True:
                try:
                    cb, res = self._queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    cb(res)
                except Exception as exc:
                    self.statusBar().showMessage(f"界面更新出错：{exc}", 5000)

        def closeEvent(self, event: Any) -> None:  # noqa: N802
            try:
                self.runtime.close()
            except Exception:
                pass
            super().closeEvent(event)


def launch(db_path: str | None = None) -> int:
    """启动现代桌面端。"""
    if not QT_AVAILABLE:
        raise RuntimeError("未安装 PySide6，无法启动 Qt 桌面端")
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("多平台连接中心")
    win = MainWindow(db_path)
    win.show()
    return app.exec()
