"""ECH（Encrypted Client Hello）直连传输层。

当前链路对 pixiv 全域做 SNI 阻断：TLS 握手里只要明文出现 pixiv 域名，连接就会被重置
（ConnectionResetError）；而不带 SNI 时服务端只能按 Host 头兜底，返回 403/421，API 域名
因此完全不可用。TCP 分段、TLS record 分片都无法绕过。

Cloudflare 的 ECH 公钥是 zone 无关的：把内层 SNI 设为目标域名、外层 SNI 换成公开配置里的
public_name（cloudflare-ech.com），边缘节点解密 ClientHello 后按内层域名路由到 pixiv 源站。

**本机到 Cloudflare 这一段会间歇性阻断 ECH 本身**（2026-10 实测，只与本地网络/运营商有关，
与服务端无关——Cloudflare 自己的 ECH 支持一直正常，换个网络就能直连）：只要 ClientHello 里带一个
*结构合法*的 ECH 扩展（0xfe0d），无论配置是否有效、外层 SNI 是什么、连哪个 Cloudflare IP，连接都会在
ClientHello 发出后约 0.2s 被 RST（SSL_get_error=5 = SSL_ERROR_SYSCALL）；而把配置换成
OpenSSL 不接受的形状（不发扩展）或干脆不带 ECH，握手就能正常完成。服务端在解不开 ECH 时
本应回落到外层 ClientHello，所以这个重置来自链路中间盒，而不是 Cloudflare——**这种情况下
客户端无法绕过**，只能换网络/等它恢复，或改用代理（`SITE_PROXY`，见 config.Settings.proxy）。

Python 自带的 OpenSSL 3.0.x 没有 ECH API，这里用 ctypes 驱动 OpenSSL **4.0+** 的 libssl
（ECH 是 OpenSSL 4.0.0 才加入的能力，3.x 全系都没有，包括 3.5/3.6——所以「回退系统自带
OpenSSL」在只有 3.x 的机器上一定失败，必须用项目自带的 `openssl-libs/`）。两个实测约束：
  * 必须只协商 TLS 1.3：同时提供 TLS 1.2 时 Cloudflare 会以 illegal_parameter 中止握手；
  * fd 必须保持阻塞：socket.create_connection 的 timeout 会把 fd 设成非阻塞，导致
    SSL_connect 返回 SSL_ERROR_WANT_READ。
"""

import asyncio
import base64
import ctypes
import ctypes.util
import os
import platform
import re
import shutil
import socket
import struct
import sys
import sysconfig
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

# 复用无 SNI 客户端里已经过测试的报文构造与响应解析（只在进程内使用）。
from .sni_client import _build_request, _read_response

ECH_PUBLIC_NAME = "cloudflare-ech.com"
# cloudflare-ech.com / crypto.cloudflare.com 的 HTTPS(type 65) 记录里公开的 ECH 配置，
# 对 Cloudflare 上所有站点通用（内层 SNI 不限），作为 DoH 取不到时的兜底。
DEFAULT_ECH_CONFIG = (
    "AEX+DQBB8AAgACBQtB3/bF2MkiwRKDn2FbKQD5Xovg4XyRyecJV8EQBgHQAEAAEAAQASY2xvdWRm"
    "bGFyZS1lY2guY29tAAA="
)
ECH_CONFIG_NAMES = ("cloudflare-ech.com", "crypto.cloudflare.com")
# DoH 端点按顺序尝试：Cloudflare 自己的 DoH 在部分网络被阻断（RST/超时），
# AliDNS / 223.5.5.5 通常可达，返回同为 Google DoH JSON 形状。
DOH_ENDPOINTS = (
    "https://cloudflare-dns.com/dns-query",
    "https://dns.alidns.com/resolve",
    "https://223.5.5.5/resolve",
)
DOH_ENDPOINT = DOH_ENDPOINTS[0]  # 兼容旧引用
# 真实记录形如 ... ech="AEX+DQBB..." ...；旧实现按空格分词再 field[4:]，
# 引号被替换成空格后拿到的是空串，于是 DoH 永远解析失败、一直用过期内置配置。
_ECH_VALUE_RE = re.compile(r"ech\s*=\s*\"?([A-Za-z0-9+/=_-]+)\"?")
# Cloudflare 会轮换 ECH 密钥；缓存太久可能拿着过期配置（表现为证书/握手失败）。
ECH_CONFIG_TTL = 1800.0

