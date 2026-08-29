// CFDI Mapper Settings — one-click Mexico tax setup.
frappe.ui.form.on("CFDI Mapper Settings", {
	refresh: function (frm) {
		frm.add_custom_button(__("Ejecutar setup fiscal MX"), () => {
			if (!frm.doc.company) {
				frappe.msgprint(__("Selecciona la Compañía primero."));
				return;
			}
			frappe.call({
				method: "erpnext_cfdi_mx.cfdi_mx.api.setup_tax",
				args: { company: frm.doc.company },
				freeze: true,
				freeze_message: __("Configurando impuestos México…"),
				callback(r) {
					const m = r.message || {};
					frappe.msgprint({
						title: __("Setup fiscal completado"),
						indicator: "green",
						message: `<pre>${(m.summary || "").replace(/\n/g, "<br>")}</pre>`,
					});
					frm.reload_doc();
				},
				error(r) {
					frappe.msgprint(r && r.message ? r.message : __("El setup fiscal falló."));
				},
			});
		});
	},
});
