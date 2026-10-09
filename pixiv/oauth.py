"""Pixiv OAuth：用 refresh_token 换 access_token、按需续期、把令牌落盘。
"""

import asyncio
import base64
import hashlib
import json
import secrets
import time

import httpx

from .config import Settings
from .hoster import resolve, resolve_ech
from . import ech_client, sni_client


class AuthError(RuntimeError):
    """登录态相关错误：框架的错误处理器按 ``status`` 决定 HTTP 码。

    ``status`` / ``reason`` / ``message`` 是框架与站点之间唯一的约定
    （见 ``pixiv_bridge/errors.py``）—— 缺了 ``status`` 就会被当成 400，
    所以这里显式给 401。
    """

    status = 401

    def __init__(self, message, reason=""):
        super().__init__(message)
        self.message = message
        self.reason = reason


class OAuthManager:
    def __init__(self, settings, http):
        self._settings = settings
        self._http = http
        self._lock = asyncio.Lock()
        self._access_token = None
        self._refresh_token = settings.refresh_token
        self._expires_at = 0.0
        self._user = None

    @property
    def is_authenticated(self):
        return bool(self._access_token)

    @property
    def user(self):
        return self._user

    @property
    def refresh_token(self):
        return self._refresh_token

    @property
    def access_token_expires_at(self):
        return self._expires_at

    async def load(self):
        data = {}
        token_file = self._settings.token_file
        if token_file.exists():
            try:
                data = json.loads(token_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        if not self._refresh_token:
            self._refresh_token = data.get("refresh_token", "")
        self._access_token = data.get("access_token")
        self._expires_at = float(data.get("expires_at", 0))
        self._user = data.get("user")

    async def _persist(self):
        payload = {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "expires_at": self._expires_at,
            "user": self._user,
        }
        try:
            self._settings.token_file.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    @staticmethod
    def _iso_utc():
        return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())

    def _signed_headers(self):
        client_time = self._iso_utc()
        client_hash = hashlib.md5(
            (client_time + self._settings.hash_salt).encode("utf-8")
        ).hexdigest()
        s = self._settings
        return {
            "X-Client-Time": client_time,
            "X-Client-Hash": client_hash,
            "User-Agent": s.user_agent,
            "App-OS": s.app_os,
            "App-OS-Version": s.app_os_version,
            "App-Version": s.app_version,
            "Accept-Language": s.accept_language,
        }

    @staticmethod
    def _extract_error(resp):
        try:
            return resp.json().get("error", {}).get("message", resp.text)
        except ValueError:
            return resp.text

    async def _token_request(self, payload):
        try:
            if self._settings.proxy:
                # 配了代理就走代理转发，不再尝试 ECH/SNI 直连（与 api.py 的策略一致）。
                resp = await self._http.post(
                    f"{self._settings.oauth_base}/auth/token",
                    data=payload,
                    headers=self._signed_headers(),
                    timeout=self._settings.request_timeout,
                )
            elif self._settings.ech:
                # ECH 优先：加密 SNI 走 Cloudflare 边缘解密，照常校验证书。
                resp = await ech_client.request(
                    "POST",
                    f"{self._settings.oauth_base}/auth/token",
                    data=payload,
                    headers=self._signed_headers(),
                    timeout=self._settings.request_timeout,
                    ips=resolve_ech(
                        "oauth.secure.pixiv.net", self._settings.ech_ips
                    ),
                    settings=self._settings,
                )
            elif self._settings.bypass_sni:
                resp = await sni_client.request(
                    "POST",
                    f"{self._settings.oauth_base}/auth/token",
                    data=payload,
                    headers=self._signed_headers(),
                    timeout=self._settings.request_timeout,
                    ips=resolve(
                        "oauth.secure.pixiv.net", self._settings.oauth_ips
                    ),
                )
            else:
                resp = await self._http.post(
                    f"{self._settings.oauth_base}/auth/token",
                    data=payload,
                    headers=self._signed_headers(),
                    timeout=self._settings.request_timeout,
                )
        except (
            sni_client.SniConnectError,
            ech_client.EchConnectError,
            ech_client.EchUnavailable,
        ) as exc:
            raise AuthError(str(exc), "connect_failed")
        if resp.status_code != 200:
            raise AuthError(self._extract_error(resp), f"http_{resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            # 200 也可能不是 JSON（被拦、被网关换成 HTML）：裸 .json() 抛的
            # JSONDecodeError 调用方只兜 AuthError，会一路逃成裸 500。
            raise AuthError(
                f"换 token 的上游返回的不是 JSON：{resp.text[:200]}", "bad_upstream_body"
            ) from exc
        response = body.get("response", {})
        self._access_token = response.get("access_token")
        self._refresh_token = response.get("refresh_token") or self._refresh_token
        self._expires_at = time.time() + int(response.get("expires_in", 3600))
        self._user = response.get("user")
        await self._persist()
        return response

    async def _refresh_locked(self):
        if not self._refresh_token:
            raise AuthError("未配置 refresh_token")
        payload = {
            "client_id": self._settings.client_id,
            "client_secret": self._settings.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "include_policy": True,
        }
        return await self._token_request(payload)

    async def refresh(self):
        async with self._lock:
            return await self._refresh_locked()

    async def access_token(self):
        async with self._lock:
            if self._access_token and self._expires_at - time.time() > 60:
                return self._access_token
            return (await self._refresh_locked()).get("access_token")

    async def login_password(self, username, password):
        async with self._lock:
            payload = {
                "client_id": self._settings.client_id,
                "client_secret": self._settings.client_secret,
                "grant_type": "password",
                "username": username,
                "password": password,
                "device_token": "pixiv",
                "get_secure_url": True,
                "include_policy": True,
            }
            return await self._token_request(payload)

    @staticmethod
    def _code_verifier():
        return secrets.token_urlsafe(64)

    @staticmethod
    def _code_challenge(verifier):
        digest = hashlib.sha256(verifier.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def login_url(self):
        verifier = self._code_verifier()
        challenge = self._code_challenge(verifier)
        url = (
            f"{self._settings.api_base}/web/v1/login"
            f"?code_challenge={challenge}&code_challenge_method=S256"
            f"&client=pixiv-android"
        )
        return url, verifier

    async def exchange_code(self, code, code_verifier):
        async with self._lock:
            payload = {
                "client_id": self._settings.client_id,
                "client_secret": self._settings.client_secret,
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": self._settings.redirect_uri,
                "include_policy": True,
            }
            return await self._token_request(payload)
