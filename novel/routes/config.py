"""用户配置与本地书架接口。

- `GET /api/config` / `PUT /api/config`：读写 `data/settings.json`（本地用户配置文件）。
- `GET /api/library`：按配置目录扫描 `.txt` / `.epub`。
- `GET /api/library/preview`：`.txt` 试读（最多 2 MB）。
- `GET /api/library/file`：把配置目录里的文件交给浏览器下载/打开。
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from site_bridge.compat import dump_model
from site_bridge.userdata import DEFAULT_CONFIG, check_writable_dir
from . import get_config, get_library

router = APIRouter(prefix="/api", tags=["config"])

# 个人数据目录必须能写：安卓 / Termux 上把数据目录指到 `/sdcard` 是「保存配置 500」
# 的最常见原因，这里给出对应提示。
TERMUX_HINT = (
    "安卓 / Termux 上请把项目放在 Termux 家目录里（例如 ~/site-bridge），"
    "不要把数据目录指到 /sdcard 下的路径。"
)


class ConfigPatch(BaseModel):
    """只允许改这几个键（多余字段忽略）。"""

    library_dirs: list[str] | None = None
    merge_series: bool | None = None
    skip_downloaded: bool | None = None
    scan_depth: int | None = None
    max_files: int | None = None


def _reason(exc):
    """把异常变成用户看得懂的一句话。"""
    text = getattr(exc, "strerror", None) or str(exc) or exc.__class__.__name__
    return str(text).strip().rstrip("。.")


def _writable_state(settings):
    """数据目录能不能写。探针本身出错也不能把接口带崩。"""
    try:
        return check_writable_dir(settings.data_dir)
    except Exception as exc:                      # noqa: BLE001 - 只为兜底
        return False, "检查失败：%s" % _reason(exc)


def _payload(request):
    settings = request.app.state.settings
    config = get_config(request)
    library = get_library(request)
    writable, why = _writable_state(settings)
    try:
        dirs = [str(path) for path in library.dirs()]
        missing = library.missing_dirs()
    except Exception:                             # noqa: BLE001 - 书架目录读不到就算了
        dirs, missing = [], []
    return {
        "ok": True,
        "file": str(config.path),
        "data_dir": str(settings.data_dir),
        "defaults": dict(DEFAULT_CONFIG),
        "config": config.as_dict(),
        "migrated_files": list(settings.migrated_files or ()),
        "warnings": list(settings.warnings or ()),
        # 数据目录写不写得进去（页面在保存前就能提示，不用等 500）。
        "writable": {"ok": writable, "dir": str(settings.data_dir), "reason": why},
        "library": {"dirs": dirs, "missing": missing},
        "files": {
            "tokens": str(settings.token_file),
            "history": str(settings.history_file),
            "downloads": str(settings.downloads_file),
            "bookmarks": str(settings.bookmarks_file),
        },
    }


@router.get("/config")
async def read_config(request: Request):
    """读当前设置：数据目录、本地书架目录、下载行为开关与各数据文件路径。"""
    try:
        return _payload(request)
    except Exception as exc:                      # noqa: BLE001 - 兜底给原因
        raise HTTPException(
            status_code=500,
            detail="读取设置失败：%s（%s）。" % (_reason(exc), exc.__class__.__name__),
        ) from exc


@router.put("/config")
async def write_config(body: ConfigPatch, request: Request):
    patch = dump_model(body, exclude_unset=True)
    config = get_config(request)
    try:
        config.save(patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        # 写盘失败（文件被占用 / 只读 / 目录不可写）不能是光秃秃的 500：
        # 把路径和系统原因一起交给页面，用户才知道该关掉什么程序。
        raise HTTPException(
            status_code=500,
            detail=(
                "写入失败：%s。配置文件 %s 可能正被其它程序打开、带了只读属性，"
                "或被同步盘/杀毒软件锁定——关掉占用它的程序后重试。%s"
                % (_reason(exc), config.path, TERMUX_HINT)
            ),
        ) from exc
    except Exception as exc:                      # noqa: BLE001 - 兜底给原因
        raise HTTPException(
            status_code=500,
            detail="写入失败：%s（%s）。配置文件 %s 所在目录可能不可写，%s"
            % (_reason(exc), exc.__class__.__name__, config.path, TERMUX_HINT),
        ) from exc
    try:
        return _payload(request)
    except Exception as exc:                      # noqa: BLE001 - 已经存上了，不能报失败
        settings = request.app.state.settings
        return {
            "ok": True,
            "saved": True,
            "file": str(config.path),
            "config": config.as_dict(),
            "warnings": list(getattr(settings, "warnings", ()) or ()),
            "payload_error": "设置已保存，但读取当前状态失败：%s" % _reason(exc),
        }


@router.get("/library")
async def scan_library(request: Request):
    """扫描本地书架目录，返回 .txt / .epub 列表（按修改时间倒序）。"""
    try:
        return get_library(request).scan()
    except OSError as exc:
        # 目录权限/硬盘异常时也别丢一个光秃秃的 500 给页面。
        reason = getattr(exc, "strerror", None) or exc.__class__.__name__
        raise HTTPException(
            status_code=500,
            detail="扫描失败：%s。检查本地书架目录的权限，或先把它从设置里移除。"
            % str(reason).strip().rstrip("。."),
        ) from exc


@router.get("/library/preview")
async def preview_library_file(request: Request, path: str):
    try:
        return get_library(request).preview(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail="读取失败：%s" % exc.__class__.__name__
        ) from exc


@router.get("/library/file")
async def download_library_file(request: Request, path: str):
    try:
        target = get_library(request).resolve(path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    media = (
        "application/epub+zip"
        if target.suffix.lower() == ".epub"
        else "text/plain; charset=utf-8"
    )
    return FileResponse(target, media_type=media, filename=target.name)
