// Client script for CFDI Recibido — drives the semi-automatic import flow.
frappe.ui.form.on("CFDI Recibido", {
	refresh: function (frm) {
		const status = frm.doc.status || "Borrador";

		frm.clear_custom_buttons();

		// ANALIZAR / REPROCESAR
		if (frm.doc.name && frm.doc.xml_file && status !== "Registrado") {
			frm.add_custom_button(
				__(status === "Error" || status === "Borrador" ? "Analizar CFDI" : "Reprocesar CFDI"),
				() => cfdi.call_action(frm, "analyze"),
				__(status === "Error" ? "Reparar" : "Acciones")
			);
		}

		// resolver mapeos pendientes
		const pending = (frm.doc.items || []).filter((r) => r.mapping_status === "Sin Regla");
		if (status !== "Registrado" && status !== "Error" && pending.length) {
			frm.add_custom_button(
				__("Resolver {0} mapeos", [pending.length]),
				() => cfdi.open_mapping_dialog(frm, pending),
				__("Acciones")
			);
		}

		// verificar y crear la compra (all lines resolved and data valid)
		if (status === "En Revisión" && frm.doc.data_ok && !pending.length) {
			frm.add_custom_button(__("Verificar y crear compra"), () => cfdi.confirm_register(frm), __("Acciones"));
		}

		// quick links when registered
		if (status === "Registrado") {
			["purchase_receipt", "purchase_invoice", "payment_entry"].forEach((field) => {
				if (frm.doc[field]) {
					frm.add_custom_button(
						__(`Abrir ${frappe.meta.get_docfield("CFDI Recibido", field).label}`),
						() => frappe.set_route("Form", field === "purchase_receipt" ? "Purchase Receipt" : field === "purchase_invoice" ? "Purchase Invoice" : "Payment Entry", frm.doc[field]),
						__("Documentos generados")
					);
				}
			});
		}

		if (status === "Listo para Verificar") {
			frm.add_custom_button(__("Verificar y crear compra"), () => cfdi.confirm_register(frm), __("Acciones"));
		}
	},
});