SSL_CTRL_SET_TLSEXT_HOSTNAME = 55
SSL_CTRL_SET_MIN_PROTO_VERSION = 123
SSL_CTRL_SET_MAX_PROTO_VERSION = 124
TLS1_3_VERSION = 0x0304
SSL_VERIFY_PEER = 1
SSL_ERROR_WANT_READ = 2
SSL_ERROR_WANT_WRITE = 3
SSL_ERROR_SYSCALL = 5
SSL_ERROR_ZERO_RETURN = 6
TLS_EXT_SERVERNAME = 0
READ_CHUNK = 65536
# Cloudflare anycast 地址的 TCP 建连实测不稳定（104.18.10.118 8/8、104.18.11.118 6/8、
# 104.18.42.239 5/8、172.64.145.17 3/8 成功），所以建连超时压到 5s，失败就换下一个 IP。
CONNECT_TIMEOUT = 5.0

# 项目自带的 OpenSSL 动态库目录（与 site_bridge/ 同级，随项目一起拷贝）。
# 这样在没有装 OpenSSL 4.0+ 的机器上也能直接开 ECH，不必让用户先去装 OpenSSL。
# 里面的库按 平台-架构 分子目录存放，见 _platform_tags()。
# 当前已带：win32-amd64（4.0.2）、android-arm64（4.0.3，给 Termux 用）。
BUNDLE_DIR_NAME = "openssl-libs"

_LIBSSL_NAMES = (
    "libssl-4-x64.dll",
    "libssl-4.dll",
    "libssl-3-x64.dll",
    "libssl-3.dll",
    "libssl.so.4",
    "libssl.so.3",
    "libssl.so",
    "libssl.4.dylib",
    "libssl.3.dylib",
    "libssl.dylib",
)
_LIBCRYPTO_NAMES = (
    "libcrypto-4-x64.dll",
    "libcrypto-4.dll",
    "libcrypto-3-x64.dll",
    "libcrypto-3.dll",
    "libcrypto.so.4",
    "libcrypto.so.3",
    "libcrypto.so",
    "libcrypto.4.dylib",
    "libcrypto.3.dylib",
    "libcrypto.dylib",
)


class EchUnavailable(RuntimeError):
    """本机缺少带 ECH 支持的 OpenSSL，或 ECH 配置不可用。"""


class EchConnectError(RuntimeError):
    """ECH 握手/传输失败。"""


def bundle_root():
    """项目自带的 OpenSSL 目录（与 ``site_bridge/`` 同级，随项目一起拷贝）。"""
    return Path(__file__).resolve().parent.parent / BUNDLE_DIR_NAME


def termux_prefix():
    """Termux（Android）的安装前缀，如 ``/data/data/com.termux/files/usr``。

    Termux 的 Python 报告的 ``sys.platform`` 是 ``linux``，和普通 Linux 分不出来，
    但它的 OpenSSL 是 bionic libc 版的、放在 ``$PREFIX/lib`` 下，且**不能**加载
    glibc 的 Linux 版库。所以这里单独认一下，好把库目录和自带子目录都指对。
    """
    prefix = os.environ.get("PREFIX", "")
    if "com.termux" in prefix:
        return prefix
    if is_termux():
        return prefix or "/data/data/com.termux/files/usr"
    return ""


def is_termux():
    """是否运行在 Termux（Android）里。

    判定逻辑与 ``deps.is_termux()`` 同源，避免两处标准不一致（那里据此挑依赖清单）。
    """
    from site_bridge import deps

    return deps.is_termux()


