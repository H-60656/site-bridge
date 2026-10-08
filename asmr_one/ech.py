"""asmr.one 的 ECH 配置（加密 Client Hello）。

和 ``iwara/ech.py`` 是同一套机制、同一份结论，只是**必须自己写一份**：
三个接口项目同级、互不依赖（``scripts/check_boundary.py`` 会拒绝站点 import
别的站点），asmr_one 必须自足。实现路线也一致 —— 把 ECH 配置交给 libcurl 的
``CURLOPT_ECH``，不需要任何动态库（pixiv 那条 ctypes + OpenSSL 4.0+ 的路子
是它自己要带 ``openssl-libs/`` 的原因）。

实测前提（2026-10-08）：

* asmr.one 的 API 与媒体主机（``api.asmr.one`` / ``raw.kiko-play-niptan.one``）
  都在 Cloudflare 后面，证书是 Cloudflare 签的，可以用 ECH。
* 这些主机的 HTTPS(type 65) 记录里**没有** ``ech=`` 字段，必须用 Cloudflare
  那份 zone 无关的公钥（``cloudflare-ech.com``），对 CF 上所有站点通用。
* 更关键的是：asmr.one **自己那几个**边缘 IP 在本机是 IP 级被阻断的
  （``104.26.4.171`` / ``172.67.70.244`` 一律 RST）。能通的做法是把请求钉到
  **Cloudflare 别的 anycast IP** 上（同一批 IP 换着试），再叠 ECH。
  不带 ECH 时那批 IP 也全灭 —— 所以这个站点实际上**依赖** ECH。

取配置的顺序：显式 override → DoH（type 65）→ 内置兜底常量。
"""

from __future__ import annotations

import base64
import re
import threading
import time

import httpx

try:  # curl_cffi 缺了就不是 ECH 的事，是依赖没装（asmr_one/requirements.txt）
    from curl_cffi.const import CurlOpt
except Exception:  # pragma: no cover - 依赖缺失由 envcheck 的 deps 项报告
    CurlOpt = None

ECH_PUBLIC_NAME = "cloudflare-ech.com"
# cloudflare-ech.com / crypto.cloudflare.com 的 HTTPS(type 65) 记录里公开的
# ECH 配置，对 Cloudflare 上所有站点通用（内层 SNI 不限）。DoH 取不到时用
# 它兜底——Cloudflare 会轮换密钥，拿旧配置的表现是握手失败。
DEFAULT_ECH_CONFIG = (
    "AEX+DQBB8AAgACBQtB3/bF2MkiwRKDn2FbKQD5Xovg4XyRyecJV8EQBgHQAEAAEAAQASY2xvdWRm"
    "bGFyZS1lY2guY29tAAA="
)
ECH_CONFIG_NAMES = ("cloudflare-ech.com", "crypto.cloudflare.com")
# 与 iwara 那组同因：查 type 65 只有这两个端点实测可用（doh.pub/dns-query 与
# cloudflare-dns.com 分别返回 400 / 被 RST）。
DOH_ENDPOINTS = (
    "https://dns.alidns.com/resolve",
    "https://223.5.5.5/resolve",
)
# 记录形如 ... ech=\"AEX+DQBB...\" ...：DoH 的 JSON 里引号被转义过，所以这里
# 必须允许反斜杠，否则匹配不到、永远退回内置配置。
_ECH_VALUE_RE = re.compile(r'ech\s*=\s*\\?"?([A-Za-z0-9+/=_-]+)')
# Cloudflare 轮换 ECH 密钥的周期是小时级，缓存半小时足够，也避免每次请求
# 都去打一次 DoH。
ECH_CONFIG_TTL = 1800.0
# CURLOPT_ECH 的取值语义：false|true|grease|hard|ecl:<base64>|pn:<public_name>。
# 只有 ecl: 才是「带上这份配置」，true/grease 只是开关或填充。
ECH_OPTION_PREFIX = "ecl:"


class EchUnavailable(Exception):
    """ECH 不可用（依赖缺失、配置非法或 DoH 全挂）。"""


