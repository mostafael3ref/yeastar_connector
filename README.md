# yeastar_connector

Private Yeastar P-Series (P570) integration for ERPNext/Frappe.

This app provides a direct integration between Yeastar PBX and ERPNext using API & Webhooks.
It logs calls, links them to Customers/Leads, and maps PBX extensions to ERPNext users.

> ⚠️ Proprietary Software  
> This repository is **not open source**. Redistribution or reuse is prohibited without permission.
> See `LICENSE` for full terms.

---

## Features (MVP)
- Receive Yeastar Webhook Events (incoming / outgoing / answered / ended / missed)
- Create & update call logs in ERPNext (custom DocType)
- Auto-link calls to Customer or Lead by phone number
- Extension → User (Agent) mapping
- Store raw webhook payload for debugging and auditing

---

## Requirements
- ERPNext / Frappe v15+
- Yeastar P-Series (tested with P570)
- Public ERPNext URL or network access from Yeastar to ERPNext

---

## Installation

### 1) Get the app
```bash
bench get-app https://github.com/mostafael3ref/yeastar_connector
bench --site yoursite.com install-app yeastar_connector
bench --site yoursite.com migrate