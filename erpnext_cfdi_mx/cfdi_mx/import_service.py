"""Core import pipeline: parse XML -> preview lines with mapping states
-> (on confirm) create Purchase Receipt + Purchase Invoice + Payment Entry.

All document creation is atomic per CFDI: if any step fails, the CFDI is
marked "Error" and nothing partial is left submitted.
"""

from __future__ import annotations

import os
from decimal import Decimal, ROUND_HALF_UP

import frappe
from frappe import _

from . import mapper, supplier
from .normalizer import normalize_description
from .parser import CFDIValidationError, parse_cfdi

# Mapping states used on the CFDI Recibido Item child table.
STATE_UNMAPPED = "Sin Regla"
STATE_MAPPED = "Mapeado"
STATE_NON_INVENTORY = "No Inventariable"


class PipelineError(frappe.ValidationError):
	pass


def _q(v):
	"""Decimal round to 2 places."""
	return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _read_xml_bytes(file_url: str) -> bytes:
	"""Resolve a CFDI XML attachment to bytes.

	Prefers the registered File record; falls back to direct filesystem reads
	under public/ and private/ so the flow also works in tests / after backups
	where the file registry may lag behind the disk.
	"""
	# 1) registered File record
	try:
		file_doc = frappe.get_doc("File", {"file_url": file_url})
		path = file_doc.get_full_path()
		if path and os.path.exists(path):
			with open(path, "rb") as f:
				return f.read()
	except (frappe.DoesNotExistError, StopIteration, OSError):
		pass

	# 2) direct site filesystem
	rel = (file_url or "").lstrip("/")
	if rel.startswith("files/"):
		rel = rel[len("files/") :]
	for folder in ("public", "private"):
		for cand in (
			frappe.get_site_path(folder, "files", rel),
			frappe.get_site_path(folder, rel),
		):
			if cand and os.path.exists(cand):
				with open(cand, "rb") as f:
					return f.read()

	raise PipelineError(_("No se pudo localizar el archivo XML adjunto ({0}).").format(file_url))


def analyze(cfdi_name: str) -> dict:
	"""Parse + structure a CFDI Recibido and (re)populate its item rows.

	Used both at upload time and as a `Reprocesar` action. Never touches GL.
	"""
	doc = frappe.get_doc("CFDI Recibido", cfdi_name)
	if not doc.xml_file:
		frappe.throw(_("Sube el archivo XML antes."))

	try:
		xml_bytes = _read_xml_bytes(doc.xml_file)
	except PipelineError as e:
		doc.status = "Error"
		doc.data_ok = 0
		doc.validation_message = str(e)
		doc.save(ignore_permissions=True)
		raise

	try:
		result = parse_cfdi(xml_bytes)
	except CFDIValidationError as e:
		doc.status = "Error"
		doc.data_ok = 0
		doc.validation_message = str(e)
		doc.save(ignore_permissions=True)
		raise PipelineError(str(e)) from e

	data = result.data
	doc.db_set(
		{
			"uuid": data["timbre"].get("uuid", ""),
			"version_cfdi": data["version"],
			"serie": data["serie"],
			"folio": data["folio"],
			"fecha_emision": data["fecha_emision"],
			"fecha_timbrado": data["timbre"].get("fecha_timbrado", ""),
			"tipo_comprobante": data["tipo_comprobante"],
			"metodo_pago": data["metodo_pago"],
			"forma_pago": data["forma_pago"],
			"moneda": data["moneda"],
			"tipo_cambio": data["tipo_cambio"],
			"subtotal": data["subtotal"],
			"descuento": data["descuento"],
			"total": data["total"],
			"emisor_rfc": data["emisor"].get("rfc", ""),
			"emisor_nombre": data["emisor"].get("nombre", ""),
			"emisor_regimen": data["emisor"].get("regimen", ""),
			"receptor_rfc": data["receptor"].get("rfc", ""),
			"receptor_nombre": data["receptor"].get("nombre", ""),
			"uso_cfdi": data["receptor"].get("uso_cfdi", ""),
		}
	)
	doc.iva_trasladado = _q(sum(t["importe"] for t in data["impuestos"]["trasladados"]))
	doc.retenciones = _q(sum(t["importe"] for t in data["impuestos"]["retenciones"]))

	# Duplicate UUID guard
	dup = frappe.db.get_value(
		"CFDI Recibido", {"uuid": doc.uuid, "name": ("!=", doc.name)}, "name"
	)
	doc.is_duplicate = 1 if dup else 0

	# Resolve supplier
	supplier_name = None
	if doc.emisor_rfc:
		try:
			supplier_name = supplier.get_or_create_supplier(doc.emisor_rfc, doc.emisor_nombre)
		except frappe.ValidationError:
			supplier_name = None
	doc.supplier = supplier_name

	doc.data_ok = 1 if (result.ok and not doc.is_duplicate) else 0
	doc.validation_message = "\n".join(result.errors + result.warnings)
	if result.errors:
		doc.status = "Error"
		doc.validation_message = "\n".join(result.errors)
	else:
		doc.status = "En Revisión"

	doc.items = []
	for c in data["conceptos"]:
		row = _build_item_child(c)
		# attempt automatic mapping
		rule = (
			mapper.find_rule(doc.emisor_rfc, c["descripcion"], doc.supplier)
			if doc.supplier
			else None
		)
		if rule:
			_apply_rule_to_row(row, rule)
		doc.append("items", row)

	doc.save(ignore_permissions=True)
	return _preview_payload(doc)


