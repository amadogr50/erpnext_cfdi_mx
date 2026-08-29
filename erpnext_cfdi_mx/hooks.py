app_name = "erpnext_cfdi_mx"
app_title = "Erpnext CFDI MX"
app_publisher = "Amado González Rodríguez"
app_description = "Import CFDI (Mexican e-invoices) as semi-automatic purchases."
app_email = "amadogr50@gmail.com"
app_license = "mit"

# Apps
# ------------------
# required_apps = []

# Includes in <head>
# ------------------
# app_include_js = "/assets/erpnext_cfdi_mx/js/erpnext_cfdi_mx.js"
# app_include_css = "/assets/erpnext_cfdi_mx/css/erpnext_cfdi_mx.css"

# DocType JS
# ----------
doctype_js = {
    "CFDI Recibido": "public/js/cfdi_recibido.js",
    "CFDI Mapper Settings": "public/js/cfdi_mapper_settings.js",
}

# Installation
# ------------
after_install = "erpnext_cfdi_mx.install.after_install"
after_app_install = "erpnext_cfdi_mx.install.after_app_install"

# Permissions
# -----------
# Role that owns the CFDI flow. Granted to Administrator on install.
# has_permission = {
#     "CFDI Recibido": "erpnext_cfdi_mx.cfdi_mx.permissions.has_permission",
# }

# Whitelisted API (called from the desk UI)
# -------------------------------------------
# The methods live in erpnext_cfdi_mx.cfdi_mx.api and expose only @frappe.whitelist funcs.

# DocEvents
# ----------
# doc_events = {
#     "Purchase Invoice": {
#         "on_submit": "erpnext_cfdi_mx.cfdi_mx.api.on_purchase_doc_submit",
#     }
# }

# Translation
# ------------
default_language = "es"
