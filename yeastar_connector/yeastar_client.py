# yeastar_connector/yeastar_connector/yeastar_client.py

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
    # Single doctype
    return frappe.get_single("Yeastar Settings")


class YeastarClient:
    """
    Yeastar P-Series (Appliance Edition) API client.

    IMPORTANT:
    - Yeastar P-Series uses:
        POST {base_url}{api_base_path}/get_token
      Body JSON:
        {"username": <Client ID>, "password": <Client Secret>}
      Requires header:
        User-Agent: OpenAPI
    - Token expires in 30 minutes.
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

        self.timeout = int(getattr(self.settings, "request_timeout", None) or 20)

        # Allow overriding token URL from settings, otherwise default to P-Series get_token
        self.token_url = (getattr(self.settings, "token_url", None) or "").strip()
        if not self.token_url:
            self.token_url = f"{self.base_url}{self.api_base_path}/get_token"

        # Refresh endpoint (optional, used if refresh_token exists)
        self.refresh_url = f"{self.base_url}{self.api_base_path}/refresh_token"

    # ---------------------------
    # Token helpers (Password fields safe storage)
    # ---------------------------
    def _set_password_field(self, fieldname: str, value: str):
        """
        Store into Password field correctly.
        Using set_password ensures Password record exists.
        """
        self.settings.set_password(fieldname, value)
        # save without modifying "modified" timestamp if possible
        self.settings.save(ignore_permissions=True)

    def _get_password_field(self, fieldname: str) -> str:
        return (self.settings.get_password(fieldname) or "").strip()

    def _token_valid(self) -> bool:
        access_token = self._get_password_field("access_token")
        exp = getattr(self.settings, "token_expires_at_ts", None)
        if not access_token or not exp:
            return False
        # refresh قبلها بدقيقة
        return _now_ts() < int(exp) - 60

    def ensure_token(self) -> str:
        if self._token_valid():
            return self._get_password_field("access_token")

        # If refresh_token exists, try refresh first, else get new token
        if self._get_password_field("refresh_token"):
            try:
                return self.refresh_token()
            except Exception:
                # fallback to fresh token
                return self.get_token()

        return self.get_token()

    def get_token(self) -> str:
        """
        Yeastar P-Series:
          POST /openapi/v1.0/get_token
          JSON {"username": client_id, "password": client_secret}
          Header User-Agent: OpenAPI
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "OpenAPI",
        }
        body = {"username": self.client_id, "password": self.client_secret}

        try:
            resp = requests.post(self.token_url, json=body, headers=headers, timeout=self.timeout)
        except Exception as e:
            frappe.log_error(title="Yeastar OAuth request failed", message=frappe.get_traceback())
            raise YeastarAPIError(str(e))

        if resp.status_code >= 300:
            frappe.log_error(
                title="Yeastar OAuth error",
                message=f"HTTP {resp.status_code}\nURL: {self.token_url}\nBody: {resp.text[:2000]}",
            )
            raise YeastarAPIError(f"Token request failed: HTTP {resp.status_code}")

        payload = resp.json() if resp.text else {}
        # Yeastar returns: errcode/errmsg/access_token/access_token_expire_time/refresh_token...
        errcode = payload.get("errcode")
        if errcode not in (0, "0", None):
            frappe.log_error(title="Yeastar OAuth invalid payload", message=str(payload)[:2000])
            raise YeastarAPIError(f"Token error: {payload.get('errmsg') or payload}")

        access_token = (payload.get("access_token") or "").strip()
        expires_in = int(payload.get("access_token_expire_time") or payload.get("expires_in") or 1800)
        refresh_token = (payload.get("refresh_token") or "").strip()
        refresh_expires_in = int(payload.get("refresh_token_expire_time") or 86400)

        if not access_token:
            frappe.log_error(title="Yeastar OAuth invalid payload", message=str(payload)[:2000])
            raise YeastarAPIError("OAuth returned no access_token")

        exp_ts = _now_ts() + expires_in
        self._set_password_field("access_token", access_token)
        self.settings.db_set("token_expires_at_ts", exp_ts, update_modified=False)

        if refresh_token:
            self._set_password_field("refresh_token", refresh_token)
            self.settings.db_set("refresh_token_expires_at_ts", _now_ts() + refresh_expires_in, update_modified=False)

        return access_token

    def refresh_token(self) -> str:
        """
        Yeastar P-Series:
          POST /openapi/v1.0/refresh_token
          JSON {"refresh_token": "<token>"}
          Header User-Agent: OpenAPI
        """
        rt = self._get_password_field("refresh_token")
        if not rt:
            return self.get_token()

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "OpenAPI",
        }
        body = {"refresh_token": rt}

        try:
            resp = requests.post(self.refresh_url, json=body, headers=headers, timeout=self.timeout)
        except Exception as e:
            frappe.log_error(title="Yeastar refresh token request failed", message=frappe.get_traceback())
            raise YeastarAPIError(str(e))

        if resp.status_code >= 300:
            frappe.log_error(
                title="Yeastar refresh token error",
                message=f"HTTP {resp.status_code}\nURL: {self.refresh_url}\nBody: {resp.text[:2000]}",
            )
            # fallback
            return self.get_token()

        payload = resp.json() if resp.text else {}
        errcode = payload.get("errcode")
        if errcode not in (0, "0", None):
            frappe.log_error(title="Yeastar refresh invalid payload", message=str(payload)[:2000])
            # fallback
            return self.get_token()

        access_token = (payload.get("access_token") or "").strip()
        expires_in = int(payload.get("access_token_expire_time") or 1800)
        refresh_token = (payload.get("refresh_token") or "").strip()
        refresh_expires_in = int(payload.get("refresh_token_expire_time") or 86400)

        if not access_token:
            # fallback
            return self.get_token()

        exp_ts = _now_ts() + expires_in
        self._set_password_field("access_token", access_token)
        self.settings.db_set("token_expires_at_ts", exp_ts, update_modified=False)

        if refresh_token:
            self._set_password_field("refresh_token", refresh_token)
            self.settings.db_set("refresh_token_expires_at_ts", _now_ts() + refresh_expires_in, update_modified=False)

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
            "User-Agent": "OpenAPI",
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
    # API wrappers
    # ---------------------------
    def fetch_extensions(self, page: int = 1, page_size: int = 100) -> Dict[str, Any]:
        endpoint = getattr(self.settings, "extensions_endpoint", None) or "/extension/list"
        params = {"page": page, "page_size": page_size}
        return self.get(endpoint, params=params)

    def fetch_call_logs(self, start_ts: int, end_ts: int, page: int = 1, page_size: int = 100) -> Dict[str, Any]:
        endpoint = getattr(self.settings, "call_logs_endpoint", None) or "/cdr/list"
        params = {
            "start_time": start_ts,
            "end_time": end_ts,
            "page": page,
            "page_size": page_size,
        }
        return self.get(endpoint, params=params)

    def fetch_recording_download_url(self, recording_id: str) -> Dict[str, Any]:
        endpoint_tpl = getattr(self.settings, "recording_endpoint_tpl", None) or "/recording/get"
        return self.get(endpoint_tpl, params={"id": recording_id})
