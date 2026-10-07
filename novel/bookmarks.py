"""收藏台账：一行一条 JSON，本地记下「我收藏了哪些小说」。

为什么本地还要记一份：
- pixiv 的 app-api 只有收藏的「写」接口（`/novel/bookmark/add|delete`），
  没有能列出「我收藏的小说」的读接口，页面拿不到收藏列表。
- 所以写的时候两边都写：上游负责让 pixiv 账号里的收藏状态正确，本地这份
  负责让页面随时能列出收藏、并且离线/上游抖动时也不丢。
- 取消收藏写一条墓碑记录（`deleted: true`），折叠之后就不在列表里了，
  历史仍然留痕，不会因为一次误操作把记录抹掉。
"""

from pixiv_bridge.jsonl import JsonlStore, as_int, as_text, iso_now

__all__ = ["BookmarkStore", "normalize_bookmark"]


def normalize_bookmark(raw, now=None):
    """把前端发来的一条收藏记录整理成固定字段（多余字段丢弃）。"""
    if not isinstance(raw, dict):
        raise ValueError("收藏记录必须是一个 JSON 对象")
    novel_id = as_int(raw.get("id", raw.get("novel_id")))
    if novel_id is None:
        raise ValueError("收藏记录缺少小说 id")
    entry = {
        "id": novel_id,
        "title": as_text(raw.get("title"), 200),
        "user": as_text(raw.get("user"), 120),
        "user_id": as_int(raw.get("user_id")),
        "series_id": as_int(raw.get("series_id")),
        "series_title": as_text(raw.get("series_title"), 200),
        "restrict": "private" if str(raw.get("restrict") or "").lower() == "private" else "public",
        "at": as_text(raw.get("at"), 40).strip() or (now or iso_now()),
    }
    if raw.get("deleted"):
        entry["deleted"] = True
    return entry


class BookmarkStore(JsonlStore):
    """追加写 + 读取时折叠的收藏台账。"""

    label = "收藏台账"
    time_field = "at"

    def normalize(self, raw, now=None):
        return normalize_bookmark(raw, now=now)

    def identity(self, entry):
        novel_id = as_int(entry.get("id"))
        if novel_id is None:
            raise ValueError("收藏记录缺少小说 id")
        return str(novel_id)

    def is_entry(self, item):
        return isinstance(item, dict) and as_int(item.get("id")) is not None

    def keep(self, entry):
        """墓碑记录（deleted）折叠后不出现，也就是「已取消收藏」。"""
        return not entry.get("deleted")

    def has(self, novel_id):
        novel_id = as_int(novel_id)
        for entry in self.entries(0):
            if as_int(entry.get("id")) == novel_id:
                return True
        return False

    def remove(self, novel_id, now=None):
        """取消收藏：写一条墓碑记录。"""
        novel_id = as_int(novel_id)
        if novel_id is None:
            raise ValueError("收藏记录缺少小说 id")
        return self.append({"id": novel_id, "deleted": True, "at": now or iso_now()})
