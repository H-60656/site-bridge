"""Site Bridge 的 Python 调用示例 —— 以 pixiv 站点为例（同步 httpx 客户端 + 一个命令行 demo）。
"""

import argparse
from urllib.parse import quote

import httpx


class PixivBridge:
    def __init__(self, base="http://127.0.0.1:8765", timeout=30.0):
        self._client = httpx.Client(base_url=base, timeout=timeout)

    def get(self, path, **params):
        query = {k: v for k, v in params.items() if v is not None}
        resp = self._client.get(path, params=query)
        resp.raise_for_status()
        return resp.json()

    def post(self, path, json=None):
        resp = self._client.post(path, json=json or {})
        resp.raise_for_status()
        return resp.json()

    def health(self):
        return self.get("/api/health")

    def auth_status(self):
        return self.get("/api/auth/status")

    def illust(self, illust_id):
        return self.get(f"/api/illust/{illust_id}")

    def ranking(self, mode="day", date=None, next_url=None):
        return self.get("/api/ranking", mode=mode, date=date, next_url=next_url)

    def search_illust(self, word, **kwargs):
        return self.get("/api/search/illust", word=word, **kwargs)

    def user(self, user_id):
        return self.get(f"/api/user/{user_id}")

    def user_illusts(self, user_id, type="illust", next_url=None):
        return self.get(
            f"/api/user/{user_id}/illusts", type=type, next_url=next_url
        )

    def next(self, next_url):
        return self.get("/api/next", url=next_url)

    def image_url(self, path_or_url):
        base = str(self._client.base_url).rstrip("/")
        if path_or_url.startswith("http"):
            return f"{base}/img?url={quote(path_or_url, safe='')}"
        return f"{base}/img/{path_or_url.lstrip('/')}"


def demo(base):
    client = PixivBridge(base)
    status = client.auth_status()
    print(f"认证状态: {status}")
    ranking = client.ranking("day")
    for item in ranking.get("illusts", [])[:3]:
        print(f"- {item['title']} by {item['user']['name']} (id={item['id']})")
    if ranking.get("next_url"):
        page2 = client.next(ranking["next_url"])
        print(f"下一页: {len(page2.get('illusts', []))} 条")


def main():
    parser = argparse.ArgumentParser(description="Site Bridge Python 客户端示例（pixiv 站点）")
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    demo(args.base)


if __name__ == "__main__":
    main()
