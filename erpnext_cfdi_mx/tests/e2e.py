"""End-to-end test of the CFDI import pipeline, runnable on any bench site.

Usage (inside the bench container):
    bench --site cfdi-test.local execute erpnext_cfdi_mx.tests.e2e.run

The test is self-contained: it creates a minimal Mexican company + accounts
inside the site, imports every CFDI XML found under /tmp/cfdi-fixtures/, maps
all lines (auto-creating canonical items and rules), registers the purchases,
and validates that the posted documents match the XML amounts exactly.
"""

from __future__ import annotations

import glob
import os

import frappe

FIXTURES = "/tmp/cfdi-fixtures"


def ensure_uom(name: str) -> str:
	"""A fresh site without the setup wizard has zero UOMs — create what we need."""
	existing = frappe.db.get_value("UOM", {"uom_name": name}, "name")
	if existing:
		return existing
	uom = frappe.get_doc({"doctype": "UOM", "uom_name": name, "must_be_whole_number": 0})
	uom.flags.ignore_mandatory = True
	uom.save(ignore_permissions=True)
	return uom.name


def _reset_company(name: str) -> None:
	"""Remove a half-created company and everything tied to it (idempotent re-runs).

	Uses direct SQL: this runs only on the disposable E2E site, where deleting
	through the document layer fights sub-tree/link rules and can leave the
	chart-of-accounts half created. Runs unconditionally: a previous failed
	run can leave orphan accounts with no Company at all.
	"""
	for table in (
		"GL Entry",
		"Stock Ledger Entry",
		"Account",
		"Warehouse",
		"Cost Center",
		"Supplier",
		"Item",
		"Purchase Invoice",
		"Purchase Receipt",
		"Payment Entry",
	):
		if frappe.db.has_column(table, "company"):
			frappe.db.sql(f"DELETE FROM `tab{table}` WHERE `company`=%s", name)
	# child tables carry no company column — the E2E site is disposable, drop them all
	for table in (
		"tabPurchase Invoice Item",
		"tabPurchase Receipt Item",
		"tabPayment Entry Reference",
	):
		frappe.db.sql(f"DELETE FROM `{table}`", [])
	frappe.db.sql("DELETE FROM `tabCompany` WHERE `name`=%s", name)
	# tax templates created by the app's setup for this company
	for dt in ("Purchase Taxes and Charges Template", "Item Tax Template"):
		if frappe.db.has_column(dt, "company"):
			frappe.db.sql(f"DELETE FROM `tab{dt}` WHERE `company`=%s", name)
	frappe.db.sql("DELETE FROM `tabPurchase Taxes and Charges`", [])
	frappe.db.sql("DELETE FROM `tabItem Tax`", [])
	# CFDI doctype tables (Single tables are born on first save — guard each delete)
	for t in (
		"CFDI Payment Account",
		"CFDI Mapper Settings",
		"CFDI Item Rule",
		"CFDI UOM Map",
		"CFDI Recibido Item",
		"CFDI Recibido",
	):
		if frappe.db.table_exists(t):
			frappe.db.sql(f"DELETE FROM `tab{t}`", [])
	# leftover suppliers/items created for E2E fixtures
	frappe.db.sql(
		"DELETE FROM `tabSupplier` WHERE `tax_id` IN (%s, %s, %s)",
		("AAA010101AAA", "BBB020202BBB", "CCC030303CCC"),  # fixture-only RFCs (not real taxpayer IDs)
	)
	frappe.db.sql("DELETE FROM `tabItem` WHERE `item_code` LIKE 'E2E-%' OR `item_code` LIKE 'CFDI-%'")
	frappe.db.commit()


def _seed_fiscal_years() -> None:
	"""Fresh sites have no Fiscal Year; stock posting requires one."""
	for year in (2026, 2027, 2028):
		if frappe.db.exists("Fiscal Year", {"year_start_date": f"{year}-01-01"}):
			continue
		frappe.get_doc(
			{
				"doctype": "Fiscal Year",
				"year": f"FY {year}",
				"year_start_date": f"{year}-01-01",
				"year_end_date": f"{year}-12-31",
			}
		).insert(ignore_permissions=True)


