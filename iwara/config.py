"""iwara 站点的配置：框架 ``Settings`` 基座 + iwara 自己的传输开关。

与 ``pixiv/config.py`` 同形：框架负责项目根、``.env``、数据目录与通用字段，
站点只补自己那几个。环境变量用 ``IWARA_`` 前缀（与既有的
``IWARA_REFRESH_TOKEN`` 一致）—— 各站点是同级的独立入口，各自的开关互不
影响，混用 ``PIXIV_`` 前缀会让人以为改一个两个站都变。

``Settings`` 在这里再导出一次：站点内其它模块写 ``from .config import Settings``
即可，不关心它是框架的还是站点的。
"""

import os
from dataclasses import dataclass

from pixiv_bridge.config import Settings  # noqa: F401  （re-export：站点内沿用同一写法）


def split_ips(value: str) -> tuple[str, ...]:
    """把 ``"1.2.3.4,5.6.7.8"`` 这样的配置串切成 IP 元组。

    与 pixiv 那几个 ``*_ips`` 同形：逗号 / 分号 / 空白都能分隔。
    """
    if not value:
        return ()
    parts = str(value).replace(";", ",").replace(" ", ",").split(",")
    return tuple(part.strip() for part in parts if part.strip())


def _flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")


@dataclass
class IwaraSettings(Settings):
    """iwara 需要的全部配置：框架通用字段 + ECH 三项。

    ECH 默认关：它只在明文 SNI 被链路阻断（握手阶段就被 RST）时才有用，
    开着会多一次 DoH 查询。而且只有 Cloudflare 后面的主机（api / www）
    能用，视频 CDN 主机不适用（见 ``iwara/ech.py``）。
    """

    ech: bool = False
    ech_config: str = ""
    ech_ips: str = ""

    @classmethod
    def load(cls, env_path=None, migrate=True):
        """读配置：先让框架把项目根、``.env`` 与数据目录定下来，再补 iwara 字段。

        框架的 ``Settings.load`` 负责把 ``.env`` 灌进 ``os.environ``，所以这里
        直接读环境变量即可。
        """
        base = Settings.load(env_path, migrate=migrate)
        return cls(
            **vars(base),
            ech=_flag("IWARA_BRIDGE_ECH"),
            ech_config=os.environ.get("IWARA_BRIDGE_ECH_CONFIG", ""),
            ech_ips=os.environ.get("IWARA_BRIDGE_ECH_IPS", ""),
        )


def as_iwara_settings(settings):
    """把框架 ``Settings``（或 ``None``）升级成 ``IwaraSettings``。

    站点内部（``app`` / ``envcheck``）要读 ``ech``；调用方只造了框架
    ``Settings`` 时，这里补上站点字段的默认值，而不是让它在深处抛
    ``AttributeError``。
    """
    if settings is None:
        return IwaraSettings.load()
    if isinstance(settings, IwaraSettings):
        return settings
    from dataclasses import fields

    base = {f.name: getattr(settings, f.name) for f in fields(Settings)}
    return IwaraSettings(**base)
