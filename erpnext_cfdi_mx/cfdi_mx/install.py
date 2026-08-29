"""Install hooks: idempotent setup of roles and seed data."""

from __future__ import annotations

import frappe

# Best-effort SAT ClaveUnidad -> ERPNext UOM seeds. The UOM names are the
# frappe/es defaults; users can add their own rows in CFDI UOM Map.
SEED_UOM_MAP = {
	"A51": ("", "Kilogramo"),
	"BB": ("", "Caja"),
	"E48": ("", "Unidad de servicio"),
	"H87": ("No(s)", "Pieza"),
	"KGM": ("Kg", "Kilogramo"),
	"LTR": ("L", "Litro"),
	"MTK": ("m2", "Metro cuadrado"),
	"MTR": ("m", "Metro"),
	"PZA": ("No(s)", "Pieza"),
	"XBX": ("Caja", "Caja"),
}


def _ensure_role() -> None:
	if frappe.db.exists("Role", "CFDI Importer"):
		return
	role = frappe.new_doc("Role")
	role.role_name = "CFDI Importer"
	role.descriptions = "Opera el flujo de compras CFDI (erpnext_cfdi_mx)."
	role.save(ignore_permissions=True)


def _seed_uom_map() -> None:
	# map UOM display names present in the DB to SAT concepts; skip duplicates
	existing = set(
		frappe.db.get_all("CFDI UOM Map", filters={}, pluck="sat_clave") or []
	)
	for clave, (_, sim) in SEED_UOM_MAP.items():
		if clave in existing:
			continue
		uom = frappe.db.get_value("UOM", {"uom_name": sim}, "name") if sim else None
		if not uom:
			# try known ERPNext English/international names
			uom = frappe.db.get_value("UOM", {"uom_name": clave}, "name")
		if not uom:
			continue
		try:
			row = frappe.new_doc("CFDI UOM Map")
			row.sat_clave = clave
			row.erpnext_uom = uom
			row.description = sim
			row.save(ignore_permissions=True)
		except Exception:
			frappe.log_error(title="CFDI seed UOM", message=f"clave={clave} uom={uom}")


def after_install() -> None:
	_ensure_role()
	_seed_uom_map()


def after_app_install() -> None:
	after_install()
