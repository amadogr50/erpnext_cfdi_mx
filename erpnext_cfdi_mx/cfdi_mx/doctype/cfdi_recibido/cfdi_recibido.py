from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class CFDIRecibido(Document):
	def validate(self):
		if self.uuid:
			dup = frappe.db.get_value(
				"CFDI Recibido",
				{"uuid": self.uuid, "name": ("!=", self.name), "is_duplicate": 0},
				"name",
			)
			if dup and self.is_duplicate != 1:
				self.is_duplicate = 1
				self.validation_message = (
					f"UUID ya registrado en {dup}. Confirma si es un envío repetido."
				)
			elif not dup:
				self.is_duplicate = 0

		if self.status == "Registrado":
			for r in self.items:
				if r.mapping_status in ("Sin Regla",):
					frappe.throw(_("CFDI registrado no puede tener líneas sin mapeo."))

	def before_update_after_submit(self):
		pass

	def on_trash(self):
		if self.purchase_invoice or self.purchase_receipt or self.payment_entry:
			frappe.throw(
				_(
					"No puedes borrar un CFDI que ya generó documentos. Cancela/elimina "
					"PR/PI/PE primero."
				)
			)