window.cfdi = {
	call_action(frm, action) {
		frappe.call({
			method: "erpnext_cfdi_mx.cfdi_mx.api." + action,
			args: { cfdi_name: frm.doc.name },
			btn: $(frm.page.wrapper).find(".btn-primary")[0],
			freeze: true,
			freeze_message: __(action === "analyze" ? "Analizando CFDI…" : "Procesando…"),
			callback(r) {
				frappe.show_alert({ message: __("Listo"), indicator: "green" });
				frm.reload_doc();
			},
			error(r) {
				frappe.msgprint(r && r.message ? r.message : __("Ocurrió un error."));
			},
		});
	},

	open_mapping_dialog(frm, pending) {
		const settings = {};

		const dialog = new frappe.ui.Dialog({
			title: __("Resolver mapeo de concepto"),
			fields: [
				{
					fieldname: "line",
					fieldtype: "Select",
					label: __("Concepto del CFDI"),
					options: pending.map((r) => `${r.idx}: ${r.description} (${r.amount})`).join("\n"),
					reqd: 1,
				},
				{ fieldtype: "Section Break" },
				{
					fieldname: "decision",
					fieldtype: "Select",
					label: __("Decisión"),
					options: "link\ncreate\nnon_inventory",
					default: "link",
					reqd: 1,
				},
				// link
				{
					fieldname: "item",
					fieldtype: "Link",
					label: __("Item canónico existente"),
					options: "Item",
					depends_on: "eval:doc.decision==='link'",
				},
				// create
				{
					fieldname: "create_item_name",
					fieldtype: "Data",
					label: __("Nombre del Item nuevo"),
					depends_on: "eval:doc.decision==='create'",
				},
				{
					fieldname: "create_item_stock_uom",
					fieldtype: "Link",
					label: __("UOM de stock del Item"),
					options: "UOM",
					depends_on: "eval:doc.decision==='create'",
				},
				{
					fieldname: "create_item_tax_rate",
					fieldtype: "Select",
					label: __("Método de IVA del Item"),
					options: "0%\n16%",
					default: "0%",
					depends_on: "eval:doc.decision==='create'",
				},
				// non-inventory
				{
					fieldname: "expense_account",
					fieldtype: "Link",
					label: __("Cuenta de gasto"),
					options: "Account",
					get_query: () => ({ filters: { is_group: 0, account_type: ["in", ["Expense Account", ""]] } }),
					depends_on: "eval:doc.decision==='non_inventory'",
				},
				{ fieldtype: "Section Break" },
				{
					fieldname: "stock_uom",
					fieldtype: "Link",
					label: __("UOM de stock (conversión)"),
					options: "UOM",
					depends_on: "eval:doc.decision!=='non_inventory'",
				},
				{
					fieldname: "conversion_factor",
					fieldtype: "Float",
					label: __("Factor de conversión"),
					default: 1,
					depends_on: "eval:doc.decision!=='non_inventory'",
				},
				{
					fieldname: "suggest_btn",
					fieldtype: "Button",
					label: __("Sugerir factor desde el empaque"),
					depends_on: "eval:doc.decision!=='non_inventory'",
				},
			],
			primary_action_label: __("Guardar regla"),
			primary_action(values) {
				cfdi.save_rule(frm, values, pending, dialog);
			},
		});

		dialog.fields_dict.suggest_btn.$input.on("click", function () {
			const v = dialog.get_values();
			if (!v || !v.line) return;
			const desc = cfdi.line_by_key(frm, v.line).description;
			const uom = v.stock_uom || v.create_item_stock_uom || "No(s)";
			frappe.call({
				method: "erpnext_cfdi_mx.cfdi_mx.api.suggest_factor",
				args: { description: desc, stock_uom: uom },
				callback(r) {
					dialog.set_value("conversion_factor", r.message);
					frappe.show_alert({ message: __("Factor sugerido: {0}", [r.message]), indicator: "blue" });
				},
			});
		});

		dialog.show();
	},

	line_by_key(frm, key) {
		const idx = parseInt(key.split(":")[0], 10);
		return (frm.doc.items || []).find((r) => r.idx === idx);
	},

	save_rule(frm, values, pending, dialog) {
		const line = cfdi.line_by_key(frm, values.line);
		if (!line) {
			frappe.msgprint(__("No se encontró la línea seleccionada."));
			return;
		}
		frappe.call({
			method: "erpnext_cfdi_mx.cfdi_mx.api.create_rule_from_line",
			args: {
				cfdi_name: frm.doc.name,
				line_name: line.name,
				decision: values.decision,
				item: values.item || "",
				create_item_name: values.create_item_name || "",
				create_item_stock_uom: values.create_item_stock_uom || "",
				create_item_tax_rate: values.create_item_tax_rate || "0%",
				expense_account: values.expense_account || "",
				conversion_factor: values.conversion_factor || 1,
				stock_uom: values.stock_uom || "",
			},
			freeze: true,
			freeze_message: __("Guardando regla…"),
			callback(r) {
				dialog.hide();
				frappe.show_alert({ message: __("Regla guardada."), indicator: "green" });
				frm.reload_doc();
			},
			error(r) {
				frappe.msgprint(r && r.message ? r.message : __("No se pudo guardar la regla."));
			},
		});
	},

	confirm_register(frm) {
		const d = new frappe.ui.Dialog({
			title: __("Confirmar compra"),
			fields: [
				{
					fieldname: "receive_stock",
					fieldtype: "Check",
					label: __("Recibir mercancía (crear Purchase Receipt)"),
					default: 1,
				},
				{ fieldtype: "HTML", fieldname: "summary" },
			],
			primary_action_label: __("Crear compra"),
			primary_action(values) {
				frappe.call({
					method: "erpnext_cfdi_mx.cfdi_mx.api.confirm",
					args: { cfdi_name: frm.doc.name, receive_stock: values.receive_stock ? "1" : "0" },
					freeze: true,
					freeze_message: __("Creando compra (PR/PI/PE)…"),
					callback(r) {
						d.hide();
						const msg = r.message || {};
						frappe.show_alert(
							{
								message: __("Compra registrada: PR {0} · PI {1} · PE {2}", [msg.purchase_receipt || "—", msg.purchase_invoice, msg.payment_entry]),
								indicator: "green",
								seconds: 12,
							},
							frm.reload_doc()
						);
					},
					error(r) {
						frappe.msgprint(r && r.message ? r.message : __("No se pudo crear la compra."));
					},
				});
			},
		});
		d.fields_dict.summary.$wrapper.html(
			`<p style="margin-top:-6px">Se generará la recepción de mercancía (si aplica), la factura de compra y el pago, contra las reglas ya guardadas.</p>`
		);
		d.show();
	},
};
