<div align="center">

# erpnext_cfdi_mx

**Importa CFDI recibidos (comprobantes fiscales mexicanos) a ERPNext como compras semi-automáticas.**

[![Frappe](https://img.shields.io/badge/Frappe-v15-2496ED?logo=frappe)](https://frappeframework.com)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/amadogr50/erpnext_cfdi_mx/pulls)

*Sube un XML, parsea los conceptos, mapea contra tu catálogo y genera la recepción de mercancía, la factura de compra y el pago — al centavo.*

</div>

---

## ¿Qué hace?

Un módulo para [Frappe/ERPNext](https://erpnext.com) que convierte un **CFDI 4.0 de compras** en los documentos contables de ERPNext con un clic:

```
XML del proveedor (Costco, La Alpina, etc.)
        │
        ▼
┌───────────────────┐
│  1. Analizar CFDI │  parsea conceptos, timbre (UUID), impuestos
└─────────┬─────────┘
          ▼
┌───────────────────┐
│  2. Mapear líneas │  cada concepto contra un Item canónico o una cuenta de gasto
└─────────┬─────────┘      (reglas automáticas por proveedor + descripción)
          ▼
┌───────────────────┐
│  3. Crear compra  │  Purchase Receipt (stock) + Purchase Invoice (costo/IVA) + Payment Entry (pago)
└───────────────────┘
```

Sin copiar datos a mano, sin reconciliar totales: la factura cierra **exacta al centavo** contra el XML.

## Características

- **Parser CFDI 4.0 puro** (sin dependencias SAT): conceptos, traslados/retenciones por línea, timbre fiscal (UUID = llave anti-duplicado), descuentos, moneda extranjera.
- **Capa de mapeo inteligente** con reglas por `(RFC del proveedor, descripción normalizada)`:
  - ligar a un **Item canónico** existente,
  - **crear un Item** nuevo con su UOM y factor de conversión,
  - o registrarlo como **gasto no inventariable** (cuenta de gasto).
- **Factor de conversión nativo de ERPNext**: la línea de compra conserva la unidad del emisor (`1 Paquete × $90.52`) y ERPNext deriva el stock (`100 Unidades`) — sin redondeos fantasma. El rate nunca se diluye.
- **Totales exactos**: simula el redondeo de `rate` a 2 decimales del servidor y absorbe los residuos (línea de ajuste o redistribución), validando que `grand_total == total del XML`.
- **Impuestos mexicanos reales por línea**: IVA trasladado acreditable, ISR retenido (10%) e IVA retenido (2/3) con sus cuentas.
- **Anti-duplicado por UUID** normalizado (los emisores varían mayúsculas/minúsculas).
- **UI en el Desk (español)**: flujo Analizar → Resolver mapeos → Verificar y crear compra, con botones de acceso directo a los documentos generados.
- **E2E autocontenido** con fixtures reales de proveedores mexicanos (valida GL balanceado, stock y PE).

## Instalación

Dentro del bench de tu sitio:

```bash
cd $PATH_TO_YOUR_BENCH
cp -r erpnext_cfdi_mx apps/
pip install -e apps/erpnext_cfdi_mx        # registra el import path
bench --site tu-sitio.com install-app erpnext_cfdi_mx
bench --site tu-sitio.com migrate
bench build --app erpnext_cfdi_mx
```

Reinicia los procesos del sitio (`restart`) para que los workers tomen el nuevo `sys.path`.

## Uso

1. Crea un registro **CFDI Recibido** y adjunta el XML.
2. **Analizar CFDI** → verás las líneas parseadas con el estado de mapeo.
3. **Resolver mapeos** → liga cada concepto a un Item o cuenta de gasto (la regla se guarda sola para futuros CFDI del mismo proveedor).
4. **Verificar y crear compra** → genera `Purchase Receipt` + `Purchase Invoice` + `Payment Entry` (PUE). El botón solo aparece cuando todas las líneas están resueltas y los datos son válidos.

> El CFDI queda bloqueado como **Registrado**; cada UUID solo puede importarse una vez.

## Cómo funciona por dentro

| Módulo | Responsabilidad |
|---|---|
| `parser.py` | CFDI 4.0 → estructura de conceptos + impuestos (namespace-agnostic) |
| `normalizer.py` | Canoniza descripciones y detecta empaques ("C/100", "3/980 ML") |
| `mapper.py` | Reglas `(RFC, descripción)` → Item / gasto / factor de conversión |
| `import_service.py` | Pipeline analyze → preview → confirm, construcción de PR/PI/PE al centavo |
| `tax_setup.py` | Auto-configura el esquema fiscal MX (IVA acreditable, retenciones, plantillas) |
| `api.py` | Endpoints whitelisted usados por el Desk |

Lección de diseño clave del mapeo: **`ClaveProdServ` no es una llave estable** — el mismo producto cambia de clave entre proveedores (y dentro de un mismo archivo puede repetirse para productos distintos). El matching se apoya en `RFC + descripción normalizada`, con `ClaveProdServ` solo como referencia diagnóstica.

## Testing

```bash
# dentro del contenedor/bench con fixtures en /tmp/cfdi-fixtures/
bench --site cfdi-test.local execute erpnext_cfdi_mx.tests.e2e.run
```

El E2E crea una empresa mexicana mínima, importa los XML reales, mapea todo, registra las compras y verifica que cada factura cierre exacta, con GL balanceado, stock ledger correcto y pago aplicado.

## Requisitos

- Frappe/ERPNext **v15**
- Python **3.10+**
- Plan de cuentas mexicano (la app auto-crea las plantillas MX de compra/item)

## Licencia

MIT — haz lo que quieras, con gusto.

---

*Desarrollado originalmente para las compras reales de [Black Brûlée](https://blackbrulee.com) — postres de autor y mixología en Guadalajara, México. 🖤*