def _system_name():
    """``openssl-libs/`` 里用的系统名。

    只对 Windows 做归并（``cygwin`` 也当 ``win32``，因为 Cygwin 用的是 Win32 版 dll），
    其余取 ``sys.platform`` 的字母前缀，这样 ``freebsd13`` → ``freebsd``、
    ``linux`` → ``linux``、``darwin`` → ``darwin``，不会把 BSD 误当成 Linux。
    Termux 单列成 ``android``：它的库是 bionic 版，跟 glibc 的 Linux 版不通用，
    两者混在一起会加载到不能用的那一份。
    """
    if is_termux():
        return "android"
    if sys.platform in ("win32", "cygwin"):
        return "win32"
    match = re.match(r"[a-z]+", sys.platform or "")
    return match.group(0) if match else (sys.platform or "unknown")


def _multiarch_triple():
    """Debian/Ubuntu 的多架构三元组，例如 ``x86_64-linux-gnu``、``aarch64-linux-gnu``。

    Python 自己带着这个变量（Debian 打补丁的 CPython 会设置 ``MULTIARCH``），拿不到
    就按 ``platform.machine()`` 推一个最常见的形状。
    """
    try:
        value = sysconfig.get_config_var("MULTIARCH")
    except Exception:  # pragma: no cover - sysconfig 极少出问题
        value = None
    if value:
        return value
    machine = (platform.machine() or "").lower()
    guess = {
        "x86_64": "x86_64-linux-gnu",
        "amd64": "x86_64-linux-gnu",
        "aarch64": "aarch64-linux-gnu",
        "arm64": "aarch64-linux-gnu",
        "armv7l": "arm-linux-gnueabihf",
        "armv6l": "arm-linux-gnueabihf",
        "i386": "i386-linux-gnu",
        "i686": "i386-linux-gnu",
        "riscv64": "riscv64-linux-gnu",
        "ppc64le": "powerpc64le-linux-gnu",
        "s390x": "s390x-linux-gnu",
    }.get(machine)
    return guess or ""


def _linux_library_dirs():
    """Linux 上要找的库目录，按「本机专属 → 通用」排序。

    不止 ``/usr/lib/x86_64-linux-gnu`` 一个：Fedora/RHEL 用 ``/usr/lib64``，
    Ubuntu/Debian 的 ARM 板子用 ``/usr/lib/aarch64-linux-gnu`` 或
    ``/usr/lib/arm-linux-gnueabihf``，得按当前架构挑，否则非 x86_64 的机器
    会漏掉系统那份 OpenSSL。
    """
    triple = _multiarch_triple()
    machine = (platform.machine() or "").lower()
    arch = {"amd64": "x86_64", "arm64": "aarch64", "i386": "i686", "i686": "i686"}.get(
        machine, machine
    )
    dirs = []
    for candidate in (
        f"/usr/lib/{triple}" if triple else "",
        f"/lib/{triple}" if triple else "",
        f"/usr/lib/{arch}-linux-gnu",
        f"/usr/lib/{machine}-linux-gnu",
        "/usr/lib64",
        "/usr/local/lib64",
        "/usr/local/lib",
        f"/usr/lib/{arch}",
        "/usr/lib",
        "/lib64",
        "/lib",
    ):
        if candidate and candidate not in dirs:
            dirs.append(candidate)
    return dirs


def _macos_library_dirs():
    """macOS 上要找的库目录：Homebrew 的 openssl 单独放在 opt/ 下，不在 lib/ 里。

    优先 openssl@4（只有 4.0+ 才有 ECH），也保留 openssl@3 —— 后者留着是为了
    让「装了 3.x 但没有 ECH」这种情况能被找到并给出准确的失败原因，而不是静默跳过。
    """
    dirs = []
    for prefix in ("/opt/homebrew", "/usr/local"):  # Apple Silicon / Intel
        for candidate in (
            f"{prefix}/opt/openssl@4/lib",
            f"{prefix}/opt/openssl@3/lib",
            f"{prefix}/opt/openssl/lib",
            f"{prefix}/lib",
        ):
            if candidate not in dirs:
                dirs.append(candidate)
    return dirs


def _platform_tags():
    """当前平台在 ``openssl-libs/`` 下的候选子目录名（先精确、后宽松）。

    约定布局：``openssl-libs/<系统>-<架构>/``，例如 ``win32-amd64``、
    ``linux-x86_64``、``darwin-arm64``；直接把库文件丢在 ``openssl-libs/``
    根下也能识别。
    """
    system = _system_name()
    machine = (platform.machine() or "").lower()
    arch = {
        "amd64": "amd64",
        "x86_64": "x86_64",
        "x64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
        "i386": "x86",
        "i686": "x86",
    }.get(machine, machine)
    tags = []
    for item in (f"{system}-{arch}", f"{system}-{machine}", system):
        if item and item not in tags:
            tags.append(item)
    return tuple(tags)


