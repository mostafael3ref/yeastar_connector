app_name = "yeastar_connector"
app_title = "Yeastar Connector"
app_publisher = "Mostafa EL-Areef"
app_description = "Private Yeastar P-Series (P570) integration for ERPNext/Frappe"
app_icon = "octicon octicon-device-mobile"
app_color = "blue"
app_email = "info@el3ref.com"
app_license = "Proprietary"

after_install = "yeastar_connector.install.after_install"

# (اختياري) ممكن تشيله؛ مش ضروري لو دالتك معمولها whitelist في api.py
override_whitelisted_methods = {
    "yeastar_connector.api.webhook": "yeastar_connector.api.webhook"
}

scheduler_events = {
    "cron": {
        "*/5 * * * *": [
            "yeastar_connector.sync.run"
        ]
    }
}

fixtures = []
