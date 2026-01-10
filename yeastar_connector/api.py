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

def _require_secret(settings):
    # Yeastar will send header: X-Yeastar-Secret
    incoming = (frappe.get_request_header("X-Yeastar-Secret") or "").strip()
    if not settings.webhook_secret:
        frappe.throw(_("Webhook Secret is not set in Yeastar Settings."), frappe.PermissionError)
    if incoming != settings.webhook_secret.strip():
        frappe.throw(_("Invalid webhook secret."), frappe.PermissionError)

def _extract_event(payload: dict) -> dict:
    """
    We keep this generic because Yeastar payload differs by firmware/settings.
    You will later adjust mapping once you send me a real webhook sample.
    """
    # Common guesses:
    call_id = payload.get("call_id") or payload.get("callId") or payload.get("unique_id") or payload.get("id")
    direction = payload.get("direction") or payload.get("call_direction") or payload.get("type")
    status = payload.get("status") or payload.get("event") or payload.get("state")

    from_no = payload.get("from") or payload.get("caller") or payload.get("caller_number") or payload.get("callerNumber")
    to_no = payload.get("to") or payload.get("callee") or payload.get("callee_number") or payload.get("calleeNumber")

    extension = payload.get("extension") or payload.get("ext") or payload.get("agent_extension") or payload.get("extension_number")

    start_time = payload.get("start_time") or payload.get("startTime")
    end_time = payload.get("end_time") or payload.get("endTime")
    duration = payload.get("duration") or payload.get("billsec") or payload.get("talk_time")

    recording_url = payload.get("recording_url") or payload.get("recordingUrl") or payload.get("recording")

    return {
        "call_id": str(call_id) if call_id else None,
        "direction": (direction or "").lower()[:20],
        "status": (status or "").lower()[:30],
        "from_no": str(from_no) if from_no else "",
        "to_no": str(to_no) if to_no else "",
        "extension": str(extension) if extension else "",
        "start_time": start_time,
        "end_time": end_time,
        "duration": int(duration) if str(duration).isdigit() else None,
        "recording_url": recording_url or "",
    }

def _upsert_call_log(data: dict, raw_payload: dict, settings):
    if not data.get("call_id"):
        # still allow insert but unique key is missing; we fallback to hash
        data["call_id"] = frappe.generate_hash(length=12)

    # normalize phone numbers
    default_cc = settings.phone_country_code or "+966"
    from_norm = normalize_phone(data.get("from_no"), default_cc)
    to_norm = normalize_phone(data.get("to_no"), default_cc)

    # choose “party phone” based on direction
    # inbound: caller is customer, outbound: callee is customer
    party_phone = from_norm if (data.get("direction") in ["inbound", "incoming", "in"]) else to_norm

    linked_doctype, linked_name = find_party_by_phone(party_phone)

    if not linked_name and settings.create_lead_if_not_found:
        linked_doctype = "Lead"
        linked_name = create_lead_from_phone(party_phone)

    agent_user = get_agent_user_by_extension(data.get("extension"))

    existing = frappe.db.get_value("Yeastar Call Log", {"call_id": data["call_id"]}, "name")

    doc_dict = {
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

    # Handle start/end if present (store as text/datetime string - you can refine later)
    if data.get("start_time"):
        doc_dict["start_time"] = str(data["start_time"])
    if data.get("end_time"):
        doc_dict["end_time"] = str(data["end_time"])

    if existing:
        doc = frappe.get_doc("Yeastar Call Log", existing)
        doc.update(doc_dict)
        doc.save(ignore_permissions=True)
        return doc.name

    doc = frappe.get_doc(doc_dict)
    doc.insert(ignore_permissions=True)
    return doc.name

@frappe.whitelist(allow_guest=True)
def webhook():
    """
    Yeastar Webhook receiver.
    Configure Yeastar to POST JSON to:
      https://<erp>/api/method/yeastar_connector.api.webhook
    Send header:
      X-Yeastar-Secret: <secret>
    """
    settings = get_settings()
    if not settings.enabled:
        return {"ok": False, "message": "disabled"}

    _require_secret(settings)

    # Read raw body
    raw = frappe.request.get_data(as_text=True) or "{}"
    try:
        payload = json.loads(raw)
    except Exception:
        payload = {"raw": raw}

    data = _extract_event(payload)
    name = _upsert_call_log(data, payload, settings)

    return {"ok": True, "call_log": name}
