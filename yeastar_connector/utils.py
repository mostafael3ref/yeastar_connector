import re
import frappe

def get_settings():
    return frappe.get_single("Yeastar Settings")

def normalize_phone(phone: str, default_cc: str = "+966") -> str:
    """
    Normalize phone to E.164-ish without spaces.
    Examples:
      0555123456 -> +966555123456
      +966 55 512 3456 -> +966555123456
      966555123456 -> +966555123456
    """
    if not phone:
        return ""

    p = str(phone).strip()
    p = re.sub(r"[^\d+]", "", p)

    # If starts with 00 -> +
    if p.startswith("00"):
        p = "+" + p[2:]

    # If starts with + keep it
    if p.startswith("+"):
        return "+" + re.sub(r"[^\d]", "", p[1:])

    # If starts with country code digits (like 966...)
    cc_digits = re.sub(r"[^\d]", "", default_cc)
    if p.startswith(cc_digits):
        return "+" + p

    # If local leading 0
    if p.startswith("0"):
        p = p[1:]

    return default_cc + p

def find_party_by_phone(phone_norm: str):
    """
    Return tuple (doctype, name) if matches Customer or Lead.
    Searches in common fields.
    """
    if not phone_norm:
        return (None, None)

    # Customer
    cust = frappe.db.get_value(
        "Customer",
        {"mobile_no": phone_norm},
        "name",
    ) or frappe.db.get_value(
        "Customer",
        {"phone": phone_norm},
        "name",
    )
    if cust:
        return ("Customer", cust)

    # Lead
    lead = frappe.db.get_value(
        "Lead",
        {"mobile_no": phone_norm},
        "name",
    ) or frappe.db.get_value(
        "Lead",
        {"phone": phone_norm},
        "name",
    )
    if lead:
        return ("Lead", lead)

    return (None, None)

def create_lead_from_phone(phone_norm: str, source="Phone Call"):
    lead = frappe.get_doc({
        "doctype": "Lead",
        "lead_name": phone_norm,
        "mobile_no": phone_norm,
        "source": source
    })
    lead.insert(ignore_permissions=True)
    return lead.name

def get_agent_user_by_extension(extension: str):
    if not extension:
        return None
    return frappe.db.get_value("Yeastar Agent", {"extension": str(extension)}, "user")

def safe_json(obj):
    try:
        import json
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return str(obj)
