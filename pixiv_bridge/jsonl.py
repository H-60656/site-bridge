"""个人数据的通用存放方式：一行一条 JSON（JSON Lines）。

为什么统一用这个格式（格式决定权在用户手里，这里记录理由）：
- 阅读进度、下载记录这类数据会频繁追加，追加一行不重写整个文件：进程被中断
  最多丢掉最后一行，不像整份 JSON 那样会把文件写坏；并发写也只是多一行。
- 纯文本、一行一条，可以直接 `tail` / `grep` 人工查看和手工修。
- 读取时按业务主键折叠成「每个东西一条最新记录」，前端拿到的就是最新状态；
  需要「删除」时写一条墓碑记录（`deleted: true`），不用改写历史。
- 行数超过阈值时压成「每个主键一条」，避免长期使用后无限增长。
- 只有个人使用，不做用户/项目区分，条目里不带任何账号字段。
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

# 单行上限：正常一条记录几百字节，超过这个长度说明写入的不是本程序的内容，直接跳过。
MAX_LINE_BYTES = 64 * 1024
DEFAULT_LIMIT = 200
# 超过这么多行就把文件压成「每个主键一条」，下次启动也不会一次读很久。
COMPACT_AT_LINES = 2000


def iso_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_ratio(value):
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return None
    if ratio < 0:
        return 0.0
    if ratio > 1:
        return 1.0
    return round(ratio, 4)


def as_text(value, limit=200):
    return str(value or "")[:limit]


class JsonlStore:
    """追加写 + 读取时折叠的 JSON Lines 文件。子类给出主键与归一化规则。"""

    label = "记录"
    # 折叠排序用的时间字段名。
    time_field = "at"

    def __init__(self, path, compact_at=COMPACT_AT_LINES):
        self.path = Path(path)
        # 早失败早发现：配置写错时（比如路径解析成了项目目录本身）给出明确信息，
        # 而不是在读文件时抛一个看不懂的 PermissionError。
        if self.path.is_dir():
            raise ValueError(f"{self.label}路径指向一个目录，应该是文件：{self.path}")
        self.compact_at = compact_at
        self._lock = threading.Lock()

    # ---- 子类实现 ----
    def normalize(self, raw, now=None):
        """把外部输入整理成固定字段（多余字段丢弃）；缺主键时抛 ValueError。"""
        raise NotImplementedError

    def identity(self, entry):
        """折叠、删除用的业务主键（字符串）；缺主键时抛 ValueError。"""
        raise NotImplementedError

    def is_entry(self, item):
        """读回一行时判断是不是本文件的合法记录。"""
        if not isinstance(item, dict):
            return False
        try:
            return self.identity(item) is not None
        except ValueError:
            return False

    def keep(self, entry):
        """折叠之后是否保留（默认全留；收藏用墓碑记录表示删除）。"""
        return True

    def sort_key(self, entry):
        return str(entry.get(self.time_field) or "")

    # ---- 写 ----
    def append(self, entry):
        """把一条记录追加到文件末尾，返回写入的记录。"""
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            if self.compact_at and self._line_count_locked() > self.compact_at:
                self._compact_locked()
        return entry

    def record(self, raw, now=None):
        return self.append(self.normalize(raw, now=now))

    # ---- 读 ----
    def _read_all_locked(self):
        """读回所有合法记录（按写入顺序），跳过坏行，返回 (记录列表, 坏行数)。"""
        if not self.path.exists():
            return [], 0
        entries = []
        bad = 0
        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                if len(line) > MAX_LINE_BYTES:
                    bad += 1
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if self.is_entry(item):
                    entries.append(item)
                else:
                    bad += 1
        return entries, bad

    def read_all(self):
        with self._lock:
            return self._read_all_locked()

    def fold(self, items):
        """每个主键只留时间最新的一条，按时间倒序返回。"""
        latest = {}
        for item in items:
            key = self.identity(item)
            previous = latest.get(key)
            if previous is None or self.sort_key(item) >= self.sort_key(previous):
                latest[key] = item
        return sorted(latest.values(), key=self.sort_key, reverse=True)

    def entries(self, limit=DEFAULT_LIMIT):
        items = [it for it in self.fold(self.read_all()[0]) if self.keep(it)]
        if limit and limit > 0:
            items = items[:limit]
        return items

    def status(self, limit=DEFAULT_LIMIT):
        entries, bad = self.read_all()
        kept = [it for it in self.fold(entries) if self.keep(it)]
        return {
            "file": str(self.path),
            "count": len(kept),
            "lines": len(entries),
            "bad_lines": bad,
            "entries": kept[:limit] if limit and limit > 0 else kept,
        }

    # ---- 压缩 ----
    def _line_count_locked(self):
        if not self.path.exists():
            return 0
        with open(self.path, "rb") as fh:
            return sum(1 for _ in fh)

    def _compact_locked(self):
        """把文件重写成「每个主键一条」（按时间正序），返回压掉的行数。"""
        items = sorted(self.fold(self._read_all_locked()[0]), key=self.sort_key)
        before = self._line_count_locked()
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for item in items:
                fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        return before - len(items)

    def compact(self):
        """手动压缩（自检/测试用）。"""
        with self._lock:
            return self._compact_locked()

    def clear(self):
        """清空文件（保留文件本身，方便继续追加）。"""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.flush()
                os.fsync(fh.fileno())
