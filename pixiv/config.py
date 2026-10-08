"""Pixiv 站点的配置：框架 ``Settings`` 基座 + Pixiv 凭据与协议常量。

环境变量名沿用既有前缀（``PIXIV_`` / ``PIXIV_BRIDGE_``）——既有的 ``.env`` 里
就是这些名字，改名会破坏升级路径；划分的依据是「路径/布局归框架、站点凭据归站点」，
不是名字前缀。

``Settings`` 在这里再导出一次：站点内的模块（``api`` / ``oauth`` / ``image_proxy``）
原本就 ``from .config import Settings``，搬进 ``pixiv/`` 后一行都不用改。
"""

import os
from dataclasses import dataclass

from pixiv_bridge.config import Settings  # noqa: F401  （re-export：站点内沿用旧写法）

DEFAULT_CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
DEFAULT_CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
DEFAULT_HASH_SALT = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c"
API_BASE = "https://app-api.pixiv.net"
OAUTH_BASE = "https://oauth.secure.pixiv.net"
IMAGE_BASE = "https://i.pximg.net"
IMAGE_REFERER = "https://app-api.pixiv.net/"
REDIRECT_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
USER_AGENT = "PixivAndroidApp/5.0.155 (Android 10.0; Pixel C)"
APP_OS = "Android"
APP_OS_VERSION = "Android 10.0"
APP_VERSION = "5.0.166"


@dataclass
class PixivSettings(Settings):
    """Pixiv 需要的全部配置：框架的通用字段 + 下面这些站点字段。"""

    refresh_token: str = ""
    bypass_sni: bool = False
    ech: bool = False
    ech_config: str = ""
    ech_ips: str = ""
    api_ips: str = ""
    oauth_ips: str = ""
    image_ips: str = ""
    client_id: str = DEFAULT_CLIENT_ID
    client_secret: str = DEFAULT_CLIENT_SECRET
    hash_salt: str = DEFAULT_HASH_SALT
    api_base: str = API_BASE
    oauth_base: str = OAUTH_BASE
    image_base: str = IMAGE_BASE
    image_referer: str = IMAGE_REFERER
    redirect_uri: str = REDIRECT_URI
    user_agent: str = USER_AGENT
    app_os: str = APP_OS
    app_os_version: str = APP_OS_VERSION
    app_version: str = APP_VERSION

    @classmethod
    def load(cls, env_path=None, migrate=True):
        """读配置：先让框架把项目根、``.env`` 与数据目录路径定下来，再补 Pixiv 字段。

        框架的 ``Settings.load`` 负责把 ``.env`` 灌进 ``os.environ``，所以这里
        直接读环境变量即可（既有 ``.env`` 一个都不用改）。
        """
        base = Settings.load(env_path, migrate=migrate)
        return cls(
            **vars(base),
            refresh_token=os.environ.get("PIXIV_REFRESH_TOKEN", ""),
            bypass_sni=os.environ.get("PIXIV_BRIDGE_BYPASS_SNI", "").lower()
            in ("1", "true", "yes", "on"),
            ech=os.environ.get("PIXIV_BRIDGE_ECH", "").lower()
            in ("1", "true", "yes", "on"),
            ech_config=os.environ.get("PIXIV_BRIDGE_ECH_CONFIG", ""),
            ech_ips=os.environ.get("PIXIV_BRIDGE_ECH_IPS", ""),
            api_ips=os.environ.get("PIXIV_BRIDGE_API_IPS", ""),
            oauth_ips=os.environ.get("PIXIV_BRIDGE_OAUTH_IPS", ""),
            image_ips=os.environ.get("PIXIV_BRIDGE_IMAGE_IPS", ""),
        )


def as_pixiv_settings(settings):
    """把框架 ``Settings``（或 ``None``）升级成 ``PixivSettings``。

    站点内部的模块（OAuth / API）需要 client_id 之类的站点字段；调用方只造了
    框架 ``Settings`` 时，这里补上站点字段的默认值，而不是让它在深处抛
    ``AttributeError``。
    """
    if settings is None:
        return PixivSettings.load()
    if isinstance(settings, PixivSettings):
        return settings
    from dataclasses import fields

    base = {f.name: getattr(settings, f.name) for f in fields(Settings)}
    return PixivSettings(**base)
