"""Supplier resolution by RFC (Tax ID) using an existing Supplier or auto-create."""

from __future__ import annotations

import frappe
from frappe import _

DEFAULT_SUPPLIER_GROUP = "All Supplier Groups"


def get_supplier_by_rfc(rfc: str) -> str | None:
	"""Look up an existing, enabled Supplier by its SAT RFC (tax_id).

	If a stale duplicate exists (same RFC on several Suppliers), the first
	enabled one wins and a debug-note is logged; the app never guesses between
	duplicates without telling the user.
	"""
	if not rfc:
		return None
	names = frappe.db.get_all(
		"Supplier", filters={"tax_id": rfc, "disabled": 0}, fields=["name"], order_by="creation asc", limit=5
	)
	names = [n["name"] for n in names]
	if not names:
		# maybe tax_id stored with different casing/spaces
		names = frappe.db.get_all(
			"Supplier",
			filters=[["tax_id", "like", f"%{rfc.strip()}%"], ["disabled", "=", 0]],
			fields=["name"],
			limit=5,
		)
		names = [n["name"] for n in names]
	if len(names) > 1:
		frappe.log_error(
			title="CFDI: RFC duplicado en proveedores",
			message=f"RFC {rfc} tiene varios proveedores: {names}. Se usó {names[0]}.",
		)
	return names[0] if names else None


def _default_supplier_group() -> str:
	"""Best supplier group for auto-created suppliers.

	Prefers the standard "All Supplier Groups" root; falls back to whatever
	root group this chart actually has (often localized, e.g. the Spanish
	"Todos los grupos de proveedores").
	"""
	name = frappe.db.get_value(
		"Supplier Group", {"supplier_group_name": "All Supplier Groups"}, "name"
	)
	if name:
		return name
	root = frappe.db.get_all("Supplier Group", filters={"is_group": 1}, pluck="name", limit=1)
	if root:
		return root[0]
	first = frappe.db.get_all("Supplier Group", pluck="name", limit=1)
	if first:
		return first[0]
	frappe.throw(_("No hay ningún grupo de proveedores en el sistema."))


def get_or_create_supplier(rfc: str, name: str) -> str:
	"""Return the existing Supplier for `rfc` or create one from the CFDI emisor data."""
	existing = get_supplier_by_rfc(rfc)
	if existing:
		return existing

	if not rfc:
		raise frappe.ValidationError(_("El CFDI no trae RFC de emisor; no se puede resolver el proveedor."))

	# name fallback: suppliers created without tax_id (rescue + attach the RFC)
	by_name = frappe.db.get_value("Supplier", {"supplier_name": name}, "name")
	if by_name:
		frappe.db.set_value("Supplier", by_name, "tax_id", rfc)
		return by_name

	supplier = frappe.get_doc(
		{
			"doctype": "Supplier",
			"supplier_name": name or rfc,
			"tax_id": rfc,
			"supplier_group": _default_supplier_group(),
			"supplier_type": "Company",
		}
	)
	supplier.flags.ignore_mandatory = True
	supplier.save(ignore_permissions=True)
	# naming: keep whatever naming series the site uses; name may differ from supplier_name
	return supplier.name
