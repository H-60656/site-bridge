"""用户数据目录、用户配置文件与本地书架。

设计要点：
- 所有个人数据都集中在 `data/` 子目录下（`data/settings.json`、`data/tokens.json`、
  `data/history.jsonl`、`data/downloads.jsonl`、`data/bookmarks.jsonl`），升级时
  只拷 `data/` 就能把数据带走，程序文件可以整个覆盖。
- 旧版本把 `tokens.json` / `history.jsonl` 放在项目根目录，启动时自动搬进 `data/`
  （只在目标不存在时搬，幂等，不会覆盖新数据）。
- `data/settings.json` 是「用户配置文件」：本地文件，网页上可以改（`/api/config`）。
- 本地书架只认白名单目录里的 `.txt` / `.epub`：扫描只读元信息，取文件时也必须落在
  这些目录里，避免把任意路径变成可下载的文件。
"""

import errno
import json
import os
import shutil
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR_NAME = "data"
CONFIG_NAME = "settings.json"
# 老版本放在项目根目录的个人数据文件名。
LEGACY_NAMES = ("tokens.json", "history.jsonl")

# 「也许只是暂时被占用」的 errno：值得退避重试。其余（EXDEV/EROFS/ENOSYS…）立刻退回覆盖写。
_RETRY_ERRNOS = (errno.EACCES, errno.EPERM, errno.EBUSY)


BOOK_EXTENSIONS = (".txt", ".epub")
KIND_BY_EXT = {".txt": "txt", ".epub": "epub"}
# 单次预览最多读这么多字节，避免一本几十兆的 txt 把内存和页面撑爆。
PREVIEW_MAX_BYTES = 2 * 1024 * 1024

MAX_LIBRARY_DIRS = 20
MAX_DIR_LENGTH = 500
MAX_FILES_RANGE = (50, 5000)
SCAN_DEPTH_RANGE = (1, 6)

DEFAULT_CONFIG = {
    # 本地书架扫描的目录（绝对路径或带 ~ 的路径，网页上可改）。
    "library_dirs": [],
    # 下载时把「系列名相同」的章节合并成一本 EPUB。
    "merge_series": True,
    # 合并/下载系列时跳过已经下载过的章节。
    "skip_downloaded": True,
    # 扫描子目录的层数。
    "scan_depth": 3,
    # 一次最多列出的本地文件数。
    "max_files": 1000,
}


def iso_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normcase(path):
    return os.path.normcase(str(path))


def is_inside(child, parent):
    """child 是否就是 parent 或在 parent 目录里（Windows 下不区分大小写）。"""
    child_s = _normcase(Path(child).resolve())
    parent_s = _normcase(Path(parent).resolve())
    if child_s == parent_s:
        return True
    return child_s.startswith(parent_s.rstrip("\\/") + os.sep)


def migrate_legacy_data(root, data_dir, names=LEGACY_NAMES):
    """把项目根目录里的旧个人数据搬进 data/，返回搬动的文件名列表。

    只在「源文件存在 + 目标不存在」时搬，所以可以每次启动都调用。
    """
    root = Path(root)
    data_dir = Path(data_dir)
    moved = []
    for name in names:
        source = root / name
        target = data_dir / name
        if not source.is_file() or target.exists():
            continue
        data_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        moved.append(name)
    return tuple(moved)


def _clear_readonly(path):
    """摘掉目标的只读位。

    Windows 上目标文件带只读属性时 `os.replace` 会抛 `PermissionError`（WinError 5），
    而用户往往只是不小心把文件设成了只读——先清掉这个属性，保存就能继续。
    """
    try:
        target = Path(path)
        if target.exists() and not target.stat().st_mode & stat.S_IWRITE:
            os.chmod(target, stat.S_IWRITE)
    except OSError:
        pass


def _unlink_quietly(path):
    try:
        Path(path).unlink()
    except OSError:
        pass