def _candidate_dirs():
    """按优先级返回要搜索 libssl/libcrypto 的目录。

    顺序 = 显式覆盖（``PIXIV_OPENSSL_DIR``）→ **项目自带** → 系统安装。
    项目自带的那份排在系统前面，这样拷到一台没装 OpenSSL 的机器上也能直接开 ECH；
    显式指定的目录仍放最前，是因为那是用户明确点名的位置，不该被内置那份悄悄顶掉。
    """
    dirs = []

    def add(path):
        if not path:
            return
        try:
            candidate = Path(path)
        except (TypeError, ValueError):
            return
        if candidate.is_dir() and candidate not in dirs:
            dirs.append(candidate)

    # 1) 显式指定的目录（用户手动覆盖）。
    add(os.environ.get("PIXIV_OPENSSL_DIR"))
    # 2) 项目自带（首选：不依赖目标机器装 OpenSSL 4.0+）。
    bundle = bundle_root()
    for tag in _platform_tags():
        add(bundle / tag)
    add(bundle)
    # 3) 系统安装：openssl 可执行文件旁边 → 常见安装路径 → PATH → 解释器目录。
    exe = shutil.which("openssl")
    if exe:
        add(Path(exe).resolve().parent)
    if sys.platform == "win32":
        add(r"C:\Program Files\OpenSSL-Win64\bin")
        add(r"C:\Program Files\OpenSSL\bin")
        add(r"C:\Program Files (x86)\OpenSSL-Win32\bin")
    # dlsym 探测：有些系统把库放在非标准位置（Homebrew keg-only、conda、Nix），
    # find_library 能问出真实路径，比硬编码清单更可靠。
    for probe in ("ssl", "crypto"):
        try:
            found = ctypes.util.find_library(probe)
        except Exception:  # pragma: no cover - 探测失败不影响其它候选
            continue
        if found and os.path.sep in found:
            add(Path(found).resolve().parent)
    add(Path(sys.prefix) / "bin")
    add(Path(sys.prefix) / "lib")
    add(Path(sys.prefix) / "DLLs")
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        add(entry)
    prefix = termux_prefix()
    if prefix:
        # Termux：库在 $PREFIX/lib，是 bionic 版，**不能**套用 glibc 的 Linux 清单
        # （/usr/lib/x86_64-linux-gnu 之类在安卓上根本不存在，混进去没意义）。
        add(Path(prefix) / "lib")
        add(Path(prefix) / "libexec")
    elif sys.platform == "darwin":
        for entry in _macos_library_dirs():
            add(entry)
    elif sys.platform.startswith("linux"):
        for entry in _linux_library_dirs():
            add(entry)
    elif sys.platform.startswith(("freebsd", "openbsd", "netbsd", "dragonfly")):
        # BSD 的 ports 装在 /usr/local 下，顺序与 Linux 不同，别套 Linux 的清单。
        for entry in (
            "/usr/local/lib",
            "/usr/local/lib64",
            "/usr/lib",
            "/lib",
        ):
            add(entry)
    return dirs


def _library_pairs():
    """按优先级产出 (libcrypto, libssl) 候选路径，逐个尝试而不是只认第一个。"""
    for directory in _candidate_dirs():
        crypto = next(
            (directory / name for name in _LIBCRYPTO_NAMES if (directory / name).is_file()),
            None,
        )
        if crypto is None:
            continue
        ssl_path = next(
            (directory / name for name in _LIBSSL_NAMES if (directory / name).is_file()),
            None,
        )
        if ssl_path is not None:
            yield crypto, ssl_path


def find_library_pair():
    """返回第一个带 ECH 能力的 (libcrypto, libssl) 路径，找不到抛 EchUnavailable。"""
    pair = next(_library_pairs(), None)
    if pair is not None:
        return pair
    raise EchUnavailable(
        "未找到带 ECH 支持的 OpenSSL 动态库（需要 OpenSSL 4.0+ 的 libssl）。"
        f"可把库文件放进 {bundle_root()}，或用 PIXIV_OPENSSL_DIR 指定其所在目录"
    )


