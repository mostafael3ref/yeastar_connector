from __future__ import annotations

import time
from typing import Any, Dict, Optional

import frappe
import requests


class YeastarAPIError(Exception):
    pass


def get_settings():
    return frappe.get_single("Yeastar Settings")


class YeastarClient:
    """
    Yeastar OpenAPI Client
    Auth method:
      - X-Client-Id
      - X-Client-Secret

    NO token, NO get_token.
    """

    def __init__(self, settings=None):
        self.settings = settings or get_settings()

        self.base_url = (self.settings.pbx_base_url or "").rstrip("/")
        if not self.base_url:
            frappe.throw("Yeastar Settings: PBX Base URL is required")

        self.api_base_path = (self.settings.api_base_path or "/openapi/v1.0").strip()
        if not self.api_base_path.startswith("/"):
            self.api_base_path = "/" + self.api_base_path

        self.timeout = int(self.settings.request_timeout or 20)

        self.client_id = (self.settings.api_username or "").strip()
        try:
            self.client_secret = self.settings.get_password("api_password")
        except Exception:
            self.client_secret = self.settings.api_password

        if not self.client_id or not self.client_secret:
            frappe.throw("Yeastar Settings: API Username / Password not set")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Client-Id": self.client_id,
            "X-Client-Secret": self.client_secret,
        }

    def _build_url(self, path: str) -> str:
        path = path.strip()
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{self.api_base_path}{path}"

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self._build_url(path)
        try:
            resp = requests.get(
                url,
                params=params or {},
                headers=self._headers(),
                timeout=self.timeout,
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Yeastar GET failed")
            raise YeastarAPIError("GET failed (connection error)")

        if resp.status_code >= 300:
            frappe.log_error(
                title="Yeastar GET error",
                message=f"URL: {url}\nStatus: {resp.status_code}\n{resp.text[:2000]}",
            )
            raise YeastarAPIError(resp.text)

        try:
            return resp.json() if resp.text else {}
        except Exception:
            return {}

    def post(self, path: str, json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = self._build_url(path)
        try:
            resp = requests.post(
                url,
                json=json or {},
                headers=self._headers(),
                timeout=self.timeout,
            )
        except Exception:
            frappe.log_error(frappe.get_traceback(), "Yeastar POST failed")
            raise YeastarAPIError("POST failed (connection error)")

        if resp.status_code >= 300:
            frappe.log_error(
                title="Yeastar POST error",
                message=f"URL: {url}\nStatus: {resp.status_code}\n{resp.text[:2000]}",
            )
            raise YeastarAPIError(resp.text)

        try:
            return resp.json() if resp.text else {}
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # API wrappers
    # ------------------------------------------------------------------

    def fetch_extensions(self, page: int = 1, page_size: int = 100) -> Dict[str, Any]:
        endpoint = self.settings.extensions_endpoint or "/extension/list"
        params = {
            "page": page,
            "page_size": page_size,
        }
        return self.get(endpoint, params=params)

    def fetch_call_logs(
        self,
        start_ts: int,
        end_ts: int,
        page: int = 1,
        page_size: int = 100,
    ) -> Dict[str, Any]:
        endpoint = self.settings.call_logs_endpoint or "/cdr/list"
        params = {
            "start_time": start_ts,
            "end_time": end_ts,
            "page": page,
            "page_size": page_size,
        }
        return self.get(endpoint, params=params)

    def fetch_recording_download_url(self, recording_id: str) -> Dict[str, Any]:
        endpoint = self.settings.recording_endpoint or "/recording/get"
        return self.get(endpoint, params={"id": recording_id})
