"""桌面端（PySide6/Qt6）的离屏冒烟测试。

没装 PySide6 就跳过——`mpc gui` 会回落到 tkinter 简化版，不该因此让 CI 变红。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="未安装 PySide6（桌面端是可选依赖）")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mpc.desktop_qt import (  # noqa: E402
    PLATFORM_CATALOG,
    QT_AVAILABLE,
    MainWindow,
    SettingsDialog,
)


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_qt_available() -> None:
    assert QT_AVAILABLE is True


def test_window_builds_and_lists_platforms(app: QApplication, tmp_path: Path) -> None:
    win = MainWindow(str(tmp_path / "qt.db"))
    try:
        assert win.platform_list.count() == len(PLATFORM_CATALOG)
        # 每个平台都要有卡片，且卡片上写清楚了状态
        for i in range(win.platform_list.count()):
            item = win.platform_list.item(i)
            assert item.data(Qt.UserRole) in {e["key"] for e in PLATFORM_CATALOG}
            assert win.platform_list.itemWidget(item) is not None
    finally:
        win.close()


def test_selecting_platform_does_not_crash(app: QApplication, tmp_path: Path) -> None:
    win = MainWindow(str(tmp_path / "qt2.db"))
    try:
        for i in range(win.platform_list.count()):
            win.platform_list.setCurrentRow(i)
            app.processEvents()
        assert win.chat_title.text() != "未选择平台"
    finally:
        win.close()


def test_model_chip_elides_instead_of_clipping(app: QApplication, tmp_path: Path) -> None:
    """侧栏窄，模型名太长时要省略号 + 悬浮提示，而不是把字裁掉。"""
    win = MainWindow(str(tmp_path / "qt4.db"))
    try:
        win.settings.llm.provider = "minimax"
        win.settings.llm.model = "MiniMax-M3.1-Flash-超长模型名字测试"
        win._set_model_chip("模型：minimax · MiniMax-M3.1-Flash-超长模型名字测试（OpenAICompat）")
        win.show()
        app.processEvents()
        chip = win.model_chip
        assert chip.toolTip() == win._model_chip_full
        # 显示出来的文字必须能放进控件宽度（不裁字）
        fm = chip.fontMetrics()
        assert fm.horizontalAdvance(chip.text()) <= chip.width()
        # 太长时必须出现省略号
        assert chip.text().endswith("…")
        # 也不许把侧栏撑破
        assert chip.width() <= win.width()
    finally:
        win.close()


def test_settings_dialog_keeps_user_model(app: QApplication, tmp_path: Path) -> None:
    """打开设置窗口不应该用预设覆盖用户已经填好的模型。"""
    win = MainWindow(str(tmp_path / "qt3.db"))
    try:
        win.settings.llm.provider = "minimax"
        win.settings.llm.model = "My-Custom-Model"
        win.settings.llm.base_url = "https://example.invalid/v1"
        dlg = SettingsDialog(win, win.settings)
        try:
            assert dlg.model.text() == "My-Custom-Model"
            assert dlg.base_url.text() == "https://example.invalid/v1"
        finally:
            dlg.deleteLater()
    finally:
        win.close()


def test_settings_network_buttons_do_not_block_ui_thread(
    app: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归：设置窗口里三个联网按钮必须在后台线程跑。

    之前它们直接跑在 UI 线程上，点一下就冻结最长 30~120 秒，
    窗口会显示"未响应"。这里用慢网络模拟，断言点击立刻返回。
    """
    import time

    import httpx

    from mpc.agent.llm import OpenAICompatLLM
    from mpc.connectors.ilink import ILinkConnector

    def slow(*_a, **_k):
        time.sleep(2.0)
        raise RuntimeError("模拟慢网络")

    monkeypatch.setattr(httpx, "get", slow)
    monkeypatch.setattr(OpenAICompatLLM, "chat", slow)
    monkeypatch.setattr(ILinkConnector, "login_qr", slow)

    win = MainWindow(str(tmp_path / "qt5.db"))
    dlg = SettingsDialog(win, win.settings)
    try:
        dlg.api_key.setText("sk-test")
        dlg.base_url.setText("https://example.invalid/v1")

        for call in (dlg._do_fetch_models, dlg._do_test_llm, dlg._do_qr_login):
            t0 = time.perf_counter()
            call()
            assert time.perf_counter() - t0 < 1.0, f"{call.__name__} 卡住了 UI 线程"

        # 请求进行中按钮应当被禁用，防止重复点击起多个请求
        assert not dlg.fetch_btn.isEnabled()
        assert not dlg.test_btn.isEnabled()
        assert not dlg.qr_btn.isEnabled()
    finally:
        dlg.deleteLater()
        win.close()
