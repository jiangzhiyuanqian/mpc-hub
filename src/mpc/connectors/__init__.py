"""连接器注册表与内置适配器。

枢纽通过注册表找到"某个平台该由谁去回话"，并对外公布每个平台的能力矩阵。
"""

from __future__ import annotations

from ..contracts import Capability, Platform
from .base import Connector
from .loopback import LoopbackConnector
from .onebot import OneBotConnector
from .registry import Registry
from .webhook import WebhookConnector

__all__ = [
    "Connector",
    "LoopbackConnector",
    "OneBotConnector",
    "WebhookConnector",
    "Registry",
    "Capability",
    "Platform",
]
