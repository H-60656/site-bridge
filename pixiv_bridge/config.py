"""运行配置：从 ``.env`` / 环境变量读出 Settings，并把数据文件都收进项目内。
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

# 只取这一个纯函数用于解析 PIXIV_BRIDGE_EXTENSIONS；extensions.py 不反向 import
# 本模块，所以这里没有循环依赖。
from .extensions import parse_extension_specs
from .userdata import (
    DATA_DIR_NAME,
    is_inside,
    migrate_legacy_data,
    relocate_inside_project,
)

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


def _load_dotenv(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    refresh_token: str = ""
    # 所有个人数据都放在项目目录下的 data/ 里，升级时只拷这一个目录即可。
    data_dir: Path = Path(DATA_DIR_NAME)
    token_file: Path = Path(DATA_DIR_NAME) / "tokens.json"
    # 下面四个路径只有「小说阅读子项目」用（阅读历史 / 下载记录 / 收藏台账 / 书架配置）。
    # 接口项目本身不读不写它们，但作为「数据都落在 data/ 下」的统一配置留在 Settings 里，
    # 子项目通过 app.state 拿到 Settings 后自行建台账。
    # 个人阅读历史（JSON Lines）；默认落在 data/history.jsonl。
    history_file: Path = Path(DATA_DIR_NAME) / "history.jsonl"
    # 下载历史（按书 + 章节记录已导出过的 EPUB）。
    downloads_file: Path = Path(DATA_DIR_NAME) / "downloads.jsonl"
    # 收藏台账（pixiv 只有收藏的写接口，本地再记一份用于列收藏列表）。
    bookmarks_file: Path = Path(DATA_DIR_NAME) / "bookmarks.jsonl"
    # 用户配置文件（网页可改）：本地书架目录等设置。
    config_file: Path = Path(DATA_DIR_NAME) / "settings.json"
    # 启动时从项目根目录搬进 data/ 的旧数据文件名（供页面提示用）。
    migrated_files: tuple = field(default_factory=tuple)
    # 启动时的提醒（例如项目外的数据目录被收回 data/）；页面与启动日志都会显示。
    warnings: tuple = field(default_factory=tuple)
    rate_limit_rpm: int = 100
    accept_language: str = "zh-CN"
    request_timeout: float = 30.0
    bypass_sni: bool = False
    proxy: str = ""
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
    project_root: Path = Path.cwd()
    # 要挂到接口项目上的附属项目（扩展）。配置项写目录相对路径，程序去该目录下
    # 找固定的 entry 文件名 extension.py；默认空 = 只跑纯接口，一个扩展都不装。
    #   PIXIV_BRIDGE_EXTENSIONS=novel        挂上小说阅读器
    #   PIXIV_BRIDGE_EXTENSIONS=novel,reader 逗号/分号/空白分隔，按顺序挂
    # 解析成元组前会做去空、去重（见 pixiv_bridge.extensions.parse_extension_specs）。
    extensions: tuple = field(default_factory=tuple)
    # 导航页（`/`）「其它入口」那一段的额外链接，写法 `标题=地址`，逗号分隔。
    # 用来把主项目不认识的东西（例如另一个入口 iwara 的页面）也列上去。
    #   PIXIV_BRIDGE_NAV_LINKS=iwara 视频=http://127.0.0.1:8766/
    nav_links: str = ""

    @classmethod
    def load(cls, env_path=None, migrate=True):
        if env_path:
            target = Path(env_path).expanduser()
            if target.is_dir():
                root = target
                dotenv = root / ".env"
            else:
                # --env 指向具体的 .env 文件：以它的父目录作为项目根，
                # 这样 token_file 等相对路径仍然落在项目目录里。
                root = target.parent or Path.cwd()
                dotenv = target
        else:
            root = Path.cwd()
            dotenv = root / ".env"
        _load_dotenv(dotenv)

        def env_path(name, default):
            """环境变量里的相对路径 → 项目目录下的绝对路径。

            空值（`.env` 里写了 `PIXIV_HISTORY_FILE=`）按「没设置」处理：
            否则 `Path("")` 会变成当前目录，服务会去读整个项目文件夹。
            """
            value = (os.environ.get(name) or "").strip()
            path = Path(value or default)
            if not path.is_absolute():
                path = root / path
            return path

        data_dir = env_path("PIXIV_DATA_DIR", DATA_DIR_NAME)
        token_file = env_path("PIXIV_TOKEN_FILE", str(data_dir / "tokens.json"))
        history_file = env_path("PIXIV_HISTORY_FILE", str(data_dir / "history.jsonl"))
        downloads_file = env_path("PIXIV_DOWNLOADS_FILE", str(data_dir / "downloads.jsonl"))
        bookmarks_file = env_path("PIXIV_BOOKMARKS_FILE", str(data_dir / "bookmarks.jsonl"))
        config_file = env_path("PIXIV_CONFIG_FILE", str(data_dir / "settings.json"))
        # 个人数据只写项目目录里：安卓 / Termux 上项目外的目录（尤其 /sdcard）经常
        # 读得到却写不进，`os.replace` 失败后页面看到的就是「保存配置 500」。
        # 所以这里把项目外的路径收回 `data/`，并留一条提示（页面与启动日志都会显示）。
        notes = []
        project_data_dir = root / DATA_DIR_NAME
        if not is_inside(data_dir, root):
            notes.append(
                "PIXIV_DATA_DIR：%s 在项目目录外，已改用项目内的 %s"
                % (data_dir, project_data_dir)
            )
            data_dir = project_data_dir
        token_file, note = relocate_inside_project(
            token_file, root, data_dir / "tokens.json", "PIXIV_TOKEN_FILE", move=migrate
        )
        if note:
            notes.append(note)
        history_file, note = relocate_inside_project(
            history_file, root, data_dir / "history.jsonl", "PIXIV_HISTORY_FILE", move=migrate
        )
        if note:
            notes.append(note)
        downloads_file, note = relocate_inside_project(
            downloads_file,
            root,
            data_dir / "downloads.jsonl",
            "PIXIV_DOWNLOADS_FILE",
            move=migrate,
        )
        if note:
            notes.append(note)
        bookmarks_file, note = relocate_inside_project(
            bookmarks_file,
            root,
            data_dir / "bookmarks.jsonl",
            "PIXIV_BOOKMARKS_FILE",
            move=migrate,
        )
        if note:
            notes.append(note)
        config_file, note = relocate_inside_project(
            config_file,
            root,
            data_dir / "settings.json",
            "PIXIV_CONFIG_FILE",
            must_write=True,
            move=migrate,
        )
        if note:
            notes.append(note)

        # 老版本的 .env / 默认值写的是项目根目录下的文件名：这种「还指着老位置」
        # 的配置按新默认值处理，否则下面的迁移会把文件搬进 data/、配置却仍指着空路径。
        if token_file == root / "tokens.json":
            token_file = data_dir / "tokens.json"
        if history_file == root / "history.jsonl":
            history_file = data_dir / "history.jsonl"
        # 旧版个人数据（项目根目录下的 tokens.json / history.jsonl）自动搬进 data/。
        # `migrate=False` 给单元测试用：测试不该顺手改动工作区里的真实数据。
        migrated = migrate_legacy_data(root, data_dir) if migrate else ()
        return cls(
            host=os.environ.get("PIXIV_BRIDGE_HOST", "127.0.0.1"),
            port=int(os.environ.get("PIXIV_BRIDGE_PORT", "8765")),
            refresh_token=os.environ.get("PIXIV_REFRESH_TOKEN", ""),
            data_dir=data_dir,
            token_file=token_file,
            history_file=history_file,
            downloads_file=downloads_file,
            bookmarks_file=bookmarks_file,
            config_file=config_file,
            migrated_files=migrated,
            warnings=tuple(notes),
            rate_limit_rpm=int(os.environ.get("PIXIV_RATE_LIMIT_RPM", "100")),
            accept_language=os.environ.get("PIXIV_ACCEPT_LANGUAGE", "zh-CN"),
            request_timeout=float(os.environ.get("PIXIV_REQUEST_TIMEOUT", "30")),
            bypass_sni=os.environ.get("PIXIV_BRIDGE_BYPASS_SNI", "").lower()
            in ("1", "true", "yes", "on"),
            # 代理（例如 http://127.0.0.1:7890）：设了就走代理转发，不再尝试 ECH/SNI 直连。
            proxy=(
                os.environ.get("PIXIV_PROXY", "").strip()
                or os.environ.get("PIXIV_BRIDGE_PROXY", "").strip()
            ),
            ech=os.environ.get("PIXIV_BRIDGE_ECH", "").lower()
            in ("1", "true", "yes", "on"),
            ech_config=os.environ.get("PIXIV_BRIDGE_ECH_CONFIG", ""),
            ech_ips=os.environ.get("PIXIV_BRIDGE_ECH_IPS", ""),
            api_ips=os.environ.get("PIXIV_BRIDGE_API_IPS", ""),
            oauth_ips=os.environ.get("PIXIV_BRIDGE_OAUTH_IPS", ""),
            image_ips=os.environ.get("PIXIV_BRIDGE_IMAGE_IPS", ""),
            extensions=tuple(
                parse_extension_specs(os.environ.get("PIXIV_BRIDGE_EXTENSIONS", ""))
            ),
            nav_links=os.environ.get("PIXIV_BRIDGE_NAV_LINKS", "").strip(),
            project_root=root,
        )
