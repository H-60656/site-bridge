"""github 接口站点 —— GitHub 的只读 JSON 接口。

和 pixiv / iwara / asmr_one / ehviewer 是**同级**的接口项目：只依赖框架
``site_bridge``，不认识别的站点，别的站点也不认识它。它比那四个都省事 ——
``api.github.com`` 在本机直连就通，不需要 ECH、不需要借道 Cloudflare、不需要
HTTP/3，所以只用根清单里的 ``httpx``，没有单独的 ``requirements.txt``。

真正的约束只有配额：未认证 60 次/小时（search 单独 10 次/小时），配
``GITHUB_TOKEN`` 提高到 5000 次/小时。入口：``python github/run.py``。
"""

from site_bridge import __version__

__all__ = ["__version__"]
