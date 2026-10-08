"""运行配置基座：从 ``.env`` / 环境变量读出 Settings，并把数据文件都收进项目内。

这里只有**站点无关**的配置（监听地址、数据目录、代理、扩展、导航链接）。站点自己的
凭据与协议参数在站点目录里（``pixiv/config.py`` 的 ``PixivSettings``，继承本模块的
``Settings`` 再加 Pixiv 的字段）。

环境变量名沿用 ``PIXIV_BRIDGE_*`` / ``PIXIV_*``（既有用户的 ``.env`` 里就是这些名字，
改名会破坏升级路径），这里按「路径/布局归框架、站点凭据归站点」划分：
数据文件路径由框架解析，站点凭据由站点解析。
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
    # 所有个人数据都放在项目目录下的 data/ 里，升级时只拷这一个目录即可。
    data_dir: Path = Path(DATA_DIR_NAME)
    # 下面五个路径是「数据统一落 data/」这一约定的载体：站点与扩展按需取用
    # （tokens.json 给接口项目存令牌，其余四个给小说阅读器那样的台账）。
    # 框架不读它们的内容，只负责把项目外的路径收回项目内。
    token_file: Path = Path(DATA_DIR_NAME) / "tokens.json"
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
    # 代理（例如 http://127.0.0.1:7890）：设了就走代理转发，不再尝试直连。
    proxy: str = ""
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

    @staticmethod
    def locate(env_path=None):
        """把 ``--env`` 参数解析成 ``(项目根, .env 路径)``。

        给的是目录就当项目根；给的是 .env 文件就以它的父目录作为项目根，
        这样 token_file 等相对路径仍然落在项目目录里。
        """
        if env_path:
            target = Path(env_path).expanduser()
            if target.is_dir():
                return target, target / ".env"
            return (target.parent or Path.cwd()), target
        root = Path.cwd()
        return root, root / ".env"

    @staticmethod
    def data_paths(root, env_path_fn, migrate=True):
        """解析 data/ 下那五个数据文件路径，把项目外的路径收回项目内。

        返回 ``(data_dir, token_file, history_file, downloads_file, bookmarks_file,
        config_file, notes)``。个人数据只写项目目录里：安卓 / Termux 上项目外的目录
        （尤其 /sdcard）经常读得到却写不进，``os.replace`` 失败后页面看到的就是
        「保存配置 500」。所以这里把项目外的路径收回 ``data/``，并留一条提示
        （页面与启动日志都会显示）。
        """
        data_dir = env_path_fn("PIXIV_DATA_DIR", DATA_DIR_NAME)
        token_file = env_path_fn("PIXIV_TOKEN_FILE", str(data_dir / "tokens.json"))
        history_file = env_path_fn("PIXIV_HISTORY_FILE", str(data_dir / "history.jsonl"))
        downloads_file = env_path_fn("PIXIV_DOWNLOADS_FILE", str(data_dir / "downloads.jsonl"))
        bookmarks_file = env_path_fn("PIXIV_BOOKMARKS_FILE", str(data_dir / "bookmarks.jsonl"))
        config_file = env_path_fn("PIXIV_CONFIG_FILE", str(data_dir / "settings.json"))
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
        return data_dir, token_file, history_file, downloads_file, bookmarks_file, config_file, notes

    @classmethod
    def load(cls, env_path=None, migrate=True):
        root, dotenv = cls.locate(env_path)
        _load_dotenv(dotenv)

        def simple_env_path(name, default):
            """环境变量里的相对路径 → 项目目录下的绝对路径。

            空值（`.env` 里写了 `PIXIV_HISTORY_FILE=`）按「没设置」处理：
            否则 `Path("")` 会变成当前目录，服务会去读整个项目文件夹。
            """
            value = (os.environ.get(name) or "").strip()
            path = Path(value or default)
            if not path.is_absolute():
                path = root / path
            return path

        (
            data_dir,
            token_file,
            history_file,
            downloads_file,
            bookmarks_file,
            config_file,
            notes,
        ) = cls.data_paths(root, simple_env_path, migrate=migrate)

        # 旧版个人数据（项目根目录下的 tokens.json / history.jsonl）自动搬进 data/。
        # `migrate=False` 给单元测试用：测试不该顺手改动工作区里的真实数据。
        migrated = migrate_legacy_data(root, data_dir) if migrate else ()
        return cls(
            host=os.environ.get("PIXIV_BRIDGE_HOST", "127.0.0.1"),
            port=int(os.environ.get("PIXIV_BRIDGE_PORT", "8765")),
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
            # 代理（例如 http://127.0.0.1:7890）：设了就走代理转发。
            proxy=(
                os.environ.get("PIXIV_PROXY", "").strip()
                or os.environ.get("PIXIV_BRIDGE_PROXY", "").strip()
            ),
            extensions=tuple(
                parse_extension_specs(os.environ.get("PIXIV_BRIDGE_EXTENSIONS", ""))
            ),
            nav_links=os.environ.get("PIXIV_BRIDGE_NAV_LINKS", "").strip(),
            project_root=root,
        )
