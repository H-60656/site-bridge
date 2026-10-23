"""ehviewer 站点的配置。

比 asmr / iwara 简单：这个站点**不需要 ECH**（它的封锁是 SNI 字符串 + 本地
DNS 投毒，不是 Cloudflare 侧的握手问题），所以没有 ``ech*`` 那一组配置；
取而代之的是 ``http3``（必须开）、借道边缘池与图片重试次数。

沿用 asmr 的写法：站点配置继承框架 ``Settings``，用**站点自己的环境变量前缀**
（``EH_BRIDGE_*``），不混用 ``PIXIV_``。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields

from site_bridge.config import Settings  # noqa: F401  （re-export：站点内沿用同一写法）


def split_ips(value: str) -> tuple[str, ...]:
    """逗号 / 分号 / 空白分隔的地址串 → 元组。"""
    items = str(value or "").replace(";", ",").replace(" ", ",").replace("\t", ",")
    return tuple(part.strip() for part in items.split(",") if part.strip())


def _flag(name: str) -> bool:
    """环境变量是不是「开着」；没写也算 False。"""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _flag_or_default(name: str, default: bool) -> bool:
    """没写（或只写了空白）才用默认值；写了 ``0`` / ``false`` 就是关。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int_or_default(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


@dataclass
class EhSettings(Settings):
    """站点专属配置（另有 ``EH_BRIDGE_*`` 前缀的环境变量）。"""

    #: HTTP/3（QUIC）—— 这个站点的 TCP 路径在本机是死的，关掉就等于关掉站点。
    http3: bool = True
    #: 借道用的 Cloudflare 边缘 IP，留空用 ``client.EDGE_IPS``。
    edge_ips: str = ""
    #: 一个主机最多试几个边缘。
    edge_attempts: int = 5
    #: 图片落在死节点上时最多重解析几次 ``/s/`` 页。
    image_attempts: int = 4

    @classmethod
    def load(cls, env_path=None, migrate: bool = True) -> "EhSettings":
        base = Settings.load(env_path, migrate=migrate)
        return cls(
            **vars(base),
            http3=_flag_or_default("EH_BRIDGE_HTTP3", True),
            edge_ips=os.environ.get("EH_BRIDGE_EDGE_IPS", ""),
            edge_attempts=_int_or_default("EH_BRIDGE_EDGE_ATTEMPTS", 5),
            image_attempts=_int_or_default("EH_BRIDGE_IMAGE_ATTEMPTS", 4),
        )


def as_eh_settings(settings) -> EhSettings:
    """把框架的 ``Settings`` 升级成本站点的 ``Settings``。

    ``run.py`` 走框架时拿到的是框架 ``Settings``（它不认识 ``http3`` 这些
    字段），所以各处都要过一道这个函数。已是本类就原样返回。
    """
    if settings is None:
        return EhSettings.load()
    if isinstance(settings, EhSettings):
        return settings
    base = {field.name: getattr(settings, field.name) for field in fields(Settings)}
    return EhSettings(
        **base,
        http3=_flag_or_default("EH_BRIDGE_HTTP3", True),
        edge_ips=os.environ.get("EH_BRIDGE_EDGE_IPS", ""),
        edge_attempts=_int_or_default("EH_BRIDGE_EDGE_ATTEMPTS", 5),
        image_attempts=_int_or_default("EH_BRIDGE_IMAGE_ATTEMPTS", 4),
    )