def _build_item_child(c: dict) -> dict:
	"""Child row from a parsed concept (before any mapping)."""
	# per-line tax: IVA trasladado + retenciones del concepto
	iva = [t for t in c.get("traslados", []) if t.get("impuesto") == "002"]
	isr = [t for t in c.get("retenciones", []) if t.get("impuesto") in ("001", "019")]
	iva_ret = [t for t in c.get("retenciones", []) if t.get("impuesto") == "002"]

	return {
		"clave_prod_serv": c.get("clave_prod_serv", ""),
		"description": c.get("descripcion", ""),
		"cfdi_unit": c.get("clave_unidad", ""),
		"sat_unit_label": c.get("unidad", ""),
		"quantity": c.get("cantidad", 0),
		"unit_value": c.get("valor_unitario", 0),
		"amount": c.get("importe", 0),
		"iva_rate": _q(iva[0]["tasa"] * 100) if iva else 0,
		"iva_amount": _q(sum(t["importe"] for t in iva)),
		"isr_rate": _q(isr[0]["tasa"] * 100) if isr else 0,
		"isr_amount": _q(sum(t["importe"] for t in isr)),
		"iva_ret_rate": _q(iva_ret[0]["tasa"] * 100) if iva_ret else 0,
		"iva_ret_amount": _q(sum(t["importe"] for t in iva_ret)),
		"mapping_status": STATE_UNMAPPED,
	}


def _apply_rule_to_row(row, rule: dict) -> None:
	"""Fill a child row from a matched CFDI Item Rule.

	row is either a plain dict (analyze path, pre-append) or a Frappe child
	Document (create_rule_from_line path) — both are supported.
	"""

	def _set(key: str, value) -> None:
		if isinstance(row, dict):
			row[key] = value
		else:
			setattr(row, key, value)

	def _get(key: str):
		return row.get(key) if isinstance(row, dict) else getattr(row, key)

	_set("rule", rule["name"])
	_set("mapping_status", STATE_MAPPED if rule["is_inventory"] == 1 else STATE_NON_INVENTORY)
	_set("item", rule.get("item"))
	_set("is_stock_item", 1 if rule["is_inventory"] == 1 else 0)
	if rule["is_inventory"] == 1 and rule.get("item"):
		_set("stock_uom", rule.get("stock_uom"))
		cf = rule.get("conversion_factor") or 1.0
		_set("conversion_factor", cf)
		_set("stock_quantity", _q(_get("quantity") * cf))
	else:
		_set("expense_account", rule.get("expense_account"))


def preview(cfdi_name: str) -> dict:
	"""Server-side preview: recalculates per-line stock quantities from rules."""
	doc = frappe.get_doc("CFDI Recibido", cfdi_name)
	return _preview_payload(doc)


