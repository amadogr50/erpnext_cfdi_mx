"""Whitelisted API used by the desk UI (CFDI Recibido + Settings).

Every method here is callable from JavaScript; all of them require either the
"System Manager" role or the app's "CFDI Importer" role.
"""

from __future__ import annotations

import frappe
from frappe import _

from . import import_service, mapper, tax_setup


def _require_importer() -> None:
	if frappe.session.user == "Administrator":
		return
	if "System Manager" in frappe.get_roles() or "CFDI Importer" in frappe.get_roles():
		return
	frappe.throw(_("No tienes permiso para usar el flujo CFDI."), frappe.PermissionError)


@frappe.whitelist()
def analyze(cfdi_name: str) -> dict:
	_require_importer()
	return import_service.analyze(cfdi_name)


@frappe.whitelist()
def preview(cfdi_name: str) -> dict:
	_require_importer()
	return import_service.preview(cfdi_name)


@frappe.whitelist()
def create_rule_from_line(
	cfdi_name: str,
	line_name: str,
	decision: str,
	item: str = "",
	create_item_name: str = "",
	create_item_stock_uom: str = "",
	create_item_tax_rate: str = "0%",
	expense_account: str = "",
	conversion_factor: float = 1.0,
	stock_uom: str = "",
) -> dict:
	"""Resolve one unmapped line and persist its rule.

	decision in {"link", "create", "non_inventory"}:
	  - link:        connect to an existing Item (inventory)
	  - create:      create a new canonical Item + UOM/factor (inventory)
	  - non_inventory: register as expense (expense_account)
	"""
	_require_importer()
	doc = frappe.get_doc("CFDI Recibido", cfdi_name)

	if not doc.supplier:
		raise frappe.ValidationError(_("Resuelve el proveedor antes de mapear líneas."))
	if doc.status == "Registrado":
		raise frappe.ValidationError(_("CFDI ya registrado; no se puede modificar el mapeo."))

	row = next((r for r in doc.items if r.name == line_name), None)
	if not row:
		raise frappe.ValidationError(_("Línea no encontrada."))

	if decision == "non_inventory":
		if not expense_account:
			settings = frappe.get_single("CFDI Mapper Settings")
			expense_account = expense_account or settings.default_expense_account
		if not expense_account:
			raise frappe.ValidationError(
				_("Selecciona una cuenta de gasto (o define la cuenta por defecto en CFDI Mapper Settings).")
			)
		rule = mapper.create_rule(
			doc.emisor_rfc,
			row.description,
			supplier=doc.supplier,
			is_inventory=False,
			expense_account=expense_account,
			clave_prod_serv=row.clave_prod_serv,
			cfdi_unit=row.cfdi_unit,
		)
	elif decision == "link":
		if not item:
			raise frappe.ValidationError(_("Selecciona el Item canónico."))
		rule = mapper.create_rule(
			doc.emisor_rfc,
			row.description,
			supplier=doc.supplier,
			item=item,
			is_inventory=True,
			conversion_factor=conversion_factor or 1.0,
			stock_uom=stock_uom or frappe.db.get_value("Item", item, "stock_uom"),
			tax_rate="0%",  # el IVA real se toma del CFDI línea por línea
			clave_prod_serv=row.clave_prod_serv,
			cfdi_unit=row.cfdi_unit,
		)
	elif decision == "create":
		if not create_item_name:
			raise frappe.ValidationError(_("Define el nombre del Item nuevo."))
		rule = mapper.create_rule(
			doc.emisor_rfc,
			row.description,
			supplier=doc.supplier,
			is_inventory=True,
			# mapper.create_rule auto-creates the Item when item is empty;
			# here we pass the chosen name/uom so the created Item uses them
			item=None,
			conversion_factor=conversion_factor or 1.0,
			stock_uom=create_item_stock_uom,
			tax_rate=create_item_tax_rate,
			clave_prod_serv=row.clave_prod_serv,
			cfdi_unit=row.cfdi_unit,
		)
	else:
		raise frappe.ValidationError(_("Decisión de mapeo inválida."))

	# apply to the row within the CFDI doc
	from .import_service import _apply_rule_to_row

	_apply_rule_to_row(row, rule)
	doc.save(ignore_permissions=True)

	return import_service.preview(cfdi_name)


@frappe.whitelist()
def confirm(cfdi_name: str, receive_stock: str = "1") -> dict:
	_require_importer()
	return import_service.confirm_and_register(cfdi_name, receive_stock=receive_stock in ("1", "true", "True"))


@frappe.whitelist()
def setup_tax(company: str = "") -> dict:
	_require_importer()
	return tax_setup.setup_mexico_tax(company or None)


@frappe.whitelist()
def suggest_factor(description: str, stock_uom: str = "") -> float:
	return mapper.resolve_factor(description, stock_uom)


@frappe.whitelist()
def supplier_options() -> list[dict]:
	_require_importer()
	return frappe.db.get_all("Supplier", fields=["name", "supplier_name", "tax_id"], order_by="supplier_name asc")
