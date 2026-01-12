from __future__ import annotations

import time
from typing import Any, Dict, Optional

import frappe
import requests


class YeastarAPIError(Exception):
    pass


def _now_ts() -> int:
    return int(time.time())


def get_settings():
    return frappe.get_single("Yeastar Settings")


class YeastarClient:
    """
    Yeastar P-Series Cloud Edition OpenAPI client.

    Auth:
      POST <base_url><api_base_path>/get_token
      Headers MUST include: User-Agent: OpenAPI
      Body: {"username": "<Client ID>", "password": "<Client Secret>"}

    API calls:
      Use query param: access_token=<token>
      (Not Authorization header, not token=)
    """

    def __init__(self, settings=None):
        self.settings = settings or get_settings()

        self.base_url = (getattr(self.settings, "pbx_base_url", "") or "").strip().rstrip("/")
        if not self.base_url:
            frappe.throw("Yeastar Settings: pbx_base_url is required")

        self.api_base_path = (getattr(self.settings, "api_base_path", "") or "/openapi/v1.0").strip()
        if not self.api_base_path.startswith("/"):
            self.api_base_path = "/" + self.api_base_path

        self.token_url = (getattr(self.settings, "token_url", "") or "").strip()
        if not self.token_url:
            self.token_url = f"{self.base_url}{self.api_base_path}/get_token"

        self.timeout = int(getattr(self.settings, "request_timeout", 0) or 20)

    # ---------------------------
    # Token
    # ---------------------------
    def _get_access_token(self) -> str:
        return (getattr(self.settings, "access_token", "") or "").strip()

    def _token_valid(self) -> bool:
        token = self._get_access_token()
        exp = int(getattr(self.settings, "token_expires_at_ts", 0) or 0)
        if not token or not exp:
            return False
        return _now_ts() < exp - 60  # refresh 60s early

    def ensure_token(self) -> str:
        if self._token_valid():
            return self._get_access_token()
        return self.refresh_token()

    def refresh_token(self) -> str:
        api_username = (getattr(self.settings, "api_username", "") or "").strip()

        try:
            api_password = (self.settings.get_password("api_password") or "").strip()
        except Exception:
            api_password = (getattr(self.settings, "api_password", "") or "").strip()

        if not api_username or not api_password:
            raise YeastarAPIError("Yeastar Settings missing api_username / api_password (Client ID/Secret)")

        payload: Dict[str, Any] = {"username": api_username, "password": api_password}

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "OpenAPI",  # REQUIRED by Yeastar docs
        }

        try:
            resp = requests.post(self.token_url, json=payload, headers=headers, timeout=self.timeout)
        except Exception:
            frappe.log_error(title="Yeastar token request failed", message=frappe.get_traceback())
            raise YeastarAPIError("Token request failed (connection error)")

        try:
            data = resp.json() if resp.text else {}
        except Exception:
            data = {}

        if isinstance(data, dict) and data.get("errcode") and int(data.get("errcode") or 0) != 0:
            frappe.log_error(title="Yeastar token invalid payload", message=str(data)[:2000])
            raise YeastarAPIError(f"Token error: {data.get('errmsg') or data.get('errcode')}")

        access_token = (data.get("access_token") or "").strip()
        refresh_token = (data.get("refresh_token") or "").strip()
        expires_in = int(data.get("access_token_expire_time") or 1800)

        if not access_token:
            frappe.log_error(
                title="Yeastar token missing",
                message=f"HTTP {resp.status_code}\nURL: {self.token_url}\nBody: {resp.text[:2000]}",
            )
            raise YeastarAPIError("Token returned no access_token")

        exp_ts = _now_ts() + expires_in

        self.settings.db_set("access_token", access_token, update_modified=False)
        self.settings.db_set("refresh_token", refresh_token, update_modified=False)
        self.settings.db_set("token_expires_at_ts", exp_ts, update_modified=False)

        return access_token

    # ---------------------------
    # Requests
    # ---------------------------
    def _build_url(self, path: str) -> str:
        path = (path or "").strip()
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{self.api_base_path}{path}"

    def _with_access_token(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        p = dict(params or {})
        p["access_token"] = self.ensure_token()
        return p

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self._build_url(path)
        try:
            resp = requests.get(
                url,
                params=self._with_access_token(params),
                timeout=self.timeout,
                headers={"Accept": "application/json", "User-Agent": "OpenAPI"},
            )
        except Exception:
            frappe.log_error(title="Yeastar GET failed", message=frappe.get_traceback())
            raise YeastarAPIError("GET failed (connection error)")

        try:
            data = resp.json() if resp.text else {}
        except Exception:
            data = {}

        if isinstance(data, dict) and data.get("errcode") and int(data.get("errcode") or 0) != 0:
            frappe.log_error(title="Yeastar GET error payload", message=str(data)[:2000])
            raise YeastarAPIError(f"GET error: {data.get('errmsg') or data.get('errcode')}")

        if resp.status_code >= 300:
            frappe.log_error(title="Yeastar GET HTTP error", message=f"HTTP {resp.status_code}\nURL: {url}\n{resp.text[:2000]}")
            raise YeastarAPIError(f"GET failed: HTTP {resp.status_code}")

        return data if isinstance(data, dict) else {}

    # ---------------------------
    # API wrappers
    # ---------------------------
    def fetch_extensions(self, page: int = 1, page_size: int = 100) -> Dict[str, Any]:
        endpoint = getattr(self.settings, "extensions_endpoint", None) or "/extension/list"
        params = {"page": page, "page_size": page_size}
        return self.get(endpoint, params=params)

    def fetch_call_logs(self, start_ts: int, end_ts: int, page: int = 1, page_size: int = 100) -> Dict[str, Any]:
        endpoint = getattr(self.settings, "call_logs_endpoint", None) or "/cdr/list"
        params = {"start_time": start_ts, "end_time": end_ts, "page": page, "page_size": page_size}
        return self.get(endpoint, params=params)
