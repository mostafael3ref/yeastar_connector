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
    return frappe.get_single("Yeastar Settings")


class YeastarClient:
    """
    Token client for Yeastar API.

    Your Yeastar seems to use:
      POST <base_url><api_base_path>/get_token
    and requires username + password (NOT oauth2/token client_credentials).

    Required settings fields (recommended):
      - pbx_base_url        e.g. https://abragtaj.ras.yeastar.com
      - api_base_path       e.g. /openapi/v1.0
      - token_url (optional) e.g. https://abragtaj.ras.yeastar.com/openapi/v1.0/get_token
      - api_username
      - api_password (Password field)

    Optional:
      - client_id / client_secret (some firmwares may require them, keep if you have)
      - request_timeout
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
            # your Yeastar does NOT have /oauth2/token, it has /get_token
            self.token_url = f"{self.base_url}{self.api_base_path}/get_token"

        self.timeout = int(getattr(self.settings, "request_timeout", 0) or 20)

        # Optional (keep if you want)
        self.client_id = (getattr(self.settings, "client_id", "") or "").strip()
        try:
            self.client_secret = (self.settings.get_password("client_secret") or "").strip()
        except Exception:
            self.client_secret = (getattr(self.settings, "client_secret", "") or "").strip()

    # ---------------------------
    # Token
    # ---------------------------
    def _get_access_token(self) -> str:
        # IMPORTANT: do NOT use get_password for access_token unless it's a Password field.
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
        """
        Yeastar get_token requires username/password (based on your error).
        """

        api_username = (getattr(self.settings, "api_username", "") or "").strip()

        api_password = ""
        # api_password should be Password field ideally
        try:
            api_password = (self.settings.get_password("api_password") or "").strip()
        except Exception:
            api_password = (getattr(self.settings, "api_password", "") or "").strip()

        if not api_username or not api_password:
            raise YeastarAPIError("Yeastar Settings missing api_username / api_password")

        # Some firmwares accept JSON, others accept form. We'll try JSON then fallback.
        payload_json = {
            "username": api_username,
            "password": api_password,
        }

        # If your firmware also needs client_id/secret, keep them (won't hurt if ignored)
        if self.client_id:
            payload_json["client_id"] = self.client_id
        if self.client_secret:
            payload_json["client_secret"] = self.client_secret

        headers_json = {"Accept": "application/json", "Content-Type": "application/json"}

        try:
            resp = requests.post(self.token_url, json=payload_json, headers=headers_json, timeout=self.timeout)
        except Exception:
            frappe.log_error(title="Yeastar OAuth request failed", message=frappe.get_traceback())
            raise YeastarAPIError("Token request failed (connection error)")

        # If server rejects JSON, retry as form
        if resp.status_code >= 300:
            headers_form = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
            try:
                resp2 = requests.post(self.token_url, data=payload_json, headers=headers_form, timeout=self.timeout)
                resp = resp2
            except Exception:
                frappe.log_error(title="Yeastar OAuth request failed (form retry)", message=frappe.get_traceback())
                raise YeastarAPIError(f"Token request failed: HTTP {resp.status_code}")

        # Parse payload
        ct = (resp.headers.get("content-type") or "").lower()
        data: Dict[str, Any] = {}
        if "application/json" in ct:
            try:
                data = resp.json() or {}
            except Exception:
                data = {}
        else:
            # sometimes Yeastar returns json but with wrong content-type
            try:
                data = resp.json() or {}
            except Exception:
                data = {}

        # Handle Yeastar-style error payloads
        # Example: {'errcode': 40002, 'errmsg': 'PARAMETER ERROR', 'invalid_param_list': ...}
        if isinstance(data, dict) and data.get("errcode") and int(data.get("errcode") or 0) != 0:
            frappe.log_error(title="Yeastar OAuth invalid payload", message=str(data)[:2000])
            raise YeastarAPIError(f"OAuth error: {data.get('errmsg') or data.get('message') or data.get('errcode')}")

        # Token can be at top-level or nested
        access_token = ""
        expires_in = 3600

        if isinstance(data.get("data"), dict):
            access_token = (data["data"].get("access_token") or data["data"].get("token") or "").strip()
            expires_in = int(data["data"].get("expires_in") or data["data"].get("expires") or 3600)
        else:
            access_token = (data.get("access_token") or data.get("token") or "").strip()
            expires_in = int(data.get("expires_in") or data.get("expires") or 3600)

        if not access_token:
            frappe.log_error(
                title="Yeastar OAuth invalid payload",
                message=f"HTTP {resp.status_code}\nURL: {self.token_url}\nBody: {resp.text[:2000]}",
            )
            raise YeastarAPIError("OAuth returned no access_token")

        exp_ts = _now_ts() + expires_in
        # store token as plain data field
        self.settings.db_set("access_token", access_token, update_modified=False)
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

    def _headers(self) -> Dict[str, str]:
        token = self.ensure_token()
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self._build_url(path)
        try:
            resp = requests.get(url, headers=self._headers(), params=params or {}, timeout=self.timeout)
        except Exception:
            frappe.log_error(title="Yeastar GET failed", message=frappe.get_traceback())
            raise YeastarAPIError("GET failed (connection error)")

        if resp.status_code >= 300:
            frappe.log_error(title="Yeastar GET error", message=f"HTTP {resp.status_code}\nURL: {url}\n{resp.text[:2000]}")
            raise YeastarAPIError(f"GET failed: HTTP {resp.status_code}")

        try:
            return resp.json() if resp.text else {}
        except Exception:
            return {}

    def post(self, path: str, json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self._build_url(path)
        try:
            resp = requests.post(url, headers=self._headers(), json=json or {}, timeout=self.timeout)
        except Exception:
            frappe.log_error(title="Yeastar POST failed", message=frappe.get_traceback())
            raise YeastarAPIError("POST failed (connection error)")

        if resp.status_code >= 300:
            frappe.log_error(title="Yeastar POST error", message=f"HTTP {resp.status_code}\nURL: {url}\n{resp.text[:2000]}")
            raise YeastarAPIError(f"POST failed: HTTP {resp.status_code}")

        try:
            return resp.json() if resp.text else {}
        except Exception:
            return {}

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

    def fetch_recording_download_url(self, recording_id: str) -> Dict[str, Any]:
        endpoint_tpl = getattr(self.settings, "recording_endpoint_tpl", None) or "/recording/get"
        return self.get(endpoint_tpl, params={"id": recording_id})
