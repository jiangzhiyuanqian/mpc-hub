"""连接器注册表。

枢纽通过它找到"某个平台该由谁去回话"，并对外公布每个平台的能力矩阵。
单独成文件是为了让 `plugins.py`（扩展点加载器）能引用而不造成循环导入。
"""

from __future__ import annotations

from ..contracts import Capability, Platform
from .base import Connector


class Registry:
    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {}

    def register(self, connector: Connector, name: str | None = None) -> Connector:
        key = name or connector.platform.value
        self._connectors[key] = connector
        return connector

    def get(self, name: str) -> Connector | None:
        return self._connectors.get(name)

    def for_platform(self, platform: Platform | str) -> Connector | None:
        p = platform.value if isinstance(platform, Platform) else str(platform)
        return self._connectors.get(p)

    def capabilities(self) -> list[Capability]:
        return [c.capability() for c in self._connectors.values()]

    def names(self) -> list[str]:
        return sorted(self._connectors)

    def start_all(self) -> None:
        for c in self._connectors.values():
            c.start()

    def stop_all(self) -> None:
        for c in self._connectors.values():
            c.stop()