def _seed_groups() -> None:
	"""Fresh sites lack the standard root groups the wizard would create."""
	if not frappe.db.exists("Supplier Group", "All Supplier Groups"):
		frappe.get_doc(
			{"doctype": "Supplier Group", "supplier_group_name": "All Supplier Groups"}
		).insert(ignore_permissions=True)
	if not frappe.db.exists("Item Group", "All Item Groups"):
		frappe.get_doc(
			{"doctype": "Item Group", "item_group_name": "All Item Groups"}
		).insert(ignore_permissions=True)


def _mk_account(company, name, account_type, parent, is_group=0):
	"""Create an account under the given parent (idempotent within a fresh reset)."""
	payload = {
		"doctype": "Account",
		"account_name": name,
		"parent_account": parent,
		"account_type": account_type,
		"company": company,
		"is_group": is_group,
	}
	if not parent and account_type:
		payload["root_type"] = account_type  # root accounts need their root type
		payload["account_type"] = None  # root_type is the axis; account_type comes from the allowed set
	acc = frappe.get_doc(payload)
	acc.flags.ignore_mandatory = True
	acc.save(ignore_permissions=True)
	return acc.name


def setup_minimal_company() -> dict:
	"""Create a minimal MX-flavored accounting tree by hand.

	The Standard chart-of-accounts import is avoided: create_default_accounts
	on this erpnext version raises DuplicateEntryError on 'Application of Funds
	(Assets)' during chart import, so we build the few accounts the pipeline
	actually needs (cash, VAT, tax retentions, payables, expenses).
	"""
	_reset_company("Black Brûlée E2E")
	_seed_groups()
	_seed_fiscal_years()

	# fresh sites lack the standard warehouse types ERPNext expects
	for wt in ("Transit", "Stores", "Maintenance"):
		if not frappe.db.exists("Warehouse Type", wt):
			frappe.get_doc({"doctype": "Warehouse Type", "name": wt}).insert(ignore_permissions=True)

	company_doc = frappe.new_doc("Company")
	company_doc.company_name = "Black Brûlée E2E"
	company_doc.abbr = "BE2E"
	company_doc.country = "Mexico"
	company_doc.default_currency = "MXN"
	company_doc.enable_perpetual_inventory = 1
	company_doc.flags.ignore_mandatory = True
	company_doc.save(ignore_permissions=True)

	# minimal tree (suffix bank matches company abbr)
	company = company_doc.name
	suf = "BE2E"
	assets = _mk_account(company, f"Assets - {suf}", "Asset", "", 1)
	liab = _mk_account(company, f"Liabilities - {suf}", "Liability", "", 1)
	equity = _mk_account(company, f"Equity - {suf}", "Equity", "", 1)
	expense = _mk_account(company, f"Expenses - {suf}", "Expense", "", 1)
	income = _mk_account(company, f"Income - {suf}", "Income", "", 1)

	cash = _mk_account(company, f"Caja - {suf}", "Cash", assets)
	tax_assets = _mk_account(company, f"Impuestos por cobrar - {suf}", None, assets, 1)
	iva_acred = _mk_account(company, f"IVA ACREDITABLE - {suf}", "Tax", tax_assets)
	isr_ret = _mk_account(company, f"ISR RETENIDO - {suf}", "Tax", tax_assets)
	iva_ret = _mk_account(company, f"IVA RETENIDO - {suf}", "Tax", tax_assets)
	payable = _mk_account(company, f"Accounts Payable - {suf}", "Payable", liab)
	gastos = _mk_account(company, f"Gastos - {suf}", "Expense Account", expense)
	ventas = _mk_account(company, f"Ventas - {suf}", "Income Account", income)
	discount_received = _mk_account(company, f"Discount Received - {suf}", "Expense Account", expense)

	# cost center tree: the root carries the company name (erpnext rule), then one leaf
	root_cc = frappe.db.get_value("Cost Center", {"company": company, "is_group": 1}, "name")
	if not root_cc:
		root_doc = frappe.get_doc(
			{
				"doctype": "Cost Center",
				"cost_center_name": company,
				"company": company,
				"is_group": 1,
				"parent_cost_center": "",
			}
		)
		root_doc.flags.ignore_mandatory = True
		root_doc.save(ignore_permissions=True)
		root_cc = root_doc.name
	cost_center = frappe.get_doc(
		{
			"doctype": "Cost Center",
			"cost_center_name": f"E2E - {suf}",
			"company": company,
			"parent_cost_center": root_cc,
		}
	)
	cost_center.flags.ignore_mandatory = True
	cost_center.save(ignore_permissions=True)

	# point the company at the pieces it needs
	company_doc.reload()
	company_doc.default_payable_account = payable
	company_doc.default_expense_account = gastos
	company_doc.default_income_account = ventas
	company_doc.default_discount_account = discount_received
	company_doc.cost_center = cost_center.name
	company_doc.create_default_warehouses()  # needs Warehouse Types (seeded above)
	company_doc.save(ignore_permissions=True)
	company_doc.flags.ignore_permissions = True

	cfg = {
		"company": company,
		"warehouse": frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name"),
		"payable": payable,
		"cash": cash,
		"IVA ACREDITABLE": iva_acred,
		"ISR RETENIDO": isr_ret,
		"IVA RETENIDO": iva_ret,
		"expense": gastos,
	}

	# settings: point the app at our infrastructure
	settings = frappe.get_single("CFDI Mapper Settings")
	settings.company = company
	settings.default_warehouse = cfg["warehouse"]
	settings.use_bridge_account = 1
	settings.bridge_account = cash
	settings.iva_acreditable_account = iva_acred
	settings.isr_retention_account = isr_ret
	settings.iva_retention_account = iva_ret
	settings.default_expense_account = gastos
	settings.save(ignore_permissions=True)

	# run the one-click tax setup against the hand-made accounts
	from erpnext_cfdi_mx.cfdi_mx import tax_setup

	res = tax_setup.setup_mexico_tax(company)
	print("  [setup fiscal]", res["summary"].replace("\n", " | "))
	return cfg


