from __future__ import annotations

import frappe
from frappe.model.document import Document


class CFDIMapperSettings(Document):
	def validate(self):
		if not self.company:
			frappe.throw("Selecciona la Compañía antes de guardar la configuración.")
