"""运行时装配测试：连接器注册、LLM 选择、配置驱动。"""

from __future__ import annotations

from pathlib import Path

from mpc.agent.llm import MockLLM, OpenAICompatLLM
from mpc.runtime import build_registry, build_runtime, load_settings
from mpc.settings import Settings


def _settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.llm.api_key = ""  # 不填 Key → 离线 Mock
    return s


def test_registry_always_has_loopback(tmp_path: Path) -> None:
    reg = build_registry(_settings(tmp_path))
    assert "loopback" in reg.names()


def test_registry_enables_configured_platforms(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    s.platforms.wechat_ilink.enabled = True
    s.platforms.wechat_ilink.bot_token = "tk"
    s.platforms.onebot.enabled = True
    s.platforms.onebot.base_url = "http://127.0.0.1:3000"
    reg = build_registry(s)
    names = set(reg.names())
    assert "wechat" in names
    assert "qq" in names


def test_registry_respects_disabled_flags(tmp_path: Path) -> None:
    reg = build_registry(_settings(tmp_path))
    assert "wechat" not in reg.names()
    assert "qq" not in reg.names()


def test_build_runtime_without_key_uses_mock(tmp_path: Path) -> None:
    rt = build_runtime(db_path=str(tmp_path / "rt.db"), settings=_settings(tmp_path))
    assert isinstance(rt.llm, MockLLM)
    assert rt.hub is not None
    assert rt.store is not None
    rt.close()


def test_build_runtime_with_key_uses_real_client(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    s.llm.api_key = "sk-test"
    rt = build_runtime(db_path=str(tmp_path / "rt2.db"), settings=s)
    assert isinstance(rt.llm, OpenAICompatLLM)
    rt.close()


def test_load_settings_reads_env_path(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "env.json"
    target.write_text('{"llm": {"provider": "minimax"}}', encoding="utf-8")
    monkeypatch.setenv("MPC_CONFIG", str(target))
    assert load_settings().llm.provider == "minimax"
