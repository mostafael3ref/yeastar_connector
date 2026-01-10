import json
import frappe
from frappe import _
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
# Security
# ----------------------------------------------------------------------

def _require_secret(settings, payload: dict):
    """
    Validate Yeastar Webhook Secret.

    Yeastar may send the secret via:
    - Header: X-Yeastar-Secret
    - Header: X-Webhook-Secret
    - Body: secret / webhook_secret
    """

    expected = (settings.get_password("webhook_secret") or "").strip()
    if not expected:
        frappe.throw(
            _("Webhook Secret is not set in Yeastar Settings."),
            frappe.PermissionError,
        )

    incoming = (
        (frappe.get_request_header("X-Yeastar-Secret") or "").strip()
        or (frappe.get_request_header("X-Webhook-Secret") or "").strip()
        or str(payload.get("secret") or payload.get("webhook_secret") or "").strip()
    )

    if incoming != expected:
        frappe.throw(_("Invalid webhook secret."), frappe.PermissionError)


# ----------------------------------------------------------------------
# Payload extraction (generic – adjusted later per real samples)
# ----------------------------------------------------------------------

def _extract_event(payload: dict) -> dict:
    """
    Extract common call data from Yeastar webhook payload.
    Payload structure may vary by firmware/version.
    """

    call_id = (
        payload.get("call_id")
        or payload.get("callId")
        or payload.get("unique_id")
        or payload.get("uniqueid")
        or payload.get("id")
    )

    direction = (
        payload.get("direction")
        or payload.get("call_direction")
        or payload.get("type")
        or ""
    )

    status = (
        payload.get("status")
        or payload.get("event")
        or payload.get("state")
        or ""
    )

    from_no = (
        payload.get("from")
        or payload.get("caller")
        or payload.get("caller_number")
        or payload.get("callerNumber")
        or ""
    )

    to_no = (
        payload.get("to")
        or payload.get("callee")
        or payload.get("callee_number")
        or payload.get("calleeNumber")
        or ""
    )

    extension = (
        payload.get("extension")
        or payload.get("ext")
        or payload.get("agent_extension")
        or payload.get("extension_number")
        or ""
    )

    start_time = payload.get("start_time") or payload.get("startTime")
    end_time = payload.get("end_time") or payload.get("endTime")

    duration = (
        payload.get("duration")
        or payload.get("billsec")
        or payload.get("talk_time")
    )

    recording_url = (
        payload.get("recording_url")
        or payload.get("recordingUrl")
        or payload.get("recording")
        or ""
    )

    return {
        "call_id": str(call_id) if call_id else None,
        "direction": str(direction).lower()[:20],
        "status": str(status).lower()[:30],
        "from_no": str(from_no),
        "to_no": str(to_no),
        "extension": str(extension),
        "start_time": start_time,
        "end_time": end_time,
        "duration": int(duration) if str(duration).isdigit() else None,
        "recording_url": recording_url,
    }


# ----------------------------------------------------------------------
# Call Log upsert
# ----------------------------------------------------------------------

def _upsert_call_log(data: dict, raw_payload: dict, settings):
    # Fallback unique id
    if not data.get("call_id"):
        data["call_id"] = frappe.generate_hash(length=12)

    default_cc = settings.phone_country_code or "+966"

    from_norm = normalize_phone(data.get("from_no"), default_cc)
    to_norm = normalize_phone(data.get("to_no"), default_cc)

    # Determine customer side
    if data.get("direction") in ("inbound", "incoming", "in"):
        party_phone = from_norm
    else:
        party_phone = to_norm

    linked_doctype, linked_name = find_party_by_phone(party_phone)

    if not linked_name and settings.create_lead_if_not_found:
        linked_doctype = "Lead"
        linked_name = create_lead_from_phone(party_phone)

    agent_user = get_agent_user_by_extension(data.get("extension"))

    existing = frappe.db.get_value(
        "Yeastar Call Log", {"call_id": data["call_id"]}, "name"
    )

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

    if data.get("start_time"):
        doc_data["start_time"] = str(data["start_time"])
    if data.get("end_time"):
        doc_data["end_time"] = str(data["end_time"])

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
    """
    Yeastar Webhook Receiver

    URL:
      https://<site>/api/method/yeastar_connector.api.webhook

    Headers (one of them):
      X-Yeastar-Secret: <secret>
      X-Webhook-Secret: <secret>
    """

    settings = get_settings()
    if not settings.enabled:
        return {"ok": False, "message": "Yeastar Connector disabled"}

    raw = frappe.request.get_data(as_text=True) or "{}"

    # Debug: confirm webhook hit
    frappe.log_error(
        title="Yeastar Webhook HIT",
        message=raw[:2000],
    )

    try:
        payload = json.loads(raw)
    except Exception:
        payload = {"raw": raw}

    _require_secret(settings, payload)

    data = _extract_event(payload)
    call_log_name = _upsert_call_log(data, payload, settings)

    return {
        "ok": True,
        "call_log": call_log_name,
    }
