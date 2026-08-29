### Erpnext CFDI MX

Import CFDI (Mexican e-invoices) as semi-automatic purchases.

Importa CFDI recibidos (compras) a ERPNext: parsea el XML, mapea cada
concepto contra tu catálogo (con una capa de canonización proveedor +
descripción), y en un clic genera la recepción de mercancía, la factura
de compra y el pago.

### Installation

```bash
cd $PATH_TO_YOUR_BENCH
# sync the app sources into apps/erpnext_cfdi_mx, then:
bench --site erp.example.com install-app erpnext_cfdi_mx
bench --site erp.example.com migrate
bench build --app erpnext_cfdi_mx
```

### License

MIT
