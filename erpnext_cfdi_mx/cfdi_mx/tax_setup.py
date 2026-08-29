"""One-click Mexico tax configuration (Q9 decision: the app self-configures).

Creates/verifies purchase + item tax templates for the Black Brûlée chart,
pointing at *existing* company accounts. Never creates accounts itself.
"""

from __future__ import annotations

import frappe
from frappe import _

# Template names this app owns. Generic ones the user already has are untouched.
PURCHASE_TEMPLATES = {
	"16%": ("MX Compra IVA 16%", "IVA"),
	"0%": ("MX Compra Sin IVA", None),
}
RETENTION_TEMPLATES = {
	"honorarios": ("MX Compra Retencion Honorarios", "ISR", "IVA"),
	"arrendamiento": ("MX Compra Retencion Arrendamiento", "ISR", "IVA"),
}


def _find_account(company: str, needle: str, account_type: str = "") -> str | None:
	"""Find a single account whose path name contains `needle`, under company.

	Comparison collapses repeated spaces so "ISR  RETENIDO - BB" still matches
	the needle "ISR RETENIDO" (seen in the real chart).
	"""
	filters: dict = {"company": company, "is_group": 0}
	matches = frappe.db.get_all(
		"Account",
		filters=filters,
		fields=["name", "account_type", "parent_account", "is_group"],
		limit_page_length=0,
	)
	needle_norm = " ".join(needle.lower().split())
	pool = [a["name"] for a in matches if needle_norm in " ".join(a["name"].lower().split())]
	if not pool:
		return None
	# prefer tax-typed accounts when a type is requested
	for a in matches:
		if a["name"] in pool and account_type and a.get("account_type") == account_type:
			return a["name"]
	return pool[0]


def _tax_like_account(company: str, preferred: str | None) -> str | None:
	"""Return an account valid for Item Tax Templates (type Tax/Income/Expense/Chargeable).

	Prefers the app-configured account; falls back to any Tax-typed account so
	we never mutate the account_type of existing chart accounts.
	"""
	def _ok(name: str) -> bool:
		atype = frappe.db.get_value("Account", name, "account_type") or ""
		return atype in ("Tax", "Income Account", "Expense Account", "Chargeable")

	if preferred and _ok(preferred):
		return preferred
	return _find_account(company, "IVA", account_type="Tax")


def setup_mexico_tax(company: str | None = None) -> dict:
	"""Idempotent setup: ensure the app's purchase/item tax templates exist.

	Account lookup is name-fuzzy but never destructive. Returns the created
	facts so the Settings screen can display them.
	"""
	settings = frappe.get_single("CFDI Mapper Settings")
	company = company or settings.company
	if not company:
		frappe.throw(_("Selecciona la Compañía en CFDI Mapper Settings primero."))

	iva_account = settings.iva_acreditable_account or _find_account(company, "IVA ACREDITABLE")
	if not iva_account:
		frappe.throw(
			_("No se encontró una cuenta 'IVA ACREDITABLE' en la compañía {0}. "
				"Crea o selecciona la cuenta manualmente en CFDI Mapper Settings.").format(company)
		)
	isr_account = settings.isr_retention_account or _find_account(company, "ISR RETENIDO")
	iva_ret_account = settings.iva_retention_account or _find_account(company, "IVA RETENIDO")

	created = []

	def ensure_purchase_template(title, rows):
		if frappe.db.exists("Purchase Taxes and Charges Template", title):
			return False
		doc = frappe.new_doc("Purchase Taxes and Charges Template")
		doc.title = title
		doc.company = company
		for row in rows:
			doc.append(
				"taxes",
				{
					"charge_type": row.get("charge_type", "On Net Total"),
					"account_head": row["account_head"],
					"rate": row.get("rate", 0),
					"description": row.get("description", title),
				},
			)
		doc.save(ignore_permissions=True)
		created.append(title)
		return True

	def ensure_item_template(title, rate):
		if frappe.db.exists("Item Tax Template", title):
			return False
		doc = frappe.new_doc("Item Tax Template")
		doc.title = title
		doc.company = company
		# a row is mandatory even for the 0% template (rate 0.0)
		doc.append("taxes", {"tax_type": item_tax_account, "tax_rate": rate or 0.0})
		doc.save(ignore_permissions=True)
		created.append(title)
		return True

	item_tax_account = _tax_like_account(company, iva_account)
	# Purchase templates use the configured (acreditable) account; item
	# templates are labels only and need a Tax-typed account.
	ensure_purchase_template(
		"MX Compra IVA 16%",
		[{"account_head": iva_account, "rate": 16.0, "description": "IVA @ 16.0"}],
	)
	ensure_purchase_template("MX Compra Sin IVA", [])

	# Item tax templates used to tag Items with their tax method
	ensure_item_template("MX Item IVA 16%", 16.0)
	ensure_item_template("MX Item IVA 0%", None)

	# Retention templates for services (only if the accounts exist)
	if isr_account:
		ensure_purchase_template(
			"MX Compra Retencion Honorarios",
			[
				{"account_head": isr_account, "rate": 10.0, "description": "ISR retenido 10%"},
				{"account_head": iva_ret_account, "rate": 10.6667, "description": "IVA retenido 2/3"} if iva_ret_account else {},
			],
		)

	settings.iva_acreditable_account = iva_account
	settings.isr_retention_account = isr_account
	settings.iva_retention_account = iva_ret_account
	summary = "\n".join(
		[
			"IVA acreditable: " + (iva_account or "-"),
			"ISR retenido: " + (isr_account or "-"),
			"IVA retenido: " + (iva_ret_account or "-"),
			"Plantillas: " + (", ".join(created) if created else "(ya existían)"),
		]
	)
	settings.configured_tax_templates = summary
	settings.save(ignore_permissions=True)
	return {"created": created, "iva_account": iva_account, "summary": summary}


def purchase_template_name(tax_rate_key: str) -> str:
	"""Resolve the internal row key of a line (line's IVA rate) to a template name."""
	key = str(tax_rate_key)
	if key == "16":
		return "MX Compra IVA 16%"
	if key == "0":
		return "MX Compra Sin IVA"
	raise frappe.ValidationError(_("Método de IVA no soportado en la línea: {0}").format(key))