def _ca_bundle():
    try:
        import certifi

        return certifi.where()
    except ImportError:  # pragma: no cover - httpx 一定会带上 certifi
        import ssl as _ssl

        paths = _ssl.get_default_verify_paths()
        return paths.cafile or paths.openssldir


class _LibSsl:
    def __init__(self, crypto_path, ssl_path):
        # Windows 上 libssl 依赖 libcrypto：先按完整路径加载 libcrypto，再加载 libssl。
        self._dll_dir = None
        if hasattr(os, "add_dll_directory"):
            self._dll_dir = os.add_dll_directory(str(Path(crypto_path).parent))
        elif sys.platform == "win32":  # pragma: no cover - Python 3.8+ 都有该 API
            os.environ["PATH"] = (
                str(Path(crypto_path).parent) + os.pathsep + os.environ.get("PATH", "")
            )
        try:
            ctypes.CDLL(str(crypto_path))
            self.ssl = ctypes.CDLL(str(ssl_path))
            self.ssl_path = str(ssl_path)
            if not hasattr(self.ssl, "SSL_set1_ech_config_list"):
                raise EchUnavailable(f"{ssl_path} 不支持 ECH（需要 OpenSSL 4.0+）")
        except BaseException:
            # 这个候选不可用（文件损坏、架构不符、版本没有 ECH）：先收回 dll 目录句柄
            # 再让上层换下一个候选，免得逐个失败时把句柄一路攒着。
            self._release_dll_dir()
            raise
        self._declare()

    def _release_dll_dir(self):
        if self._dll_dir is not None:
            try:
                self._dll_dir.close()
            except Exception:  # pragma: no cover - 关闭失败不值得影响主流程
                pass
            self._dll_dir = None

    def _declare(self):
        lib = self.ssl
        lib.TLS_client_method.restype = ctypes.c_void_p
        lib.SSL_CTX_new.argtypes = [ctypes.c_void_p]
        lib.SSL_CTX_new.restype = ctypes.c_void_p
        lib.SSL_CTX_free.argtypes = [ctypes.c_void_p]
        lib.SSL_CTX_ctrl.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_long,
            ctypes.c_void_p,
        ]
        lib.SSL_CTX_ctrl.restype = ctypes.c_long
        lib.SSL_CTX_load_verify_locations.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]
        lib.SSL_CTX_load_verify_locations.restype = ctypes.c_int
        lib.SSL_CTX_set_verify.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        lib.SSL_new.argtypes = [ctypes.c_void_p]
        lib.SSL_new.restype = ctypes.c_void_p
        lib.SSL_free.argtypes = [ctypes.c_void_p]
        lib.SSL_set_fd.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.SSL_set_fd.restype = ctypes.c_int
        lib.SSL_ctrl.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_long,
            ctypes.c_void_p,
        ]
        lib.SSL_ctrl.restype = ctypes.c_long
        lib.SSL_set1_host.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.SSL_set1_host.restype = ctypes.c_int
        lib.SSL_set1_ech_config_list.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        lib.SSL_set1_ech_config_list.restype = ctypes.c_int
        lib.SSL_connect.argtypes = [ctypes.c_void_p]
        lib.SSL_connect.restype = ctypes.c_int
        lib.SSL_read.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        lib.SSL_read.restype = ctypes.c_int
        lib.SSL_write.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        lib.SSL_write.restype = ctypes.c_int
        lib.SSL_get_error.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.SSL_get_error.restype = ctypes.c_int
        lib.SSL_get_verify_result.argtypes = [ctypes.c_void_p]
        lib.SSL_get_verify_result.restype = ctypes.c_long
        lib.SSL_get_version.argtypes = [ctypes.c_void_p]
        lib.SSL_get_version.restype = ctypes.c_char_p
        lib.SSL_get_servername.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.SSL_get_servername.restype = ctypes.c_char_p
        lib.SSL_shutdown.argtypes = [ctypes.c_void_p]
        lib.SSL_shutdown.restype = ctypes.c_int


