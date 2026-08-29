from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class CFDIItemRule(Document):
	def validate(self):
		if not self.normalized_key:
			self.normalized_key = self._normalize(self.description)

		filters = {
			"supplier_tax_id": self.supplier_tax_id,
			"normalized_key": self.normalized_key,
		}
		if duplicate := frappe.db.get_value(
			"CFDI Item Rule", {**filters, "name": ("!=", self.name)}, "name"
		):
			frappe.throw(
				_("Ya existe la regla {0} para el mismo proveedor y descripción normalizada.").format(
					duplicate
				)
			)

		if self.is_inventory and self.item:
			if not self.stock_uom:
				self.stock_uom = frappe.db.get_value("Item", self.item, "stock_uom")
			if self.conversion_factor in (None, 0):
				self.conversion_factor = 1.0

		if not self.is_inventory and not self.expense_account:
			settings = frappe.get_single("CFDI Mapper Settings")
			if settings.default_expense_account:
				self.expense_account = settings.default_expense_account

	@staticmethod
	def _normalize(text: str) -> str:
		from ..normalizer import normalize_description

		return normalize_description(text)