def _replace_with_retry(tmp, target, attempts=3, delay=0.15):
    """原子替换，失败时退避重试。

    目标被别的程序打开、被同步盘/杀软扫描时会短暂抛 `PermissionError`
    （WinError 5「拒绝访问」/ WinError 32「另一个程序正在使用」），等一下往往就好了。
    只重试这类「可能只是暂时」的错误；`EXDEV`（跨设备）/`EROFS`（只读）之类的结构性
    失败立刻抛出，交给调用方退回原地覆盖写——安卓的 `/sdcard` 就属于这种。
    """
    _clear_readonly(target)
    for index in range(attempts):
        try:
            os.replace(tmp, target)
            return
        except OSError as exc:
            retryable = isinstance(exc, PermissionError) or (
                getattr(exc, "errno", None) in _RETRY_ERRNOS
            )
            if not retryable or index + 1 >= attempts:
                raise
            time.sleep(delay * (index + 1))


def _reason(exc):
    """把 OSError 变成一句可读的中文原因（`Permission denied` / `Read-only file system`）。"""
    text = getattr(exc, "strerror", None) or exc.__class__.__name__
    return str(text).strip().rstrip("。.")


def check_writable_dir(path):
    """目录到底能不能写：真写一个探针文件再删掉。

    `os.access(..., os.W_OK)` 在 Windows 上对只读目录也会返回 True，靠不住；
    安卓的 `/sdcard`（FUSE）更是「读得到、写不进」。这里真写一次，返回 `(可写, 原因)`。
    """
    path = Path(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, "建目录失败：%s" % _reason(exc)
    probe = path / ".pixiv-bridge-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
    except OSError as exc:
        return False, "写不进去：%s" % _reason(exc)
    finally:
        _unlink_quietly(probe)
    return True, ""


def relocate_inside_project(path, root, target, name, must_write=False, move=True):
    """把项目外的数据路径收回项目内，返回 `(实际使用的路径, 提示文字或 None)`。

    为什么要收回：安卓 / Termux 上项目外的目录（尤其 `/sdcard` 那边）经常读得到却写
    不进——`os.replace` 会以 `EXDEV`/`EPERM` 失败，页面看到的就是「保存配置 500」。
    所以个人数据一律只写项目目录里的 `data/`。项目外的同名文件先搬进来；搬不动时，
    配置文件（`must_write=True`）改用项目内的新文件，其余数据（token/历史/下载/收藏）
    暂时继续用原位置，至少不丢已有数据。
    """
    path = Path(path)
    if is_inside(path, root):
        return path, None
    target = Path(target)
    note = "%s：%s 在项目目录外，已改用项目内的 %s" % (name, path, target)
    if move and path.exists():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.move(str(path), str(target))
                return target, note + "（原文件已搬进来）"
            note += "（项目内已有同名文件，按项目内的算）"
            return target, note
        except OSError as exc:
            note += "（原位置的文件搬不动：%s）" % _reason(exc)
            if not must_write:
                return path, note
    return target, note


def _clean_dirs(value):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("library_dirs 必须是目录列表")
    result = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        text = os.path.expanduser(text)
        if len(text) > MAX_DIR_LENGTH:
            raise ValueError("目录路径太长：%s" % text[:60])
        if text not in result:
            result.append(text)
    if len(result) > MAX_LIBRARY_DIRS:
        raise ValueError("本地书架目录最多 %d 个" % MAX_LIBRARY_DIRS)
    return result


def _as_bool(value, default):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError("只能是 true/false：%r" % (value,))


def _as_int_in_range(value, default, low, high, label):
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s 必须是数字" % label) from None
    if number < low or number > high:
        raise ValueError("%s 必须在 %d ~ %d 之间" % (label, low, high))
    return number


class UserConfig:
    """`data/settings.json`：本地用户配置文件，网页可以读写。"""

    def __init__(self, path):
        self.path = Path(path)
        if self.path.is_dir():
            raise ValueError(f"用户配置文件路径指向一个目录：{self.path}")
        self._lock = threading.Lock()
        self._config = dict(DEFAULT_CONFIG)

    # ---- 读 ----
    def load(self):
        with self._lock:
            self._config = self._read_locked()
            return dict(self._config)

    def _read_locked(self):
        if not self.path.exists():
            return dict(DEFAULT_CONFIG)
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 文件坏了不影响启动：退回默认值，等下次保存时再覆盖。
            return dict(DEFAULT_CONFIG)
        if not isinstance(raw, dict):
            return dict(DEFAULT_CONFIG)
        try:
            return self.validate(raw)
        except ValueError:
            return dict(DEFAULT_CONFIG)

    def as_dict(self):
        with self._lock:
            return dict(self._config)

    # ---- 校验 / 写 ----
    @staticmethod
    def validate(raw):
        if not isinstance(raw, dict):
            raise ValueError("配置必须是一个 JSON 对象")
        config = {
            "library_dirs": _clean_dirs(raw.get("library_dirs")),
            "merge_series": _as_bool(raw.get("merge_series"), DEFAULT_CONFIG["merge_series"]),
            "skip_downloaded": _as_bool(
                raw.get("skip_downloaded"), DEFAULT_CONFIG["skip_downloaded"]
            ),
            "scan_depth": _as_int_in_range(
                raw.get("scan_depth"),
                DEFAULT_CONFIG["scan_depth"],
                SCAN_DEPTH_RANGE[0],
                SCAN_DEPTH_RANGE[1],
                "扫描层数",
            ),
            "max_files": _as_int_in_range(
                raw.get("max_files"),
                DEFAULT_CONFIG["max_files"],
                MAX_FILES_RANGE[0],
                MAX_FILES_RANGE[1],
                "最多列出文件数",
            ),
        }
        return config

    def save(self, patch):
        """按 patch 合并并原子写入，返回写入后的完整配置。"""
        if patch is None:
            patch = {}
        if not isinstance(patch, dict):
            raise ValueError("配置必须是一个 JSON 对象")
        with self._lock:
            merged = dict(self._config)
            for key, value in patch.items():
                if key not in DEFAULT_CONFIG:
                    continue
                merged[key] = value
            config = self.validate(merged)
            self._write_locked(config)
            self._config = config
            return dict(config)

    def _write_locked(self, config):
        """写盘：优先「临时文件 + 原子替换」，换不动就退回直接覆盖写。

        Windows 上 `os.replace` 在目标被别的程序打开、带只读属性，或被同步盘/杀软扫描时
        会抛 `PermissionError`；这类锁往往是短暂的，所以先退避重试，仍失败就原地覆盖写
        （牺牲「原子」换「能存上」），并保证不留下 `.tmp` 残骸。
        """
        text = json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        _clear_readonly(tmp)
        try:
            tmp.write_text(text, encoding="utf-8")
            _replace_with_retry(tmp, self.path)
        except OSError:
            _unlink_quietly(tmp)
            _clear_readonly(self.path)
            self.path.write_text(text, encoding="utf-8")
        finally:
            _unlink_quietly(tmp)


class LocalLibrary:
    """按用户配置扫描本地 `.txt` / `.epub` 文件，并提供受控的文件读取。"""

    def __init__(self, config):
        self.config = config

    # ---- 目录 ----
    def dirs(self):
        result = []
        for item in self.config.as_dict().get("library_dirs") or []:
            path = Path(os.path.expanduser(str(item)))
            if path.is_dir() and not any(_normcase(path) == _normcase(p) for p in result):
                result.append(path)
        return result

    def missing_dirs(self):
        result = []
        for item in self.config.as_dict().get("library_dirs") or []:
            path = Path(os.path.expanduser(str(item)))
            if not path.exists():
                result.append(str(path))
            elif not path.is_dir():
                result.append(str(path) + "（不是目录）")
        return result

    # ---- 扫描 ----
    def scan(self):
        settings = self.config.as_dict()
        depth = int(settings.get("scan_depth") or DEFAULT_CONFIG["scan_depth"])
        max_files = int(settings.get("max_files") or DEFAULT_CONFIG["max_files"])
        files = []
        errors = []
        truncated = False
        for root in self.dirs():
            for directory, filenames in self._walk(root, depth, errors):
                for name in filenames:
                    suffix = Path(name).suffix.lower()
                    if suffix not in BOOK_EXTENSIONS:
                        continue
                    full = directory / name
                    try:
                        stat = full.stat()
                    except OSError as exc:
                        errors.append("%s：%s" % (full, exc.__class__.__name__))
                        continue
                    if not full.is_file():
                        continue
                    files.append(
                        {
                            "name": name,
                            "path": str(full),
                            "dir": str(root),
                            "dir_index": self._dir_index(root),
                            "rel": self._rel(root, full),
                            "ext": suffix,
                            "kind": KIND_BY_EXT.get(suffix, "file"),
                            "size": int(stat.st_size),
                            "mtime": int(stat.st_mtime),
                            "at": datetime.fromtimestamp(
                                stat.st_mtime, timezone.utc
                            ).replace(microsecond=0).isoformat(),
                        }
                    )
                    if len(files) >= max_files:
                        truncated = True
                        break
                if truncated:
                    break
            if truncated:
                break
        files.sort(key=lambda item: (item["mtime"], item["name"].lower()), reverse=True)
        return {
            "dirs": [str(p) for p in self.dirs()],
            "missing": self.missing_dirs(),
            "files": files,
            "count": len(files),
            "truncated": truncated,
            "errors": errors[:20],
            "scanned_at": iso_now(),
        }

    @staticmethod
    def _walk(root, depth, errors):
        """按层数限制遍历目录，跳过隐藏目录（.git / .trash 之类）。"""

        def onerror(exc):
            errors.append("%s：%s" % (getattr(exc, "filename", "?"), exc.__class__.__name__))

        for current, dirnames, filenames in os.walk(root, onerror=onerror):
            directory = Path(current)
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            try:
                level = len(directory.relative_to(root).parts)
            except ValueError:
                level = 0
            if level >= depth:
                dirnames[:] = []
            yield directory, sorted(filenames)

    def _dir_index(self, root):
        for index, path in enumerate(self.dirs()):
            if _normcase(path) == _normcase(root):
                return index
        return -1

    @staticmethod
    def _rel(root, full):
        try:
            return str(Path(full).relative_to(root))
        except ValueError:
            return str(full)

    # ---- 受控读取 ----
    def resolve(self, path):
        """把前端给的路径限制在配置目录内的 `.txt`/`.epub` 文件上。"""
        if not path:
            raise ValueError("缺少文件路径")
        target = Path(os.path.expanduser(str(path)))
        if target.suffix.lower() not in BOOK_EXTENSIONS:
            raise ValueError("只支持 .txt / .epub 文件")
        try:
            target = target.resolve()
        except OSError as exc:
            raise ValueError("路径无法解析：%s" % exc.__class__.__name__) from exc
        if not target.is_file():
            raise ValueError("文件不存在：%s" % target)
        allowed = self.dirs()
        if not allowed:
            raise ValueError("还没有配置本地书架目录")
        if not any(is_inside(target, root) for root in allowed):
            raise ValueError("文件不在配置的本地书架目录里")
        return target

    def preview(self, path, max_bytes=PREVIEW_MAX_BYTES):
        """读取 .txt 的前若干字节，给页面做「试读」。"""
        target = self.resolve(path)
        if target.suffix.lower() != ".txt":
            raise ValueError("只有 .txt 能试读，EPUB 请直接下载")
        size = target.stat().st_size
        with open(target, "rb") as fh:
            raw = fh.read(max_bytes)
        return {
            "name": target.name,
            "path": str(target),
            "size": size,
            "truncated": size > len(raw),
            "text": raw.decode("utf-8", errors="replace"),
        }