_lib_lock = threading.Lock()
_lib = None
_context_lock = threading.Lock()
_context = None


def lib():
    """进程内单例加载 OpenSSL 动态库，失败抛 EchUnavailable。

    逐个候选尝试：项目自带那份若因为架构不符、缺少运行库或版本过低而载入失败，
    就继续往系统安装的位置找，最后才报错。失败原因全部记进异常信息，方便排查
    到底是内置的坏了还是机器上根本没有。
    """
    global _lib
    with _lib_lock:
        if _lib is None:
            errors = []
            for crypto_path, ssl_path in _library_pairs():
                try:
                    _lib = _LibSsl(crypto_path, ssl_path)
                except (OSError, EchUnavailable) as exc:
                    errors.append(f"{ssl_path}: {exc}")
                    continue
                break
            if _lib is None:
                detail = "；".join(errors) if errors else "没有找到任何候选 OpenSSL 动态库"
                raise EchUnavailable(
                    f"未找到可用的 ECH OpenSSL 动态库（需要 OpenSSL 4.0+ 的 libssl）：{detail}"
                )
        return _lib


def available():
    try:
        lib()
        return True
    except EchUnavailable:
        return False


def library_path():
    try:
        return lib().ssl_path
    except EchUnavailable:
        return ""


def _client_context():
    global _context
    with _context_lock:
        if _context is None:
            lib_ssl = lib()
            ssl = lib_ssl.ssl
            ctx = ssl.SSL_CTX_new(ssl.TLS_client_method())
            if not ctx:
                raise EchConnectError("SSL_CTX_new 失败")
            # 只提供 TLS 1.3，见模块开头说明。
            ssl.SSL_CTX_ctrl(ctx, SSL_CTRL_SET_MIN_PROTO_VERSION, TLS1_3_VERSION, None)
            ssl.SSL_CTX_ctrl(ctx, SSL_CTRL_SET_MAX_PROTO_VERSION, TLS1_3_VERSION, None)
            ca = _ca_bundle()
            if ssl.SSL_CTX_load_verify_locations(ctx, str(ca).encode(), None) != 1:
                raise EchConnectError(f"加载 CA 证书失败: {ca}")
            ssl.SSL_CTX_set_verify(ctx, SSL_VERIFY_PEER, None)
            _context = ctx
        return _context


def _decode_config(value):
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    text = "".join(line for line in lines if not line.startswith("-----"))
    text = "".join(text.split())
    try:
        raw = base64.b64decode(text, validate=True)
    except Exception as exc:
        raise EchUnavailable(f"ECH 配置不是合法 base64: {exc}") from exc
    if len(raw) < 4:
        raise EchUnavailable("ECH 配置过短")
    return raw


def _ech_from_doh(payload):
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


