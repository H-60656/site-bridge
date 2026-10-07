"""个人阅读历史：一行一条 JSON（JSON Lines）追加在 data 目录下的文件里。

格式说明见 `jsonl.py`（存放理由、坏行处理、自动压缩都在那里）。
这里只负责阅读记录本身的字段与折叠主键：每本小说只留最新一条。
"""

from pixiv_bridge.jsonl import (
    COMPACT_AT_LINES,
    DEFAULT_LIMIT,
    MAX_LINE_BYTES,
    JsonlStore,
    as_int,
    as_ratio,
    as_text,
    iso_now,
)

__all__ = [
    "COMPACT_AT_LINES",
    "DEFAULT_LIMIT",
    "MAX_LINE_BYTES",
    "HistoryStore",
    "normalize_entry",
]


def normalize_entry(raw, now=None):
    """把前端发来的一条阅读记录整理成固定字段（多余字段丢弃）。"""
    if not isinstance(raw, dict):
        raise ValueError("阅读记录必须是一个 JSON 对象")
    novel_id = as_int(raw.get("id", raw.get("novel_id")))
    if novel_id is None:
        raise ValueError("阅读记录缺少小说 id")
    return {
        "id": novel_id,
        "title": as_text(raw.get("title"), 200),
        "user": as_text(raw.get("user"), 120),
        "series_id": as_int(raw.get("series_id")),
        "series_title": as_text(raw.get("series_title"), 200),
        "chapter_index": as_int(raw.get("chapter_index")),
        "chapters": as_int(raw.get("chapters")),
        "progress": as_ratio(raw.get("progress")),
        "read_at": as_text(raw.get("read_at"), 40).strip() or (now or iso_now()),
    }


class HistoryStore(JsonlStore):
    """追加写 + 读取时折叠的个人阅读历史文件。"""

    label = "阅读历史"
    time_field = "read_at"

    def normalize(self, raw, now=None):
        return normalize_entry(raw, now=now)

    def identity(self, entry):
        novel_id = as_int(entry.get("id"))
        if novel_id is None:
            raise ValueError("阅读记录缺少小说 id")
        return str(novel_id)

    def is_entry(self, item):
        return isinstance(item, dict) and as_int(item.get("id")) is not None