def available() -> bool:
    """libcurl 是否支持 ECH（``CURLOPT_ECH`` 存在即编译进来了）。"""
    return getattr(CurlOpt, "ECH", None) is not None


def _decode_config(value: str) -> bytes:
    lines = [line.strip() for line in str(value).splitlines() if line.strip()]
    text = "".join(line for line in lines if not line.startswith("-----"))
    text = "".join(text.split())
    try:
        raw = base64.b64decode(text, validate=True)
    except Exception as exc:
        raise EchUnavailable(f"ECH 配置不是合法 base64: {exc}") from exc
    if len(raw) < 4:
        raise EchUnavailable("ECH 配置过短")
    return raw


def _ech_from_doh(payload) -> bytes | None:
    """从 DoH JSON 里取出 HTTPS(type 65) 记录的 ech= 值并解码。"""
    answers = (payload or {}).get("Answer") or []
    for answer in answers:
        if answer.get("type") != 65:
            continue
        match = _ECH_VALUE_RE.search(answer.get("data") or "")
        if not match:
            continue
        try:
            return _decode_config(match.group(1))
        except EchUnavailable:
            continue
    return None


def fetch_ech_config(name=ECH_PUBLIC_NAME, timeout=5.0, endpoints=None) -> bytes | None:
    """用 DoH 查 HTTPS 记录里的 ech=（本机 DNS 不可信，必须走 DoH）。

    依次尝试 ``DOH_ENDPOINTS``，全部失败时抛 ``EchUnavailable``（调用方决定
    是否兜底到内置常量）。
    """
    errors = []
    for endpoint in endpoints or DOH_ENDPOINTS:
        try:
            resp = httpx.get(
                endpoint,
                params={"name": name, "type": "65"},
                headers={"accept": "application/dns-json"},
                timeout=timeout,
            )
            resp.raise_for_status()
            found = _ech_from_doh(resp.json())
        except Exception as exc:  # noqa: BLE001 - 任一端点失败就换下一个
            errors.append(f"{endpoint}: {exc.__class__.__name__}")
            continue
        if found:
            return found
    if errors:
        raise EchUnavailable("DoH 查询失败（" + "; ".join(errors) + "）")
    return None


_config_lock = threading.Lock()
_config_cache: bytes | None = None
_config_cached_at = 0.0


def reset_config_cache() -> None:
    """丢掉缓存的 ECH 配置，下次取用时重新走 DoH。"""
    global _config_cache, _config_cached_at
    with _config_lock:
        _config_cache = None
        _config_cached_at = 0.0


def _cache_fresh() -> bool:
    return (
        _config_cache is not None
        and (time.monotonic() - _config_cached_at) < ECH_CONFIG_TTL
    )


def ech_config(override="", refresh=False) -> bytes:
    """返回 ECHConfigList 原始字节：显式配置 > DoH 获取 > 内置常量。"""
    if override:
        return _decode_config(override)
    global _config_cache, _config_cached_at
    with _config_lock:
        if refresh or not _cache_fresh():
            for name in ECH_CONFIG_NAMES:
                try:
                    found = fetch_ech_config(name)
                except Exception:  # noqa: BLE001 - 单个名字失败就换下一个
                    found = None
                if found:
                    _config_cache = found
                    break
            else:
                _config_cache = _decode_config(DEFAULT_ECH_CONFIG)
            _config_cached_at = time.monotonic()
        return _config_cache


def ech_option(raw: bytes | None = None, override="", refresh=False) -> str | None:
    """给 libcurl 的 ``CURLOPT_ECH`` 取值（``ecl:<base64>``）。

    返回 ``None`` 表示这次不该开 ECH（依赖缺失或配置拿不到），调用方把它
    从 curl_options 里去掉即可。
    """
    if not available():
        return None
    try:
        config = raw if raw is not None else ech_config(override, refresh=refresh)
    except EchUnavailable:
        return None
    if not config:
        return None
    return ECH_OPTION_PREFIX + base64.b64encode(config).decode("ascii")