def fetch_ech_config(name, timeout=5.0, endpoints=None):
    """用 DoH 查 HTTPS 记录里的 ech=（本机 DNS 被污染，必须走 DoH）。

    依次尝试 DOH_ENDPOINTS，全部失败时抛 EchUnavailable（调用方决定是否兜底）。
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
        except Exception as exc:  # noqa: BLE001 — 任一端点失败就换下一个
            errors.append(f"{endpoint}: {exc.__class__.__name__}")
            continue
        if found:
            return found
    if errors:
        raise EchUnavailable("DoH 查询失败（" + "; ".join(errors) + "）")
    return None


_config_lock = threading.Lock()
_config_cache = None
_config_cached_at = 0.0


def reset_config_cache():
    """丢掉缓存的 ECH 配置，下次握手重新走 DoH。"""
    global _config_cache, _config_cached_at
    with _config_lock:
        _config_cache = None
        _config_cached_at = 0.0


def _cache_fresh():
    return (
        _config_cache is not None
        and (time.monotonic() - _config_cached_at) < ECH_CONFIG_TTL
    )


def ech_config(settings=None, refresh=False):
    """返回 ECHConfigList 原始字节：显式配置 > DoH 获取 > 内置常量。"""
    override = getattr(settings, "ech_config", "") if settings is not None else ""
    if override:
        return _decode_config(override)
    global _config_cache, _config_cached_at
    with _config_lock:
        if refresh or not _cache_fresh():
            for name in ECH_CONFIG_NAMES:
                try:
                    found = fetch_ech_config(name)
                except Exception:
                    found = None
                if found:
                    _config_cache = found
                    break
            else:
                _config_cache = _decode_config(DEFAULT_ECH_CONFIG)
            _config_cached_at = time.monotonic()
        return _config_cache


def _io_timeout_value(seconds):
    seconds = max(seconds, 0.5)
    if sys.platform == "win32":
        return int(seconds * 1000)  # Windows 用毫秒整数
    return struct.pack("ll", int(seconds), int((seconds % 1) * 1e6))


class _Connection:
    """一次 ECH 连接：自管 fd、SSL 对象与超时。"""

    def __init__(self, ip, port, timeout):
        self._lib = lib()
        self._ssl = None
        self._sock = socket.create_connection(
            (ip, port), timeout=min(timeout, CONNECT_TIMEOUT)
        )
        # 关键：create_connection 的 timeout 会把 fd 设为非阻塞，OpenSSL 会返回 WANT_READ。
        self._sock.settimeout(None)

    def handshake(self, hostname, config):
        ssl = self._lib.ssl
        self._ssl = ssl.SSL_new(_client_context())
        if not self._ssl:
            raise EchConnectError("SSL_new 失败")
        if ssl.SSL_set_fd(self._ssl, self._sock.fileno()) != 1:
            raise EchConnectError("SSL_set_fd 失败")
        if ssl.SSL_ctrl(
            self._ssl, SSL_CTRL_SET_TLSEXT_HOSTNAME, 0, ctypes.c_char_p(hostname.encode())
        ) != 1:
            raise EchConnectError("设置 SNI 失败")
        if ssl.SSL_set1_host(self._ssl, hostname.encode()) != 1:
            raise EchConnectError("设置证书校验域名失败")
        if ssl.SSL_set1_ech_config_list(self._ssl, config, len(config)) != 1:
            raise EchConnectError("应用 ECH 配置失败")
        rc = ssl.SSL_connect(self._ssl)
        if rc != 1:
            code = ssl.SSL_get_error(self._ssl, rc)
            raise EchConnectError(f"ECH 握手失败（SSL_get_error={code}）")
        verify = ssl.SSL_get_verify_result(self._ssl)
        if verify != 0:
            raise EchConnectError(f"证书校验失败（X509 错误码 {verify}）")
        return ssl.SSL_get_version(self._ssl)

    def _limit(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EchConnectError("ECH 传输超时")
        value = _io_timeout_value(remaining)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO, value)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, value)
        return remaining

    def send(self, data, deadline):
        ssl = self._lib.ssl
        offset = 0
        while offset < len(data):
            self._limit(deadline)
            written = ssl.SSL_write(self._ssl, data[offset:], len(data) - offset)
            if written > 0:
                offset += written
                continue
            code = ssl.SSL_get_error(self._ssl, written)
            if code in (SSL_ERROR_WANT_READ, SSL_ERROR_WANT_WRITE):
                continue  # 受 SO_SNDTIMEO 约束，下一轮 _limit 会因超时抛出
            raise EchConnectError(f"发送请求失败（SSL_get_error={code}）")

    def recv(self, deadline):
        """返回读到的字节；b"" 表示对端已关闭；None 表示本轮回调无数据。"""
        ssl = self._lib.ssl
        self._limit(deadline)
        buf = ctypes.create_string_buffer(READ_CHUNK)
        size = ssl.SSL_read(self._ssl, buf, READ_CHUNK)
        if size > 0:
            return buf.raw[:size]
        code = ssl.SSL_get_error(self._ssl, size)
        if code == SSL_ERROR_ZERO_RETURN:
            return b""
        if code == SSL_ERROR_SYSCALL:
            return b"" if size == 0 else None
        if code in (SSL_ERROR_WANT_READ, SSL_ERROR_WANT_WRITE):
            return None
        raise EchConnectError(f"读取响应失败（SSL_get_error={code}）")

    def close(self):
        try:
            if self._ssl:
                self._lib.ssl.SSL_shutdown(self._ssl)
        except OSError:
            pass
        finally:
            try:
                self._sock.close()
            except OSError:
                pass
            if self._ssl:
                self._lib.ssl.SSL_free(self._ssl)
                self._ssl = None


def _response_complete(buf):
    """头部齐了且（按 Content-Length）正文也齐了，就不必等对端关闭。"""
    idx = buf.find(b"\r\n\r\n")
    if idx < 0:
        return False
    lines = bytes(buf[:idx]).decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        return False
    status = int(parts[1])
    if 100 <= status < 200 or status in (204, 304):
        return True
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length":
            try:
                return len(buf) - idx - 4 >= int(value.strip())
            except ValueError:
                return False
    return False


def _read_raw(conn, deadline):
    buf = bytearray()
    while not _response_complete(buf):
        chunk = conn.recv(deadline)
        if chunk is None:
            continue
        buf += chunk
        if not chunk:
            break  # 对端关闭
    return bytes(buf)


class _BufferReader:
    """把已经收完的响应字节包装成 sni_client._read_response 需要的 reader。"""

    def __init__(self, data):
        self._data = data
        self._pos = 0

    async def readline(self):
        idx = self._data.find(b"\n", self._pos)
        end = len(self._data) if idx < 0 else idx + 1
        line = self._data[self._pos : end]
        self._pos = end
        return line

    async def readexactly(self, count):
        chunk = self._data[self._pos : self._pos + count]
        self._pos += len(chunk)
        return chunk

    async def read(self, _count=-1):
        chunk = self._data[self._pos :]
        self._pos = len(self._data)
        return chunk


def _request_sync(method, url, params, data, json_body, headers, timeout, ips, settings):
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise EchConnectError(f"非法地址: {url}")
    port = parsed.port or 443
    path = parsed.path or "/"
    if parsed.query:
        # 同 sni_client：翻页传进来的是完整 URL，查询串在 parsed.query 里，
        # 只取 parsed.path 会把 offset 丢掉，导致「下一页」返回的还是第一页。
        path = f"{path}?{parsed.query}"
    candidates = ips or (hostname,)
    headers = dict(headers or {})
    # 与无 SNI 路径一致：ECH 只保护握手，Host 仍是服务端定位虚拟主机的依据。
    if not any(key.lower() == "host" for key in headers):
        headers["Host"] = hostname
    raw = _build_request(method, path, params, data, json_body, headers)
    config = ech_config(settings)
    has_override = bool(getattr(settings, "ech_config", ""))
    errors = []
    refreshed = False
    while True:
        for ip in candidates:
            conn = None
            deadline = time.monotonic() + timeout
            try:
                conn = _Connection(ip, port, timeout)
                conn.handshake(hostname, config)
                conn.send(raw, deadline)
                payload = _read_raw(conn, deadline)
                return asyncio.run(_read_response(_BufferReader(payload), timeout))
            except (OSError, EchConnectError) as exc:
                errors.append(f"{ip}: {exc.__class__.__name__}({exc})")
            finally:
                if conn is not None:
                    conn.close()
        # 所有 IP 都失败：若失败发生在 TLS 层（SSL_get_error=1，例如证书不匹配），
        # 很可能是缓存的 ECH 配置过期了，强制刷新一次再试；被中间盒 RST
        # （SSL_ERROR_SYSCALL=5）时刷新没有意义，直接放弃。
        tls_failed = any("SSL_get_error=1" in item for item in errors)
        if not refreshed and not has_override and tls_failed:
            refreshed = True
            fresh = ech_config(settings, refresh=True)
            if fresh != config:
                config = fresh
                errors.append("(刷新 ECH 配置后重试)")
                continue
        break
    raise EchConnectError(f"ECH 直连失败: {'; '.join(errors)}")


async def request(
    method,
    url,
    *,
    params=None,
    data=None,
    json=None,
    headers=None,
    timeout=30.0,
    ips=None,
    settings=None,
):
    """签名与 sni_client.request 对齐；握手与读写在线程里阻塞执行，避免卡住事件循环。"""
    return await asyncio.to_thread(
        _request_sync,
        method,
        url,
        params,
        data,
        json,
        headers,
        timeout,
        ips,
        settings,
    )
