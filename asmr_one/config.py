"""asmr_one 站点的配置：框架 ``Settings`` 基座 + 自己的传输开关。

与 ``iwara/config.py`` 同形：框架负责项目根、``.env``、数据目录与通用字段，
站点只补自己那几个。环境变量用 ``ASMR_`` 前缀 —— 三个站点是同级的独立入口，
各自的开关互不影响，混用 ``PIXIV_`` 前缀会让人以为改一个全都变。

``Settings`` 在这里再导出一次：站点内其它模块写 ``from .config import Settings``
即可，不关心它是框架的还是站点的。
"""

import os
from dataclasses import dataclass

from site_bridge.config import Settings  # noqa: F401  （re-export：站点内沿用同一写法）


def split_ips(value: str) -> tuple[str, ...]:
    """把 ``"1.2.3.4,5.6.7.8"`` 这样的配置串切成 IP 元组。

    与 pixiv / iwara 那几个 ``*_ips`` 同形：逗号 / 分号 / 空白都能分隔。
    """
    if not value:
        return ()
    parts = str(value).replace(";", ",").replace(" ", ",").split(",")
    return tuple(part.strip() for part in parts if part.strip())


def _flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes", "on")


def _flag_or_default(name: str, default: bool) -> bool:
    """读布尔开关：**没写这个变量**时返回 ``default``，写了就按它算。

    ``_flag`` 把「没写」也算成 False，而 asmr 的 ECH 默认要开 —— 想关必须
    显式写 ``ASMR_BRIDGE_ECH=0``，不能靠「忘写」。
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class AsmrSettings(Settings):
    """asmr_one 需要的全部配置：框架通用字段 + ECH 三项。

    与 iwara 不同的是 ``ech`` **默认开**：asmr.one 自己那几个边缘 IP 在本机是
    IP 级被阻断的（一律 RST），只有「借道别的 Cloudflare anycast IP + ECH」
    才通；关掉 ECH 等于这个站点不可用。真跑不通时再显式关。
    """

    ech: bool = True
    ech_config: str = ""
    ech_ips: str = ""

    @classmethod
    def load(cls, env_path=None, migrate=True):
        """读配置：先让框架把项目根、``.env`` 与数据目录定下来，再补站点字段。

        框架的 ``Settings.load`` 负责把 ``.env`` 灌进 ``os.environ``，所以这里
        直接读环境变量即可。``ASMR_BRIDGE_ECH`` **不写**时是开着的（默认
        ``True``），要关必须显式写成 0 / false / no / off。
        """
        base = Settings.load(env_path, migrate=migrate)
        return cls(
            **vars(base),
            ech=_flag_or_default("ASMR_BRIDGE_ECH", True),
            ech_config=os.environ.get("ASMR_BRIDGE_ECH_CONFIG", ""),
            ech_ips=os.environ.get("ASMR_BRIDGE_ECH_IPS", ""),
        )


def as_asmr_settings(settings):
    """把框架 ``Settings``（或 ``None``）升级成 ``AsmrSettings``。

    站点内部（``app`` / ``envcheck``）要读 ``ech``；调用方只造了框架
    ``Settings`` 时，这里补上站点字段的默认值，而不是让它在深处抛
    ``AttributeError``。
    """
    if settings is None:
        return AsmrSettings.load()
    if isinstance(settings, AsmrSettings):
        return settings
    from dataclasses import fields

    base = {f.name: getattr(settings, f.name) for f in fields(Settings)}
    return AsmrSettings(**base)