def _ensure_item(code: str, stock: bool, uom: str) -> str:
	if frappe.db.exists("Item", code):
		return code
	item = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": "All Item Groups",
			"stock_uom": uom,
			"is_stock_item": 1 if stock else 0,
			"is_purchase_item": 1,
		}
	)
	item.flags.ignore_mandatory = True
	item.save(ignore_permissions=True)
	return item.name


def run():
	from erpnext_cfdi_mx.cfdi_mx import import_service, mapper

	print("=" * 70)
	print("E2E CFDI import — self-contained site test")
	print("=" * 70)

	assert os.path.isdir(FIXTURES), f"fixtures dir missing: {FIXTURES} (docker cp ~/cfdi-fixtures/*.xml {FIXTURES}/)"
	fixtures = sorted(glob.glob(f"{FIXTURES}/*.xml"))
	assert fixtures, "no XML fixtures"

	cfg = setup_minimal_company()
	uom_pcs = ensure_uom("No(s)")
	uom_kg = ensure_uom("Kg")
	ensure_uom("L")

	for path in fixtures:
		fname = os.path.basename(path)
		print(f"\n--- {fname} ---")

		# copy the fixture into the site's public files so the file_url works
		dest_dir = frappe.get_site_path("public", "files", "fixtures")
		os.makedirs(dest_dir, exist_ok=True)
		dest_path = os.path.join(dest_dir, fname)
		if not os.path.exists(dest_path):
			import shutil

			shutil.copy2(path, dest_path)
		file_url = f"/files/fixtures/{fname}"

		doc = frappe.new_doc("CFDI Recibido")
		doc.xml_file = file_url
		doc.save(ignore_permissions=True)
		print(f"  creado: {doc.name}")
		import_service.analyze(doc.name)
		doc.reload()
		if doc.is_duplicate:
			print(f"  duplicado (UUID ya registrado) — skip")
			continue
		print(f"  analyze: status={doc.status} data_ok={doc.data_ok} líneas={len(doc.items)} "
			f"supplier={doc.supplier} total={doc.total}")
		assert doc.data_ok, f"data_ok false: {doc.validation_message}"
		assert not doc.is_duplicate

		# resolve every unmapped line: alternate inventory (auto item) and expense
		needs = [r for r in doc.items if r.mapping_status == "Sin Regla"]
		for i, row in enumerate(needs):
			code = f"E2E-{i}-{row.name[:24] or 'x'}"[:40]
			if i % 3 == 2:
				# every third line: expense (non-inventory)
				mapper.create_rule(
					doc.emisor_rfc, row.description, supplier=doc.supplier,
					is_inventory=False, expense_account=cfg["expense"],
				)
			else:
				item = _ensure_item(code, True, uom_pcs if i % 2 == 0 else uom_kg)
				mapper.create_rule(
					doc.emisor_rfc, row.description, supplier=doc.supplier,
					item=item, is_inventory=True,
					conversion_factor=2.0, stock_uom=uom_pcs if i % 2 == 0 else uom_kg,
				)
			print(f"  regla creada: {row.description[:40]}…")

		# re-analyze so rules apply to the child rows
		import_service.analyze(doc.name)
		doc.reload()
		unmapped = [r for r in doc.items if r.mapping_status == "Sin Regla"]
		assert not unmapped, f"quedaron líneas sin mapear: {len(unmapped)}"

		# register
		try:
			res = import_service.confirm_and_register(doc.name, receive_stock=True)
		except Exception as exc:
			# diagnostic dump: PI state + PE references
			diag_pi = pi_cur = None
			pe_draft = []
			try:
				diag_pi = frappe.db.get_value(
					"Purchase Invoice", {"supplier": doc.supplier, "docstatus": 1},
					["name", "grand_total"], order_by="creation desc"
				)
				pe_draft = frappe.db.get_all(
					"Payment Entry Reference",
					filters={"reference_doctype": "Purchase Invoice"},
					fields=["reference_name", "total_amount", "outstanding_amount", "allocated_amount"],
					order_by="creation desc",
					limit=3,
				)
				pi_cur = frappe.db.get_value("Purchase Invoice", {"bill_no": ["like", f"%{doc.folio}%"]} or {"remark": ["like", f"%{doc.uuid[:8]}%"]}, ["name", "grand_total", "outstanding_amount"], as_dict=True)
			except Exception:
				pi_cur = None
			raise AssertionError(
				f"confirm_and_register falló para {fname}: {exc}\n"
				f"PI reciente: {diag_pi}\nPI por folio/uuid: {pi_cur}\nreferencias PE: {pe_draft}"
			) from exc
		print(f"  REGISTRADO: PR={res['purchase_receipt']} PI={res['purchase_invoice']} PE={res['payment_entry']}")

		# ---- validations ----
		pi = frappe.get_doc("Purchase Invoice", res["purchase_invoice"])
		if abs(pi.grand_total - doc.total) >= 0.02:
			lines = "\n".join(
				f"{i.item_code} qty={i.qty} rate={i.rate} amt={i.amount} disc={i.discount_amount or 0}"
				for i in pi.items
			)
			taxes = [(t.account_head, t.tax_amount) for t in pi.taxes]
			raise AssertionError(
				f"PI grand_total {pi.grand_total} != CFDI total {doc.total}\n{lines}\ntaxes={taxes}"
			)
		# GL balanced
		gle = frappe.db.get_all(
			"GL Entry",
			filters={"voucher_type": "Purchase Invoice", "voucher_no": pi.name},
			fields=["sum(debit) as d", "sum(credit) as c"],
		)
		gl = gle[0] if gle else {}
		if abs(float(gl.get("d") or 0) - float(gl.get("c") or 0)) >= 0.02:
			gl_rows = frappe.db.get_all(
				"GL Entry",
				filters={"voucher_type": "Purchase Invoice", "voucher_no": pi.name},
				fields=["account", "debit", "credit"],
			)
			raise AssertionError(
				f"GL desbalanceado para {pi.name}: d={gl.get('d')} c={gl.get('c')}\n{gl_rows}"
			)
		if res["purchase_receipt"]:
			pr = frappe.get_doc("Purchase Receipt", res["purchase_receipt"])
			assert pr.docstatus == 1, "PR no submitida"
			sle = frappe.db.get_all("Stock Ledger Entry", filters={"voucher_no": pr.name}, fields=["sum(actual_qty) as q"])
			print(f"  stock ledger qty: {sle[0]['q'] if sle else 0}")
		pe = frappe.get_doc("Payment Entry", res["payment_entry"])
		assert pe.docstatus == 1 and abs(pe.paid_amount - doc.total) < 0.02, "PE inconsistente"
		frappe.db.commit()  # keep the generated docs inspectable if a later check fails
		print(f"  ✓ PI {pi.name} total {pi.grand_total} == CFDI {doc.total}; GL balanced; PE posted")

	print("\n" + "=" * 70)
	print("E2E COMPLETO ✓  (documentos de prueba listos para inspección)")
	print("=" * 70)