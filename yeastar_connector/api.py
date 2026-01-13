import json
import hashlib
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
# Helpers
# ----------------------------------------------------------------------

def _log(title: str, message: str, settings=None):
    """
    Log to Error Log only when debug enabled (if field exists).
    """
    try:
        if settings and int(getattr(settings, "debug_webhook", 0) or 0):
            frappe.log_error(title=title, message=message[:4000])
    except Exception:
        pass


def _stable_fallback_id(data: dict) -> str:
    """
    Stable fallback id (prevents duplicates when call_id is missing).
    """
    base = f"{data.get('from_no')}|{data.get('to_no')}|{data.get('extension')}|{data.get('status')}|{data.get('start_time')}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:20]


# ----------------------------------------------------------------------
# Security
# ----------------------------------------------------------------------

def _require_secret(settings, payload: dict):
    """
    Validate Yeastar Webhook Secret.

    Yeastar may send the secret via:
    - Header: X-Yeastar-Secret
    - Header: X-Webhook-Secret
    - Header: Authorization / authorization (Bearer <secret>)
    - Body: secret / webhook_secret
    """

    expected = (settings.get_password("webhook_secret") or "").strip()
    if not expected:
        frappe.throw(_("Webhook Secret is not set in Yeastar Settings."), frappe.PermissionError)

    headers = frappe.request.headers or {}

    incoming = (
        (headers.get("X-Yeastar-Secret") or "").strip()
        or (headers.get("X-Webhook-Secret") or "").strip()
        or (headers.get("Authorization") or headers.get("authorization") or "").strip()
        or str(payload.get("secret") or payload.get("webhook_secret") or "").strip()
    )

    # Handle: Authorization: Bearer <secret>
    if incoming.lower().startswith("bearer "):
        incoming = incoming.split(" ", 1)[1].strip()

    if incoming != expected:
        frappe.log_error(
            title="Yeastar Webhook Secret mismatch",
            message=(
                "Incoming secret is missing or invalid.\n"
                f"Has X-Yeastar-Secret: {'yes' if headers.get('X-Yeastar-Secret') else 'no'}\n"
                f"Has X-Webhook-Secret: {'yes' if headers.get('X-Webhook-Secret') else 'no'}\n"
                f"Has Authorization: {'yes' if headers.get('Authorization') or headers.get('authorization') else 'no'}\n"
                f"Has Body Secret: {'yes' if payload.get('secret') or payload.get('webhook_secret') else 'no'}\n"
            ),
        )
        frappe.throw(_("Invalid webhook secret."), frappe.PermissionError)


# ----------------------------------------------------------------------
# Payload extraction
# ----------------------------------------------------------------------

def _extract_event(payload: dict) -> dict:
    call_id = (
        payload.get("call_id")
        or payload.get("callId")
        or payload.get("unique_id")
        or payload.get("uniqueid")
        or payload.get("cdr_id")
        or payload.get("cdrId")
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
        or payload.get("call_state")
        or ""
    )

    from_no = (
        payload.get("from")
        or payload.get("caller")
        or payload.get("caller_number")
        or payload.get("callerNumber")
        or payload.get("src")
        or ""
    )

    to_no = (
        payload.get("to")
        or payload.get("callee")
        or payload.get("callee_number")
        or payload.get("calleeNumber")
        or payload.get("dst")
        or ""
    )

    extension = (
        payload.get("extension")
        or payload.get("ext")
        or payload.get("agent_extension")
        or payload.get("extension_number")
        or payload.get("agent_ext")
        or ""
    )

    start_time = payload.get("start_time") or payload.get("startTime")
    end_time = payload.get("end_time") or payload.get("endTime")

    duration = (
        payload.get("duration")
        or payload.get("billsec")
        or payload.get("talk_time")
        or payload.get("talkTime")
    )

    recording_url = (
        payload.get("recording_url")
        or payload.get("recordingUrl")
        or payload.get("recording")
        or payload.get("record_url")
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
    if not data.get("call_id"):
        data["call_id"] = _stable_fallback_id(data)

    default_cc = settings.phone_country_code or "+966"
    from_norm = normalize_phone(data.get("from_no"), default_cc)
    to_norm = normalize_phone(data.get("to_no"), default_cc)

    # Ignore internal calls
    if int(getattr(settings, "ignore_internal_calls", 0) or 0):

        def _looks_ext(x: str) -> bool:
            x = (x or "").replace("+", "").strip()
            return x.isdigit() and 2 <= len(x) <= 6

        if _looks_ext(from_norm) and _looks_ext(to_norm):
            _log(
                "Yeastar Webhook skipped (internal call)",
                f"from={from_norm}, to={to_norm}",
                settings=settings,
            )
            return None

    if data.get("direction") in ("inbound", "incoming", "in"):
        party_phone = from_norm
    else:
        party_phone = to_norm

    linked_doctype, linked_name = find_party_by_phone(party_phone)

    if not linked_name and int(getattr(settings, "create_lead_if_not_found", 0) or 0):
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

    if existing:
        doc = frappe.get_doc("Yeastar Call Log", existing)
        for k, v in doc_data.items():
            if v not in (None, "", 0):
                doc.set(k, v)
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
      /api/method/yeastar_connector.api.webhook
    """

    settings = get_settings()
    if not int(getattr(settings, "enabled", 0) or 0):
        return {"ok": False, "message": "Yeastar Connector disabled"}

    raw = frappe.request.get_data(as_text=True) or "{}"

    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            payload = {"payload": payload}
    except Exception:
        payload = {"raw": raw}

    _log("Yeastar Webhook HIT", raw[:2000], settings=settings)

    _require_secret(settings, payload)

    data = _extract_event(payload)
    call_log_name = _upsert_call_log(data, payload, settings)

    return {
        "ok": True,
        "call_log": call_log_name,
    }
