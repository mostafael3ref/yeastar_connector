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
    OAuth client for Yeastar API + helper GET/POST.
    Token is stored in Yeastar Settings.

    Required in Yeastar Settings:
      - pbx_base_url          e.g. https://abrajataj.ras.yeastar.com
      - client_id
      - client_secret
      - api_base_path         e.g. /openapi/v1.0
      - token_url             e.g. https://abrajataj.ras.yeastar.com/openapi/v1.0/get_token
    """

    def __init__(self, settings=None):
        self.settings = settings or get_settings()

        self.base_url = (self.settings.pbx_base_url or "").strip().rstrip("/")
        if not self.base_url:
            frappe.throw("Yeastar Settings: PBX Base URL is required")

        self.client_id = (self.settings.client_id or "").strip()
        self.client_secret = (self.settings.get_password("client_secret", raise_exception=False) or "").strip()
        if not self.client_id or not self.client_secret:
            frappe.throw("Yeastar Settings: client_id and client_secret are required")

        self.api_base_path = (getattr(self.settings, "api_base_path", None) or "/openapi/v1.0").strip()
        if not self.api_base_path.startswith("/"):
            self.api_base_path = "/" + self.api_base_path

        # IMPORTANT: Yeastar P-Series غالباً token endpoint = /get_token
        self.token_url = (getattr(self.settings, "token_url", None) or f"{self.base_url}{self.api_base_path}/get_token").strip()

        self.timeout = int(getattr(self.settings, "request_timeout", None) or 20)

    # ---------------------------
    # Password-safe getters
    # ---------------------------
    def _get_pwd(self, fieldname: str) -> str:
        # لا ترمي Exception لو مفيش قيمة
        return (self.settings.get_password(fieldname, raise_exception=False) or "").strip()

    # ---------------------------
    # Token / OAuth
    # ---------------------------
    def _token_valid(self) -> bool:
        access_token = self._get_pwd("access_token")
        exp = int(getattr(self.settings, "token_expires_at_ts", None) or 0)

        if not access_token or not exp:
            return False

        # refresh قبلها بدقيقة
        return _now_ts() < exp - 60

    def ensure_token(self) -> str:
        if self._token_valid():
            return self._get_pwd("access_token")
        return self.refresh_token()

    def refresh_token(self) -> str:
        """
        Yeastar /get_token غالباً بيرجع:
          { "errcode": 0, "errmsg": "SUCCESS", "access_token": "...", "expires_in": 7200 }
        وبعض الإصدارات ترجع شكل OAuth عادي.
        """

        # جرّب Form (زي OAuth)
        data_form = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        # وجرّب JSON كمان (بعض الأجهزة بتحبه)
        data_json = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }

        headers_form = {"Content-Type": "application/x-www-form-urlencoded"}
        headers_json = {"Content-Type": "application/json"}

        def _parse_payload(resp) -> Dict[str, Any]:
            ct = (resp.headers.get("content-type") or "").lower()
            if "application/json" in ct:
                try:
                    return resp.json()
                except Exception:
                    return {}
            # fallback لو رجّع نص
            try:
                return resp.json()
            except Exception:
                return {}

        # 1) try POST form
        resp = None
        payload: Dict[str, Any] = {}
        try:
            resp = requests.post(self.token_url, data=data_form, headers=headers_form, timeout=self.timeout)
            payload = _parse_payload(resp)
        except Exception:
            payload = {}

        # 2) لو فشل أو رجّع payload فاضي/غلط → جرّب JSON
        if not payload or (isinstance(payload, dict) and payload.get("errcode") not in (None, 0) and not payload.get("access_token")):
            try:
                resp = requests.post(self.token_url, json=data_json, headers=headers_json, timeout=self.timeout)
                payload = _parse_payload(resp)
            except Exception as e:
                frappe.log_error(title="Yeastar OAuth request failed", message=frappe.get_traceback())
                raise YeastarAPIError(str(e))

        if not resp:
            raise YeastarAPIError("OAuth request failed (no response)")

        if resp.status_code >= 300:
            frappe.log_error(
                title="Yeastar OAuth HTTP error",
                message=f"HTTP {resp.status_code}\nURL: {self.token_url}\nBody: {resp.text[:2000]}",
            )
            raise YeastarAPIError(f"OAuth failed: HTTP {resp.status_code}")

        if not isinstance(payload, dict) or not payload:
            frappe.log_error(
                title="Yeastar OAuth invalid payload",
                message=f"URL: {self.token_url}\nBody: {resp.text[:2000]}",
            )
            raise YeastarAPIError("OAuth invalid payload (not JSON)")

        # لو Yeastar style
        if "errcode" in payload and int(payload.get("errcode") or 0) != 0:
            frappe.log_error(title="Yeastar OAuth invalid payload", message=str(payload)[:2000])
            raise YeastarAPIError(f"OAuth error: {payload.get('errmsg') or payload.get('message') or payload.get('errcode')}")

        access_token = (payload.get("access_token") or payload.get("accessToken") or "").strip()
        expires_in = int(payload.get("expires_in") or payload.get("expiresIn") or 3600)

        if not access_token:
            frappe.log_error(title="Yeastar OAuth returned no access_token", message=str(payload)[:2000])
            raise YeastarAPIError("OAuth returned no access_token")

        exp_ts = _now_ts() + expires_in

        # تخزين صحيح لحقول Password
        self.settings.set_password("access_token", access_token)
        self.settings.db_set("token_expires_at_ts", exp_ts, update_modified=False)

        # optional
        if payload.get("refresh_token"):
            self.settings.set_password("refresh_token", payload.get("refresh_token"))

        # مهم: حفظ الـ password fields
        self.settings.save(ignore_permissions=True)

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