def _preview_payload(doc) -> dict:
	lines = []
	requires_mapping = False
	for row in doc.items:
		lines.append(
			{
				"name": row.name,
				"idx": row.idx,
				"description": row.description,
				"quantity": row.quantity,
				"cfdi_unit": row.cfdi_unit,
				"amount": row.amount,
				"mapping_status": row.mapping_status,
				"item": row.item,
				"stock_uom": row.stock_uom,
				"stock_quantity": row.stock_quantity,
				"rule": row.rule,
			}
		)
		if row.mapping_status == STATE_UNMAPPED:
			requires_mapping = True

	return {
		"cfdi": doc.name,
		"status": doc.status,
		"supplier": doc.supplier,
		"supplier_name": doc.emisor_nombre,
		"supplier_rfc": doc.emisor_rfc,
		"subtotal": doc.subtotal,
		"descuento": doc.descuento,
		"total": doc.total,
		"moneda": doc.moneda,
		"lines": lines,
		"requires_mapping": requires_mapping,
		"data_ok": doc.data_ok,
	}


def confirm_and_register(cfdi_name: str, receive_stock: bool = True) -> dict:
	"""Create PR + PI + PE for a CFDI whose lines are all mapped/resolved."""
	doc = frappe.get_doc("CFDI Recibido", cfdi_name)
	if doc.status == "Registrado":
		raise PipelineError(_("Este CFDI ya fue registrado."))
	if not doc.data_ok or doc.is_duplicate:
		raise PipelineError(_("El CFDI no pasó la validación (data_ok={0}, duplicado={1}).").format(doc.data_ok, doc.is_duplicate))
	if not doc.supplier:
		raise PipelineError(_("Falta el proveedor."))

	unmapped = [r for r in doc.items if r.mapping_status == STATE_UNMAPPED]
	if unmapped:
		raise PipelineError(
			_(f"Hay {len(unmapped)} concepto(s) sin regla de mapeo. Resuélvelos en el preview antes de verificar.")
		)

	settings = frappe.get_single("CFDI Mapper Settings")
	company = settings.company
	if not company or not settings.default_warehouse:
		raise PipelineError(_("Configura CFDI Mapper Settings (compañía y almacén de recepción) primero."))

	total = doc.total
	inventory_lines = [r for r in doc.items if r.mapping_status == STATE_MAPPED]
	expense_lines = [r for r in doc.items if r.mapping_status == STATE_NON_INVENTORY]

	purchase_receipt = None
	if receive_stock and inventory_lines:
		purchase_receipt = _create_purchase_receipt(doc, inventory_lines, settings)

	purchase_invoice = _create_purchase_invoice(doc, doc.items, settings)
	payment_entry = _create_payment_entry(doc, settings, purchase_invoice, total)

	doc.db_set(
		{
			"status": "Registrado",
			"purchase_receipt": purchase_receipt or "",
			"purchase_invoice": purchase_invoice,
			"payment_entry": payment_entry,
		}
	)
	return {
		"cfdi": doc.name,
		"purchase_receipt": purchase_receipt,
		"purchase_invoice": purchase_invoice,
		"payment_entry": payment_entry,
		"total": total,
	}


# ---------------------------------------------------------------------------
# Document builders
# ---------------------------------------------------------------------------

def _purchase_uom(r) -> str:
	"""UOM used on the purchase-side line (qty is the CFDI's selling unit).

	Prefers the CFDI's SAT unit label (e.g. 'Paquete'), creating the UOM
	master row when missing; falls back to the stock UOM only when the
	conversion factor is 1 (qty is already in stock units).
	"""
	if (r.conversion_factor or 1.0) != 1.0:
		for cand in (getattr(r, "sat_unit_label", None), getattr(r, "cfdi_unit", None)):
			if cand:
				if not frappe.db.exists("UOM", cand):
					uom = frappe.get_doc(
						{"doctype": "UOM", "uom_name": cand, "must_be_whole_number": 0}
					)
					uom.flags.ignore_mandatory = True
					uom.save(ignore_permissions=True)
				return cand
	return r.stock_uom or "Units"


