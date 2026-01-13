import json
import hashlib
import frappe
from frappe.utils import now_datetime

from yeastar_connector.utils import (
    get_settings,
    normalize_phone,
    find_party_by_phone,
    create_lead_from_phone,
    get_agent_user_by_extension,
    safe_json,
)

# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _log(title: str, message: str, settings=None):
    try:
        if settings and int(getattr(settings, "debug_webhook", 0) or 0):
            frappe.log_error(title=title, message=message[:4000])
    except Exception:
        pass


def _stable_fallback_id(data: dict) -> str:
    base = f"{data.get('from_no')}|{data.get('to_no')}|{data.get('extension')}|{data.get('status')}|{data.get('start_time')}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:20]


# ----------------------------------------------------------------------
# Security (OPTIONAL – Yeastar does NOT send secret)
# ----------------------------------------------------------------------

def _check_secret_if_present(settings, payload: dict):
    """
    Yeastar Event Push DOES NOT send secret headers.
    So:
    - If secret exists → validate
    - If not exists → allow request
    """

    expected = (settings.get_password("webhook_secret") or "").strip()
    if not expected:
        return  # no secret configured

    incoming = (
        (frappe.get_request_header("X-Yeastar-Secret") or "").strip()
        or (frappe.get_request_header("X-Webhook-Secret") or "").strip()
        or str(payload.get("secret") or payload.get("webhook_secret") or "").strip()
    )

    if not incoming:
        # Just log, DO NOT BLOCK
        frappe.log_error(
            title="Yeastar Webhook: no secret sent",
            message="Yeastar Event Push does not include webhook secret"
        )
        return

    if incoming != expected:
        frappe.throw("Invalid webhook secret", frappe.PermissionError)


# ----------------------------------------------------------------------
# Payload extraction
# ----------------------------------------------------------------------

def _extract_event(payload: dict) -> dict:
    return {
        "call_id": payload.get("call_id") or payload.get("uniqueid") or payload.get("id"),
        "direction": (payload.get("direction") or "").lower(),
        "status": (payload.get("status") or payload.get("event") or "").lower(),
        "from_no": payload.get("from") or payload.get("caller") or payload.get("src"),
        "to_no": payload.get("to") or payload.get("callee") or payload.get("dst"),
        "extension": payload.get("extension") or payload.get("ext"),
        "start_time": payload.get("start_time"),
        "end_time": payload.get("end_time"),
        "duration": payload.get("duration"),
        "recording_url": payload.get("recording"),
    }


# ----------------------------------------------------------------------
# Call Log upsert
# ----------------------------------------------------------------------

def _upsert_call_log(data: dict, raw_payload: dict, settings):
    if not data.get("call_id"):
        data["call_id"] = _stable_fallback_id(data)

    from_norm = normalize_phone(data.get("from_no"), settings.phone_country_code)
    to_norm = normalize_phone(data.get("to_no"), settings.phone_country_code)

    if int(settings.ignore_internal_calls or 0):
        if from_norm.isdigit() and to_norm.isdigit():
            return None

    party_phone = from_norm if data.get("direction") == "inbound" else to_norm
    linked_doctype, linked_name = find_party_by_phone(party_phone)

    if not linked_name and int(settings.create_lead_if_not_found or 0):
        linked_doctype = "Lead"
        linked_name = create_lead_from_phone(party_phone)

    agent_user = get_agent_user_by_extension(data.get("extension"))

    existing = frappe.db.get_value("Yeastar Call Log", {"call_id": data["call_id"]})

    doc_data = {
        "doctype": "Yeastar Call Log",
        "call_id": data["call_id"],
        "direction": data.get("direction"),
        "status": data.get("status"),
        "from_number": from_norm,
        "to_number": to_norm,
        "extension": data.get("extension"),
        "agent_user": agent_user,
        "linked_doctype": linked_doctype,
        "linked_name": linked_name,
        "duration": data.get("duration"),
        "recording_url": data.get("recording_url"),
        "raw_payload": safe_json(raw_payload),
        "last_event_at": now_datetime(),
    }

    if existing:
        doc = frappe.get_doc("Yeastar Call Log", existing)
        doc.update(doc_data)
        doc.save(ignore_permissions=True)
        return doc.name

    doc = frappe.get_doc(doc_data)
    doc.insert(ignore_permissions=True)
    return doc.name


# ----------------------------------------------------------------------
# Webhook endpoint
# ----------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def webhook():
    settings = get_settings()
    if not int(settings.enabled or 0):
        return {"ok": False, "message": "Disabled"}

    raw = frappe.request.get_data(as_text=True) or "{}"

    try:
        payload = json.loads(raw)
    except Exception:
        payload = {"raw": raw}

    _log("Yeastar Webhook HIT", raw, settings)

    # IMPORTANT: secret is optional
    _check_secret_if_present(settings, payload)

    data = _extract_event(payload)
    call_log = _upsert_call_log(data, payload, settings)

    return {"ok": True, "call_log": call_log}
