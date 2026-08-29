"""Mapping layer: (supplier RFC, normalized description) -> Item / expense rule.

This is the canonization core of the app. A CFDI Item Rule is the single
source of truth for how a supplier's concept maps into the company's catalog
(which Item, which UOM conversion, which tax treatment, inventory or expense).
"""

from __future__ import annotations

import frappe
from frappe import _

from .normalizer import normalize_description, suggest_factor

RULE_LINK = "CFDI Item Rule"


def normalize(s: str) -> str:
	"""Public helper so the UI and import service share the same key."""
	return normalize_description(s)


def find_rule(supplier_tax_id: str, description: str, supplier: str | None = None) -> dict | None:
	"""Find an existing rule for (supplier, normalized description).

	Rules are identified by their supplier's RFC (tax_id) because that is the
	stable identity of the emitter across CFDI files. Falls back to the supplier
	name if no RFC is known.
	"""
	key = normalize_description(description)
	if not key:
		return None

	filters: dict = {"normalized_key": key}
	if supplier_tax_id:
		filters["supplier_tax_id"] = supplier_tax_id
	elif supplier:
		filters["supplier"] = supplier

	truth = {
		"name",
		"supplier",
		"supplier_tax_id",
		"description",
		"normalized_key",
		"item",
		"is_inventory",
		"conversion_factor",
		"stock_uom",
		"expense_account",
		"tax_rate",
	}
	row = frappe.db.get_value(RULE_LINK, filters, list(truth), as_dict=True)
	if not row:
		match = frappe.db.get_all(RULE_LINK, filters=filters, fields=list(truth), limit=1)
		if not match:
			return None
		return match[0]
	return row


def create_rule(
	supplier_tax_id: str,
	cfdi_description: str,
	*,
	supplier: str,
	item: str | None = None,
	is_inventory: bool = True,
	conversion_factor: float = 1.0,
	stock_uom: str | None = None,
	expense_account: str | None = None,
	tax_rate: str = "0%",
	clave_prod_serv: str = "",
	cfdi_unit: str = "",
) -> dict:
	"""Create a rule, auto-saving it from a mapping decision made by the user.

	When `item` is omitted and is_inventory is True, a new Item is created.
	Returns the rule as a dict.
	"""
	doc = frappe.new_doc(RULE_LINK)
	doc.supplier = supplier
	doc.supplier_tax_id = supplier_tax_id
	doc.description = cfdi_description
	doc.normalized_key = normalize_description(cfdi_description)
	doc.tax_rate = tax_rate or "0%"
	doc.clave_prod_serv = clave_prod_serv
	doc.cfdi_unit = cfdi_unit

	doc.is_inventory = 1 if is_inventory else 0
	if is_inventory:
		if not item:
			item = _auto_create_item(cfdi_description, stock_uom, tax_rate)
		doc.item = item
		doc.stock_uom = stock_uom or frappe.db.get_value("Item", item, "stock_uom")
		doc.conversion_factor = conversion_factor or 1.0
	else:
		doc.expense_account = expense_account

	doc.save(ignore_permissions=True)
	return doc.as_dict()


def _auto_create_item(description: str, stock_uom: str | None, tax_rate: str) -> str:
	"""Best-effort Item creation for a concept with no canonical item yet.

	Naming: derived from the description to stay readable; uniqueness enforced
	by appending a suffix when a collision occurs.
	"""
	base = _item_code_from(description)
	item_code = base
	suffix = 1
	while frappe.db.exists("Item", item_code):
		suffix += 1
		item_code = f"{base}-{suffix}"

	item_group = frappe.db.get_single_value("Products Settings", "default_item_group") or "All Item Groups"

	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_code,
			"item_name": description.title(),
			"item_group": item_group,
			"stock_uom": stock_uom or "No(s)",
			"is_stock_item": 1,
			"is_purchase_item": 1,
			"include_item_in_manufacturing": 1,
		}
	)
	item.flags.ignore_mandatory = True
	if tax_rate == "16%":
		item.flags.ignore_permissions = True
		item.append(
			"taxes",
			{
				"item_tax_template": _ensure_item_tax_template("16%"),
			},
		)
	item.save(ignore_permissions=True)
	return item.name


def _item_code_from(description: str) -> str:
	key = normalize_description(description)
	# take the first 3 significant tokens to build a compact code
	tokens = [t for t in key.split() if not t.isdigit()]
	prefix = "_".join(tokens[:4])[:36] or "ITEM"
	return prefix


def _ensure_item_tax_template(rate: str) -> str:
	"""Find or create an Item Tax Template for the given rate (16% / 0%).

	Templates are keyed by title + company (erpnext appends the company suffix
	to the generated name), so we match on title.
	"""
	title = f"MX Item IVA {rate}"
	settings = frappe.get_single("CFDI Mapper Settings")
	company = settings.company
	existing = frappe.db.get_value(
		"Item Tax Template", {"title": title, "company": company}, "name"
	)
	if existing:
		return existing
	account = settings.get("iva_acreditable_account")
	if rate == "16%" and not account:
		frappe.throw(
			_("Configura la cuenta de IVA acreditable en CFDI Mapper Settings antes de crear ítems con IVA 16%.")
		)
	doc = frappe.new_doc("Item Tax Template")
	doc.title = title
	doc.company = company
	# a row is mandatory even for the 0% template (rate 0.0)
	doc.append("taxes", {"tax_type": account, "tax_rate": 16.0 if rate == "16%" else 0.0})
	doc.save(ignore_permissions=True)
	return doc.name


def resolve_factor(description: str, stock_uom: str) -> float:
	"""Expose the pack-based factor suggestion server-side."""
	return suggest_factor(description, stock_uom)


def rules_for_supplier(supplier: str) -> list[dict]:
	"""All rules for a supplier (for the management view)."""
	return frappe.db.get_all(
		RULE_LINK,
		filters={"supplier": supplier},
		fields=["name", "description", "item", "is_inventory", "conversion_factor", "stock_uom"],
		order_by="modified desc",
		limit_page_length=0,
	)
