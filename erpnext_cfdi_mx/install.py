from __future__ import annotations

import frappe


def after_install(*args, **kwargs):
	"""Runs when the app is installed on a site."""
	grant_cfdi_importer_role_to_administrator()


def after_app_install(*args, **kwargs):
	"""Runs after the app is installed via bench install-app.

	Frappe invokes this hook passing the app name as a positional argument,
	so the signature must accept *args (see installer.run_after_install_hooks).
	"""
	grant_cfdi_importer_role_to_administrator()


def grant_cfdi_importer_role_to_administrator() -> None:
	"""Create the CFDI Importer role and grant it to Administrator.

	Idempotent: safe to run on every install.
	"""
	if not frappe.db.exists("Role", "CFDI Importer"):
		frappe.get_doc({"doctype": "Role", "role_name": "CFDI Importer"}).insert(
			ignore_permissions=True
		)

	if not frappe.db.exists("Has Role", {"role": "CFDI Importer", "parent": "Administrator"}):
		frappe.get_doc(
			{
				"doctype": "Has Role",
				"role": "CFDI Importer",
				"parent": "Administrator",
				"parenttype": "User",
				"parentfield": "roles",
			}
		).insert(ignore_permissions=True)