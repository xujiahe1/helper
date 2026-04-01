"""
轻量级 Wave Open API HTTP 客户端，替代 wave-opensdk。

所有接口均为 POST JSON，Authorization header 直接传 token（无 Bearer 前缀）。
"""

import os
import threading
import time
from typing import Any, Dict, Optional

import requests

_DEFAULT_TIMEOUT = 30.0
_TOKEN_REFRESH_INTERVAL = 1500.0  # 25 min
_UNKNOWN_TRACE_ID = "unknown"


class WaveClient:
    """封装 Wave Open API 的 HTTP 调用。自动管理 access_token。"""

    def __init__(self, app_id: str, app_secret: str, domain: str = "open.hoyowave.com", timeout: float = _DEFAULT_TIMEOUT):
        self.app_id = app_id
        self.app_secret = app_secret
        self.domain = domain
        self.timeout = timeout

        self._token: Optional[str] = None
        self._token_ts: float = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Token
    # ------------------------------------------------------------------

    def _refresh_token(self) -> None:
        now = time.monotonic()
        with self._lock:
            if self._token is not None and (now - self._token_ts) < _TOKEN_REFRESH_INTERVAL:
                return
        resp = requests.post(
            f"https://{self.domain}/openapi/auth/v1/access_token/internal",
            headers={"Content-Type": "application/json"},
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("retcode") != 0:
            raise RuntimeError(f"获取 access_token 失败: {data}")
        with self._lock:
            self._token = data["data"]["access_token"]
            self._token_ts = time.monotonic()

    @property
    def token(self) -> str:
        with self._lock:
            return self._token

    # ------------------------------------------------------------------
    # 通用请求
    # ------------------------------------------------------------------

    @staticmethod
    def _attach_trace_id(data: Any, trace_id: str | None) -> dict:
        resolved_trace_id = trace_id or _UNKNOWN_TRACE_ID
        if isinstance(data, dict):
            data["trace_id"] = resolved_trace_id
            return data
        return {"data": data, "trace_id": resolved_trace_id}

    def post(self, path: str, body: dict | None = None, *, token_override: str | None = None, extra_headers: dict | None = None) -> dict:
        """发起 POST JSON 请求，自动附带 Authorization。"""
        self._refresh_token()
        
        headers = {"Content-Type": "application/json", "Authorization": token_override or self.token}
        if extra_headers:
            headers.update(extra_headers)
        resp = requests.post(
            f"https://{self.domain}{path}",
            headers=headers,
            json=body or {},
            timeout=self.timeout,
        )
        try:
            return self._attach_trace_id(resp.json(), resp.headers.get("x-trace-id"))
        except Exception:
            raise RuntimeError(
                f"Wave API does not reply json: {resp.text}\n"
                f"Report this trace-id to support: {resp.headers.get('x-trace-id', _UNKNOWN_TRACE_ID)}"
            )

    def upload(self, path: str, file_obj, *, token_override: str | None = None) -> dict:
        """multipart/form-data 文件上传。"""
        self._refresh_token()
        headers = {"Authorization": token_override or self.token}
        resp = requests.post(
            f"https://{self.domain}{path}",
            headers=headers,
            files={"file": file_obj},
            timeout=self.timeout,
        )
        try:
            return self._attach_trace_id(resp.json(), resp.headers.get("x-trace-id"))
        except Exception:
            raise RuntimeError(
                f"Wave API does not reply json: {resp.text}\n"
                f"Report this trace-id to support: {resp.headers.get('x-trace-id', _UNKNOWN_TRACE_ID)}"
            )


# ------------------------------------------------------------------
# 客户端缓存（按 app_id）
# ------------------------------------------------------------------

_client_cache: Dict[str, WaveClient] = {}


def get_or_create_client(app_id: str, app_secret: str, domain: str = "open.hoyowave.com") -> WaveClient:
    if app_id in _client_cache:
        return _client_cache[app_id]
    client = WaveClient(app_id, app_secret, domain)
    _client_cache[app_id] = client
    return client