def _create_purchase_receipt(doc, lines, settings) -> str:
	pr = frappe.new_doc("Purchase Receipt")
	pr.supplier = doc.supplier
	pr.company = settings.company
	pr.currency = doc.moneda
	if doc.moneda != "MXN":
		pr.conversion_rate = doc.tipo_cambio or 1.0
	pr.posting_date = _date_from(doc)
	pr.set_posting_time = 1
	pr.remarks = f"CFDI {doc.serie or ''}{doc.folio or ''} - {doc.uuid}"

	for r in lines:
		item_code = r.item
		if not item_code:
			raise PipelineError(_("Línea '{0}' mapeada sin Item canónico.").format(r.description))
		warehouse = settings.default_warehouse or frappe.db.get_value("Item", item_code, "default_warehouse")
		factor = r.conversion_factor or 1.0
		pr.append(
			"items",
			{
				"item_code": item_code,
				"item_name": r.description,
				"qty": r.quantity,
				"uom": _purchase_uom(r),
				"stock_uom": r.stock_uom,
				"conversion_factor": factor,
				"rate": r.unit_value,
				"amount": _q(r.quantity * r.unit_value),
				"warehouse": warehouse,
				"cost_center": _default_cost_center(settings.company),
			},
		)
	pr.flags.ignore_validate = False
	pr.insert(ignore_mandatory=True)
	pr.submit()
	return pr.name


def _create_purchase_invoice(doc, lines, settings) -> str:
	pi = frappe.new_doc("Purchase Invoice")
	pi.supplier = doc.supplier
	pi.company = settings.company
	pi.currency = doc.moneda
	if doc.moneda != "MXN":
		pi.conversion_rate = doc.tipo_cambio or 1.0
	pi.bill_no = f"{doc.serie or ''}{doc.folio or ''}".strip() or doc.uuid
	pi.bill_date = _date_from(doc)
	pi.posting_date = _date_from(doc)
	pi.set_posting_time = 1
	pi.remark = f"CFDI {pi.bill_no} UUID {doc.uuid}"
	pi.remarks = pi.remark
	pi.is_return = 0
	pi.update_stock = 0  # stock is recorded by the Purchase Receipt
	# CFDI discount is header-level. ERPNext's header discount re-distributes per
	# line with rounding (can drift cents) — instead we fold the discount into
	# the unit rates so net_total/grand_total match the XML exactly.
	disc_shares = _distribute_discount(lines, doc.descuento)

	for r in lines:
		share = disc_shares.get(r.name, 0.0)
		if r.mapping_status == STATE_MAPPED:
			factor = r.conversion_factor or 1.0
			purchase_qty = r.quantity or 1.0
			net_amt = _q(r.amount - share)  # exact after discount
			rate = net_amt / purchase_qty  # price per purchase (CFDI) unit
			row = {
				"item_code": r.item,
				"item_name": r.description,
				"qty": purchase_qty,
				"uom": _purchase_uom(r),
				"stock_uom": r.stock_uom,
				"conversion_factor": factor,
				"rate": rate,
				"amount": _q(purchase_qty * rate),
				"cost_center": _default_cost_center(settings.company),
			}
			pi.append("items", row)
		else:
			item_code = _expense_item(r)
			net_amt = _q(r.amount - share)
			row = {
				"item_code": item_code,
				"item_name": r.description,
				"qty": 1,
				"rate": net_amt,
				"amount": net_amt,
				"cost_center": _default_cost_center(settings.company),
			}
			if r.expense_account:
				row["expense_account"] = r.expense_account
			pi.append("items", row)

	# Cent-exact reconciliation: the child-table rate is fixed at 2dp and the
	# server recomputes amount = qty*rate, so spreading a header discount over
	# rates can drift cents. Simulate the server arithmetic and add one explicit
	# non-stock "rounding adjustment" line for the residual (delta >= 1 cent).
	tax_total = _q(
		sum(r.iva_amount for r in lines)
		- sum((r.isr_amount or 0) + (r.iva_ret_amount or 0) for r in lines)
	)
	server_total = _q(
		sum(_q(_q(i.qty or 0) * _q(i.rate or 0)) for i in pi.items) + tax_total
	)
	delta = _q((doc.total or 0) - server_total)
	if abs(delta) >= 0.01:
		# Prefer an exact seat: a qty=1 (non-inventory) line takes the delta in
		# its rate — stays positive, no Selling-Settings flag needed.
		seat = next((i for i in pi.items if (i.qty or 0) == 1), None)
		if seat and (seat.rate + delta) > 0:
			seat.rate = _q(seat.rate + delta)
			seat.amount = seat.rate
		elif delta > 0:
			adj_code = _expense_item_described("Ajuste redondeo CFDI")
			pi.append(
				"items",
				{
					"item_code": adj_code,
					"item_name": "Ajuste de redondeo CFDI",
					"qty": 1,
					"rate": _q(delta),
					"amount": _q(delta),
					"expense_account": _default_expense_account(settings.company),
					"cost_center": _default_cost_center(settings.company),
				},
			)
		else:
			# Negative delta without a qty=1 seat: absorb it into the line with
			# the largest amount (distribute per its qty), keeping rates positive.
			# Retry once more with the same target if the rate rounding drifts again.
			target = max(pi.items, key=lambda i: (i.amount or 0))
			absorbed = False
			for attempt in range(3):
				qty = target.qty or 1
				rate_adj = _q(delta / qty)
				if (target.rate + rate_adj) > 0:
					target.rate = _q(target.rate + rate_adj)
					target.amount = _q(qty * target.rate)
					new_server = _q(
						sum(_q(_q(i.qty or 0) * _q(i.rate or 0)) for i in pi.items) + tax_total
					)
					delta = _q((doc.total or 0) - new_server)
					if abs(delta) < 0.01:
						absorbed = True
						break
				else:
					break
			if not absorbed:
				raise PipelineError(
					_(
						"El CFDI queda {0:+.2f} por debajo de sus líneas (redondeo). "
						"Revisa las reglas de conversión de esta factura o ajusta un rate manualmente."
					).format(delta)
				)

	_add_tax_rows(pi, doc, settings)
	pi.flags.ignore_mandatory = True
	pi.insert()
	pi.submit()
	return pi.name


