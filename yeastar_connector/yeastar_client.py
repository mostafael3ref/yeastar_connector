# yeastar_connector/yeastar_connector/yeastar_client.py

from __future__ import annotations

import time
import urllib.parse
from typing import Any, Dict, Optional

import frappe
import requests


class YeastarAPIError(Exception):
    pass


def _now_ts() -> int:
    return int(time.time())


def _as_bool(v) -> bool:
    return bool(v) and str(v).lower() not in ("0", "false", "no", "none", "")


def get_settings():
    # Single doctype
    return frappe.get_single("Yeastar Settings")


class YeastarClient:
    """
    OAuth client for Yeastar API + helper GET/POST.
    Token is stored in Yeastar Settings.

    You MUST fill these fields in Yeastar Settings:
      - pbx_base_url              e.g. https://abrajataj.ras.yeastar.com
      - client_id
      - client_secret
      - token_url (optional)      default: <pbx_base_url>/openapi/v1.0/oauth2/token
      - api_base_path (optional)  default: /openapi/v1.0
    """

    def __init__(self, settings=None):
        self.settings = settings or get_settings()

        self.base_url = (self.settings.pbx_base_url or "").strip().rstrip("/")
        if not self.base_url:
            frappe.throw("Yeastar Settings: pbx_base_url is required")

        self.client_id = (self.settings.client_id or "").strip()
        self.client_secret = (self.settings.get_password("client_secret") or "").strip()
        if not self.client_id or not self.client_secret:
            frappe.throw("Yeastar Settings: client_id and client_secret are required")

        self.api_base_path = (getattr(self.settings, "api_base_path", None) or "/openapi/v1.0").strip()
        if not self.api_base_path.startswith("/"):
            self.api_base_path = "/" + self.api_base_path

        self.token_url = (getattr(self.settings, "token_url", None) or f"{self.base_url}{self.api_base_path}/oauth2/token").strip()

        self.timeout = int(getattr(self.settings, "request_timeout", None) or 20)

    # ---------------------------
    # Token / OAuth
    # ---------------------------
    def _token_valid(self) -> bool:
        access_token = (self.settings.get_password("access_token") or "").strip()
        exp = getattr(self.settings, "token_expires_at_ts", None)
        if not access_token or not exp:
            return False
        # refresh قبلها بدقيقة
        return _now_ts() < int(exp) - 60

    def ensure_token(self) -> str:
        if self._token_valid():
            return self.settings.get_password("access_token")

        return self.refresh_token()

    def refresh_token(self) -> str:
        """
        Most Yeastar implementations support client_credentials.
        If your Yeastar uses auth_code, we'll adjust later.
        """
        data = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        try:
            resp = requests.post(self.token_url, data=data, headers=headers, timeout=self.timeout)
        except Exception as e:
            frappe.log_error(title="Yeastar OAuth request failed", message=frappe.get_traceback())
            raise YeastarAPIError(str(e))

        if resp.status_code >= 300:
            frappe.log_error(
                title="Yeastar OAuth error",
                message=f"HTTP {resp.status_code}\nURL: {self.token_url}\nBody: {resp.text[:2000]}",
            )
            raise YeastarAPIError(f"OAuth failed: HTTP {resp.status_code}")

        payload = resp.json() if resp.headers.get("content-type", "").lower().startswith("application/json") else {}
        access_token = (payload.get("access_token") or "").strip()
        expires_in = int(payload.get("expires_in") or 3600)

        if not access_token:
            frappe.log_error(title="Yeastar OAuth invalid payload", message=str(payload)[:2000])
            raise YeastarAPIError("OAuth returned no access_token")

        # store token in settings
        exp_ts = _now_ts() + expires_in
        self.settings.db_set("access_token", access_token, update_modified=False)
        self.settings.db_set("token_expires_at_ts", exp_ts, update_modified=False)

        # Optional refresh token if exists
        if payload.get("refresh_token"):
            self.settings.db_set("refresh_token", payload.get("refresh_token"), update_modified=False)

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

    def _headers(self) -> Dict[str, str]:
        token = self.ensure_token()
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self._build_url(path)
        try:
            resp = requests.get(url, headers=self._headers(), params=params or {}, timeout=self.timeout)
        except Exception as e:
            frappe.log_error(title="Yeastar GET failed", message=frappe.get_traceback())
            raise YeastarAPIError(str(e))

        if resp.status_code >= 300:
            frappe.log_error(title="Yeastar GET error", message=f"HTTP {resp.status_code}\nURL: {url}\n{resp.text[:2000]}")
            raise YeastarAPIError(f"GET failed: HTTP {resp.status_code}")

        return resp.json() if resp.text else {}

    def post(self, path: str, json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self._build_url(path)
        try:
            resp = requests.post(url, headers=self._headers(), json=json or {}, timeout=self.timeout)
        except Exception as e:
            frappe.log_error(title="Yeastar POST failed", message=frappe.get_traceback())
            raise YeastarAPIError(str(e))

        if resp.status_code >= 300:
            frappe.log_error(title="Yeastar POST error", message=f"HTTP {resp.status_code}\nURL: {url}\n{resp.text[:2000]}")
            raise YeastarAPIError(f"POST failed: HTTP {resp.status_code}")

        return resp.json() if resp.text else {}

    # ---------------------------
    # API wrappers (adjust paths)
    # ---------------------------
    def fetch_extensions(self, page: int = 1, page_size: int = 100) -> Dict[str, Any]:
        """
        Adjust endpoint if needed.
        """
        endpoint = getattr(self.settings, "extensions_endpoint", None) or "/extension/list"
        params = {"page": page, "page_size": page_size}
        return self.get(endpoint, params=params)

    def fetch_call_logs(self, start_ts: int, end_ts: int, page: int = 1, page_size: int = 100) -> Dict[str, Any]:
        """
        Adjust endpoint if needed.
        """
        endpoint = getattr(self.settings, "call_logs_endpoint", None) or "/cdr/list"
        params = {
            "start_time": start_ts,  # sometimes needs YYYY-mm-dd HH:MM:SS instead
            "end_time": end_ts,
            "page": page,
            "page_size": page_size,
        }
        return self.get(endpoint, params=params)

    def fetch_recording_download_url(self, recording_id: str) -> Dict[str, Any]:
        """
        Adjust if you have a recordings endpoint.
        """
        endpoint_tpl = getattr(self.settings, "recording_endpoint_tpl", None) or "/recording/get"
        # if template needs id as param
        return self.get(endpoint_tpl, params={"id": recording_id})
