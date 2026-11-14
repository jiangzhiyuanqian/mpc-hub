"""配置层测试：读写往返、掩码、权限、预置供应商。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mpc.settings import PRESETS, Settings


def test_presets_cover_expected_providers() -> None:
    for key in ("deepseek", "minimax", "openai", "ollama", "custom"):
        assert key in PRESETS
    # 接口优先：本地模型只是备选，且明确标注 <= 7B
    assert PRESETS["ollama"].key_required is False
    assert "7B" in PRESETS["ollama"].notes
    assert PRESETS["deepseek"].key_required is True


def test_save_load_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "cfg" / "config.json"
    s = Settings()
    s.llm.provider = "minimax"
    s.llm.base_url = PRESETS["minimax"].base_url
    s.llm.model = PRESETS["minimax"].model
    s.llm.api_key = "sk-secret-value-123456"
    s.governance.allowlist = ["u1", "u2"]
    s.platforms.wechat_ilink.enabled = True
    s.platforms.wechat_ilink.bot_token = "tk-abc"
    s.save(target)

    assert target.exists()
    loaded = Settings.load(target)
    assert loaded.llm.provider == "minimax"
    assert loaded.llm.api_key == "sk-secret-value-123456"
    assert loaded.governance.allowlist == ["u1", "u2"]
    assert loaded.platforms.wechat_ilink.enabled is True
    assert loaded.platforms.wechat_ilink.bot_token == "tk-abc"


def test_public_dict_never_leaks_plain_key(tmp_path: Path) -> None:
    s = Settings()
    s.llm.api_key = "sk-super-secret"
    s.save(tmp_path / "c.json")
    pub = s.public_dict()
    blob = json.dumps(pub, ensure_ascii=False)
    assert "sk-super-secret" not in blob
    assert pub["llm"]["api_key"] == ""
    assert pub["llm"]["api_key_set"] is True
    assert pub["llm"]["api_key_masked"]


def test_mask_key_hides_middle() -> None:
    masked = Settings.mask_key("sk-abcdefghijklmnopqrstuvwxyz")
    assert "abcdefghij" not in masked
    assert masked.startswith("sk-")
    assert Settings.mask_key("") == ""


def test_config_path_prefers_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "custom.json"
    monkeypatch.setenv("MPC_CONFIG", str(target))
    assert Settings.path() == target


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    s = Settings.load(tmp_path / "nope.json")
    assert s.llm.provider == "deepseek"
    assert s.llm.api_key == ""


def test_save_creates_parent_dir(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "config.json"
    Settings().save(target)
    assert target.exists()