def _expense_item_described(description: str) -> str:
	"""Item (non-stock) reused by normalized description for expense lines."""
	key = normalize_description(description)
	item_code = f"CFDI-{key.replace(' ', '-')[:40]}" or "CFDI-NC"
	if frappe.db.exists("Item", item_code):
		return item_code
	item = frappe.new_doc("Item")
	item.item_code = item_code
	item.item_name = description.title()
	item.item_group = "All Item Groups"
	item.is_stock_item = 0
	item.is_purchase_item = 1
	item.stock_uom = "No(s)"
	item.flags.ignore_mandatory = True
	item.save(ignore_permissions=True)
	return item.name


def _expense_item(r) -> str:
	"""Item (non-stock) used for a 'no inventariable' line. Reuses by normalized description."""
	return _expense_item_described(r.description)


def _default_expense_account(company: str) -> str:
	"""App-configured expense account, falling back to the company default."""
	settings = frappe.get_single("CFDI Mapper Settings")
	if settings.get("default_expense_account"):
		return settings.default_expense_account
	return frappe.db.get_value("Company", company, "default_expense_account") or ""


def _distribute_discount(lines, discount: float) -> dict:
	"""Proportional per-line shares of the CFDI header discount (cents exact)."""
	shares: dict = {}
	total_desc = _q(discount or 0)
	if not total_desc:
		return shares
	amounts = [(_q(r.amount), r.name) for r in lines]
	total = sum(a for a, _ in amounts) or 1.0
	remaining = total_desc
	for i, (amt, name) in enumerate(amounts):
		if i < len(amounts) - 1:
			share = _q(amt * total_desc / total)
			remaining -= share
		else:
			share = _q(remaining)  # absorb rounding drift so the sum is exact
		shares[name] = share
	return shares


