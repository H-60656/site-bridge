"""iwara 登录态的落盘（refresh token）。

和主项目的 `data/tokens.json`（pixiv 的）分开放，互不污染：pixiv 那边
存的是 ``{"refresh_token": ...}``，这里是 iwara 的 JWT，换了站点就没用。
默认落在项目目录的 ``data/iwara_tokens.json``。

文件权限尽力收紧到 0600 —— token 等价于账号，别跟着仓库走
（``data/`` 已在 .gitignore 里）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_FILENAME = "iwara_tokens.json"
ENV_VAR = "IWARA_REFRESH_TOKEN"


def default_path(data_dir: Path | str | None = None) -> Path:
    """token 文件的位置：给了 data_dir 就用它，否则项目目录下的 data/。"""
    if data_dir:
        return Path(data_dir) / DEFAULT_FILENAME
    return Path(__file__).resolve().parent.parent / "data" / DEFAULT_FILENAME


def load_token(path: Path | str | None = None) -> str:
    """读出 refresh token；文件不在、读不动或内容不合法都返回空串。

    这里刻意不抛异常：调用方是启动路径，坏掉的 token 文件只该退化成
    「匿名模式」，不该让整个服务起不来。
    """
    target = Path(path) if path else default_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(raw, dict):
        return ""
    token = raw.get("refresh_token")
    return token.strip() if isinstance(token, str) else ""


def save_token(refresh_token: str, path: Path | str | None = None) -> Path:
    """把 refresh token 写进文件，顺带把权限收到 0600，返回实际路径。"""
    target = Path(path) if path else default_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"refresh_token": refresh_token}, ensure_ascii=False, indent=2)
    # 先写同目录临时文件再替换：避免写到一半被中断留下半截 JSON。
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    _chmod_private(tmp)
    os.replace(tmp, target)
    _chmod_private(target)
    return target


def clear_token(path: Path | str | None = None) -> bool:
    """删掉 token 文件，返回是否真的删了（不存在算 False）。"""
    target = Path(path) if path else default_path()
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return True


def _chmod_private(path: Path) -> None:
    """尽量把权限收到只有自己能读写；Windows 上大概率无效，忽略即可。"""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def resolve(data_dir: Path | str | None, env_token: str = "") -> str:
    """按「环境变量优先，其次文件」取 refresh token。

    环境变量优先是有意的：临时用别人的账号跑一次，不必覆盖本机已存的文件。
    """
    if env_token and env_token.strip():
        return env_token.strip()
    return load_token(default_path(data_dir))
