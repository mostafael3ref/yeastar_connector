from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

import frappe
from frappe.utils import now_datetime

from yeastar_connector.yeastar_client import YeastarClient
from yeastar_connector.utils import normalize_phone


def _now_ts() -> int:
    return int(time.time())


def _get_flag(settings, *names: str) -> int:
    for n in names:
        v = getattr(settings, n, None)
        if v is None:
            continue
        try:
            if int(v or 0):
                return 1
        except Exception:
            if bool(v):
                return 1
    return 0


def _get_time_window(settings) -> Tuple[int, int]:
    start_ts = int(getattr(settings, "last_sync_at_ts", None) or 0)

    if not start_ts:
        start_ts = int(getattr(settings, "sync_from_ts", None) or 0)

    if not start_ts:
        start_ts = _now_ts() - 24 * 3600

    start_ts = max(0, start_ts - 600)
    end_ts = _now_ts()
    return start_ts, end_ts


def run():
    settings = frappe.get_single("Yeastar Settings")

    if not _get_flag(settings, "enable_sync_jobs", "sync_enabled", "enable_sync", "sync_jobs_enabled"):
        return

    client = YeastarClient(settings)

    if _get_flag(settings, "sync_extensions", "enable_sync_extensions"):
        sync_extensions(client)

    sync_call_logs(client)

    settings.db_set("last_sync_at_ts", _now_ts(), update_modified=False)


def sync_extensions(client: YeastarClient):
    settings = client.settings
    page = 1
    page_size = int(getattr(settings, "page_size", 100) or 100)

    while True:
        data = client.fetch_extensions(page=page, page_size=page_size)
        items = _extract_items(data)

        if not items:
            break

        for ext in items:
            upsert_agent_from_extension(ext)

        if not _has_more(data, page, page_size, len(items)):
            break
        page += 1


def sync_call_logs(client: YeastarClient):
    settings = client.settings
    page = 1
    page_size = int(getattr(settings, "page_size", 100) or 100)

    start_ts, end_ts = _get_time_window(settings)

    while True:
        data = client.fetch_call_logs(start_ts=start_ts, end_ts=end_ts, page=page, page_size=page_size)
        items = _extract_items(data)

        if not items:
            break

        for row in items:
            upsert_call_log(row, settings)

        if not _has_more(data, page, page_size, len(items)):
            break
        page += 1


def _extract_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []

    for key in ("data", "items", "list", "records", "result"):
        v = payload.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            for k2 in ("items", "list", "records", "data"):
                if isinstance(v.get(k2), list):
                    return v.get(k2)
    return []


def _has_more(payload: Dict[str, Any], page: int, page_size: int, got: int) -> bool:
    if not isinstance(payload, dict):
        return False

    total = None
    for k in ("total", "total_count", "count"):
        if isinstance(payload.get(k), int):
            total = payload.get(k)
            break
    if total is not None:
        return page * page_size < int(total)

    return got >= page_size


def upsert_agent_from_extension(ext: Dict[str, Any]):
    extension = str(ext.get("extension") or ext.get("ext") or ext.get("number") or "").strip()
    name = str(ext.get("name") or ext.get("username") or ext.get("display_name") or "").strip()

    if not extension:
        return

    docname = frappe.db.get_value("Yeastar Agent", {"extension": extension}, "name")
    if docname:
        doc = frappe.get_doc("Yeastar Agent", docname)
        if name and doc.get("agent_name") != name:
            doc.db_set("agent_name", name, update_modified=False)
        return

    doc = frappe.get_doc({
        "doctype": "Yeastar Agent",
        "extension": extension,
        "agent_name": name or extension,
    })
    doc.insert(ignore_permissions=True)


def upsert_call_log(row: Dict[str, Any], settings):
    call_id = str(row.get("call_id") or row.get("uniqueid") or row.get("id") or row.get("cdr_id") or row.get("cdrId") or "").strip()
    if not call_id:
        call_id = f"{row.get('start_time')}-{row.get('src')}-{row.get('dst')}"

    direction = str(row.get("direction") or row.get("call_direction") or row.get("type") or "").strip().lower()
    status = str(row.get("status") or row.get("state") or row.get("event") or row.get("call_state") or "").strip().lower()

    src = str(row.get("src") or row.get("caller") or row.get("caller_number") or row.get("from") or "").strip()
    dst = str(row.get("dst") or row.get("callee") or row.get("callee_number") or row.get("to") or "").strip()

    default_cc = str(getattr(settings, "phone_country_code", "+966") or "+966")
    src_n = normalize_phone(src, default_cc=default_cc)
    dst_n = normalize_phone(dst, default_cc=default_cc)

    extension = str(row.get("extension") or row.get("ext") or row.get("agent_ext") or row.get("agent_extension") or "").strip()

    start_time = row.get("start_time") or row.get("startTime") or row.get("start_ts") or row.get("startTs")
    end_time = row.get("end_time") or row.get("endTime") or row.get("end_ts") or row.get("endTs")

    duration = row.get("duration") or row.get("billsec") or row.get("talk_time") or row.get("talkTime") or 0
    try:
        duration = int(duration)
    except Exception:
        duration = 0

    recording_url = str(row.get("recording_url") or row.get("record_url") or row.get("recording") or row.get("recordingUrl") or "").strip()

    doc_data = {
        "call_id": call_id,
        "direction": direction or None,
        "status": status or None,
        "from_number": src_n or None,
        "to_number": dst_n or None,
        "extension": extension or None,
        "duration": duration or None,
        "recording_url": recording_url or None,
        "raw_payload": frappe.as_json(row),
        "last_event_at": now_datetime(),
    }

    if start_time:
        doc_data["start_time"] = str(start_time)
    if end_time:
        doc_data["end_time"] = str(end_time)

    existing_name = frappe.db.get_value("Yeastar Call Log", {"call_id": call_id}, "name")

    if existing_name:
        doc = frappe.get_doc("Yeastar Call Log", existing_name)
        for k, v in doc_data.items():
            if k in ("raw_payload", "last_event_at", "status"):
                doc.set(k, v)
                continue
            if v in (None, "", 0):
                continue
            if not doc.get(k) or doc.get(k) in ("", 0):
                doc.set(k, v)
        doc.save(ignore_permissions=True)
        return

    doc = frappe.get_doc({"doctype": "Yeastar Call Log", **doc_data})
    doc.insert(ignore_permissions=True)
