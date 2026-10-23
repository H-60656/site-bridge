"""下载历史：一行一条 JSON，按「书 + 章节」的细致度记录下载过什么。

为什么按章节记：
- 系列作品是一章一章下载的，只有章节粒度才能回答「这本里我还有哪章没下过」；
  前端据此给每个章节打上「已下载」，下载时可以只补缺的章节。
- 同一个系列名下出现两个不同的「第一章」时，它们是两条独立记录：主键是
  `book_id + chapter_index`（不是章节标题），所以不会互相覆盖或去重。
- 重复下载同一章只追加一行，读取时折叠成最新一条（记的是最新一次导出的文件与大小）。
"""

from site_bridge.jsonl import DEFAULT_LIMIT, JsonlStore, as_int, as_text, iso_now

__all__ = ["DownloadStore", "normalize_download"]

# 目前 EPUB 是唯一的导出格式，旧的 txt 记录仍然读得回来（format 字段保留）。
FORMATS = ("epub", "txt")


def normalize_download(raw, now=None):
    """把前端发来的一条下载记录整理成固定字段（多余字段丢弃）。"""
    if not isinstance(raw, dict):
        raise ValueError("下载记录必须是一个 JSON 对象")
    book_id = as_int(raw.get("book_id", raw.get("id", raw.get("novel_id"))))
    if book_id is None:
        raise ValueError("下载记录缺少小说 id")
    fmt = str(raw.get("format") or "").lower()
    return {
        "book_id": book_id,
        # 单篇作品没有章号，统一记 0，主键才不会退化成空值。
        "chapter_index": as_int(raw.get("chapter_index")) or 0,
        "chapter_id": as_int(raw.get("chapter_id")) or book_id,
        "chapter_title": as_text(raw.get("chapter_title"), 200),
        "title": as_text(raw.get("title"), 200),
        "user": as_text(raw.get("user"), 120),
        "series_id": as_int(raw.get("series_id")),
        "series_title": as_text(raw.get("series_title"), 200),
        # 合并同一个系列名的分组键（前端算好，方便直接按名字查）。
        "merge_key": as_text(raw.get("merge_key"), 200),
        "format": "epub" if fmt == "epub" else "txt",
        "file": as_text(raw.get("file"), 260),
        "bytes": as_int(raw.get("bytes")) or 0,
        "chapters": as_int(raw.get("chapters")),
        "at": as_text(raw.get("at"), 40).strip() or (now or iso_now()),
    }


class DownloadStore(JsonlStore):
    """追加写 + 读取时折叠的下载历史文件。"""

    label = "下载历史"
    time_field = "at"

    def normalize(self, raw, now=None):
        return normalize_download(raw, now=now)

    def identity(self, entry):
        book_id = as_int(entry.get("book_id"))
        if book_id is None:
            raise ValueError("下载记录缺少小说 id")
        return "%d#%d" % (book_id, as_int(entry.get("chapter_index")) or 0)

    def is_entry(self, item):
        return isinstance(item, dict) and as_int(item.get("book_id")) is not None

    # ---- 查询辅助 ----
    def filter(self, limit=DEFAULT_LIMIT, series_id=None, book_id=None):
        items = self.entries(limit)
        if series_id is not None:
            series_id = as_int(series_id)
            items = [it for it in items if as_int(it.get("series_id")) == series_id]
        if book_id is not None:
            book_id = as_int(book_id)
            items = [it for it in items if as_int(it.get("book_id")) == book_id]
        return items

    def series_progress(self, series_id):
        """某个系列已下载的章节：{章节序号: {at, file, bytes, format}}。"""
        result = {}
        for item in self.filter(0, series_id=series_id):
            index = as_int(item.get("chapter_index")) or 0
            result[str(index)] = {
                "at": as_text(item.get("at"), 40),
                "file": as_text(item.get("file"), 260),
                "bytes": as_int(item.get("bytes")) or 0,
                "format": as_text(item.get("format"), 10),
            }
        return result

    def status(self, limit=DEFAULT_LIMIT, series_id=None, book_id=None):
        data = super().status(0)
        items = self.filter(limit, series_id=series_id, book_id=book_id)
        data["entries"] = items
        data["count"] = len(self.entries(0))
        data["held"] = len(items)
        return data
