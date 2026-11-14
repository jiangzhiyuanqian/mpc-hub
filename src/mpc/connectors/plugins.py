"""连接器扩展点。

两种接法，不用改本仓库的代码：

1. **配置文件里写模块路径**（`~/.mpc-hub/config.json`）：

```json
{"platforms": {"custom": [
  {"platform": "telegram", "module": "my_pkg.telegram", "attr": "create"}
]}}
```

   模块里要有一个 `create(**options) -> Connector` 函数。

2. **Python 入口点**（发到 PyPI 的插件包）：

```toml
[project.entry-points."mpc.connectors"]
telegram = "my_pkg.telegram:create"
```

两种都会被 `load_custom_connectors` 收进来，能力矩阵里也会出现。
"""

from __future__ import annotations

import importlib
from typing import Any

from ..contracts import Platform
from ..settings import CustomConnectorSettings
from .base import Connector
from .registry import Registry  # noqa: F401  (兼容旧引用路径)


def _call_factory(factory: Any, options: dict[str, Any]) -> Connector:
    """调用工厂函数，尽量兼容不同签名。"""
    try:
        return factory(**options)
    except TypeError:
        return factory(options)


def _from_settings(
    registry: Registry, items: list[CustomConnectorSettings]
) -> list[str]:
    loaded: list[str] = []
    for item in items:
        if not item.enabled or not item.module:
            continue
        try:
            module = importlib.import_module(item.module)
            factory = getattr(module, item.attr or "create")
            connector = _call_factory(factory, dict(item.options or {}))
        except Exception as exc:  # 一个插件坏掉不该拖垮整个后台
            loaded.append(f"✗ {item.platform or item.module}: {exc}")
            continue
        name = item.platform or connector.platform.value
        registry.register(connector, name)
        loaded.append(f"✓ {name} ← {item.module}:{item.attr}")
    return loaded


def _from_entry_points(registry: Registry) -> list[str]:
    loaded: list[str] = []
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover
        return loaded
    try:
        eps = entry_points(group="mpc.connectors")
    except TypeError:  # pragma: no cover - 老版本 Python
        eps = entry_points().get("mpc.connectors", [])  # type: ignore[union-attr]
    for ep in eps:
        try:
            connector = ep.load()()
        except Exception as exc:
            loaded.append(f"✗ {ep.name}: {exc}")
            continue
        registry.register(connector, ep.name)
        loaded.append(f"✓ {ep.name} ← 插件 {ep.value}")
    return loaded


def load_custom_connectors(
    registry: Registry, items: list[CustomConnectorSettings] | None = None
) -> list[str]:
    """把配置里的自定义连接器与已安装的入口点插件都装进 registry。"""
    notes = _from_settings(registry, items or [])
    notes.extend(_from_entry_points(registry))
    return notes


__all__ = ["load_custom_connectors", "Platform"]