def _add_tax_rows(pi, doc, settings) -> None:
	"""Add exact tax rows (Actual) so the invoice totals equal the XML exactly."""
	iva = 0.0
	isr = 0.0
	iva_ret = 0.0
	for r in doc.items:
		iva += r.iva_amount
		isr += r.isr_amount
		iva_ret += r.iva_ret_amount

	if iva:
		pi.append(
			"taxes",
			{
				"charge_type": "Actual",
				"account_head": settings.iva_acreditable_account,
				"tax_amount": _q(iva),
				"description": "IVA trasladado (CFDI)",
			},
		)
	if isr:
		pi.append(
			"taxes",
			{
				"charge_type": "Actual",
				"account_head": settings.isr_retention_account,
				"tax_amount": -_q(isr),
				"description": "ISR retenido (CFDI)",
			},
		)
	if iva_ret:
		pi.append(
			"taxes",
			{
				"charge_type": "Actual",
				"account_head": settings.iva_retention_account,
				"tax_amount": -_q(iva_ret),
				"description": "IVA retenido (CFDI)",
			},
		)


def _create_payment_entry(doc, settings, purchase_invoice: str, total: float) -> str:
	"""Payment Entry that settles the Purchase Invoice (PUE = already paid)."""
	payment_account, mode = _resolve_payment_account(doc, settings)
	pe = frappe.new_doc("Payment Entry")
	pe.payment_type = "Pay"
	pe.party_type = "Supplier"
	pe.party = doc.supplier
	pe.company = settings.company
	payable = frappe.db.get_value(
		"Supplier Account", {"parent": doc.supplier, "company": settings.company}, "account"
	)
	if not payable:
		payable = frappe.db.get_value("Company", settings.company, "default_payable_account")
	pe.paid_to = payable
	if not pe.paid_to:
		raise PipelineError(_("Falta la cuenta por pagar del proveedor {0}.").format(doc.supplier))
	pe.paid_from = payment_account
	pe.paid_amount = total
	pe.received_amount = total
	pe.reference_no = f"{doc.serie or ''}{doc.folio or ''}".strip() or doc.uuid
	pe.reference_date = _date_from(doc)
	if mode:
		pe.mode_of_payment = mode
	# read the invoice's live outstanding (it is authoritative, not the XML total)
	live_outstanding = float(
		frappe.db.get_value("Purchase Invoice", purchase_invoice, "outstanding_amount") or total
	)
	allocated = min(total, live_outstanding)
	pe.append(
		"references",
		{
			"reference_doctype": "Purchase Invoice",
			"reference_name": purchase_invoice,
			"total_amount": live_outstanding,
			"outstanding_amount": live_outstanding,
			"allocated_amount": allocated,
		},
	)
	pe.flags.ignore_mandatory = True
	pe.insert()
	pe.submit()
	return pe.name


def _resolve_payment_account(doc, settings):
	"""Per-supplier account row > bridge > error."""
	mode = None
	for row in settings.payment_accounts:
		if row.supplier == doc.supplier:
			return row.payment_account, row.mode_of_payment
	if settings.use_bridge_account and settings.bridge_account:
		return settings.bridge_account, None
	# baseless default exists in ERPNext too
	raise PipelineError(
		_("No se resolvió cuenta de pago para {0}: configura 'Cuenta puente' o una fila por proveedor en CFDI Mapper Settings.").format(
			doc.supplier
		)
	)


def _date_from(doc):
	import datetime

	raw = doc.fecha_emision or None
	if not raw:
		return datetime.date.today()
	try:
		if isinstance(raw, str):
			raw = raw.replace("T", " ")[:19]
			return datetime.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").date()
		if hasattr(raw, "date"):
			return raw.date()
		return raw
	except ValueError:
		return datetime.date.today()


def _default_cost_center(company: str) -> str:
	cc = frappe.db.get_value("Company", company, "cost_center")
	if cc:
		return cc
	cost_center = frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")
	if not cost_center:
		cost_center = frappe.db.get_value("Cost Center", {"is_group": 0}, "name")
	return cost_center or ""
