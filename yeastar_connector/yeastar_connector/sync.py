# yeastar_connector/yeastar_connector/sync.py

from __future__ import annotations

import time
from typing import Any, Dict, List

import frappe

from yeastar_connector.yeastar_client import YeastarClient
from yeastar_connector.utils import normalize_phone


def _now_ts() -> int:
    return int(time.time())


def _get_time_window(settings) -> tuple[int, int]:
    """
    start_ts based on last_sync_at_ts or sync_from_ts
    """
    # لو أول مرة: خليه من sync_from_ts أو آخر 24 ساعة كافتراضي
    start_ts = int(getattr(settings, "last_sync_at_ts", None) or 0)
    if not start_ts:
        start_ts = int(getattr(settings, "sync_from_ts", None) or 0)
    if not start_ts:
        start_ts = _now_ts() - 24 * 3600

    end_ts = _now_ts()
    return start_ts, end_ts


def run():
    """
    Scheduled entrypoint (called by hooks cron)
    """
    settings = frappe.get_single("Yeastar Settings")
    if not int(getattr(settings, "sync_enabled", 0) or 0):
        return

    client = YeastarClient(settings)

    # 1) Extensions (optional)
    if int(getattr(settings, "sync_extensions", 0) or 0):
        sync_extensions(client)

    # 2) Call Logs (important)
    sync_call_logs(client)

    # update last sync ts after success
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


# ----------------------------
# Helpers to handle variations in Yeastar API response
# ----------------------------
def _extract_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Different Yeastar builds return different shapes.
    We'll try common keys.
    """
    for key in ("data", "items", "list", "records", "result"):
        v = payload.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):
            # some APIs nest list under dict
            for k2 in ("items", "list", "records", "data"):
                if isinstance(v.get(k2), list):
                    return v.get(k2)
    return []


def _has_more(payload: Dict[str, Any], page: int, page_size: int, got: int) -> bool:
    """
    Try to detect pagination.
    """
    # if API returns total
    total = None
    for k in ("total", "total_count", "count"):
        if isinstance(payload.get(k), int):
            total = payload.get(k)
            break
    if total is not None:
        return page * page_size < int(total)

    # fallback: if we got a full page, maybe there are more
    return got >= page_size


# ----------------------------
# Upserts
# ----------------------------
def upsert_agent_from_extension(ext: Dict[str, Any]):
    """
    Map Yeastar extension to Yeastar Agent DocType.
    You may need to adjust keys based on Yeastar response.
    """
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
    """
    Insert/update Yeastar Call Log based on a unique call id.
    Adjust field names if your DocType differs.
    """
    # try to get a stable id
    call_id = str(row.get("call_id") or row.get("uniqueid") or row.get("id") or "").strip()
    if not call_id:
        # fallback: build pseudo id
        call_id = f"{row.get('start_time')}-{row.get('src')}-{row.get('dst')}"

    exists = frappe.db.exists("Yeastar Call Log", {"call_id": call_id})
    if exists:
        return

    direction = str(row.get("direction") or row.get("call_direction") or "").strip().lower()
    src = str(row.get("src") or row.get("caller") or row.get("caller_number") or "").strip()
    dst = str(row.get("dst") or row.get("callee") or row.get("callee_number") or "").strip()

    # Normalize phones for matching CRM
    src_n = normalize_phone(src, default_cc=str(getattr(settings, "phone_country_code", "+966") or "+966"))
    dst_n = normalize_phone(dst, default_cc=str(getattr(settings, "phone_country_code", "+966") or "+966"))

    start_ts = int(row.get("start_time") or row.get("start_ts") or row.get("timestamp") or 0)
    end_ts = int(row.get("end_time") or row.get("end_ts") or 0)
    duration = int(row.get("duration") or 0)

    extension = str(row.get("extension") or row.get("ext") or row.get("agent_ext") or "").strip()

    recording_id = str(row.get("recording_id") or row.get("record_id") or "").strip()
    recording_url = str(row.get("recording_url") or row.get("record_url") or "").strip()

    doc = frappe.get_doc({
        "doctype": "Yeastar Call Log",
        "call_id": call_id,
        "direction": direction,
        "src": src,
        "dst": dst,
        "src_normalized": src_n,
        "dst_normalized": dst_n,
        "start_time_ts": start_ts,
        "end_time_ts": end_ts,
        "duration": duration,
        "extension": extension,
        "recording_id": recording_id,
        "recording_url": recording_url,
        "raw_payload": frappe.as_json(row),
    })
    doc.insert(ignore_permissions=True)

    # Auto link to Lead/Customer by phone (optional)
    if int(getattr(settings, "auto_link_crm", 0) or 0):
        try_link_crm(doc, settings)


def try_link_crm(call_log_doc, settings):
    """
    Example linking strategy:
    - Find Lead by phone
    - Find Customer by phone
    Adjust per your CRM fields.
    """
    # choose which number is "external" based on direction
    number = call_log_doc.src_normalized or call_log_doc.dst_normalized
    if not number:
        return

    # Lead match (example fields)
    lead = frappe.db.get_value("Lead", {"phone": ["like", f"%{number[-8:]}%"]}, "name")
    if lead:
        call_log_doc.db_set("lead", lead, update_modified=False)
        return

    customer = frappe.db.get_value("Customer", {"mobile_no": ["like", f"%{number[-8:]}%"]}, "name")
    if customer:
        call_log_doc.db_set("customer", customer, update_modified=False)
