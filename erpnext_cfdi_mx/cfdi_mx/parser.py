"""CFDI 4.0 XML parser (pure Python, no frappe dependency).

Parses a CFDI 4.0 (Invoiced/Ingreso) XML into a plain dict and runs
structural + arithmetic validation that the rest of the app can act on.

The intent is for this module to be kept *free of frappe imports* so it can
be unit-tested on a Mac without a bench, and reused anywhere else later.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List

CFDI_NS = "http://www.sat.gob.mx/cfd/4"
CFDI31_NS = "http://www.sat.gob.mx/cfd/3"
TFD_NS = "http://www.sat.gob.mx/TimbreFiscalDigital"

# SAT tax codes
TAX_NAMES = {"001": "ISR", "002": "IVA", "003": "IEPS", "016": "IVA"}

SUPPORTED_VERSIONS = {"4.0"}
SUPPORTED_TYPES = {"I"}  # v1 only imports Ingreso (purchases)


def _local(tag: str) -> str:
	"""Return the local part of an XML tag, ignoring the namespace."""
	if "}" in tag:
		return tag.split("}", 1)[1]
	return tag


def _dec(value: Any) -> Decimal:
	"""Best-effort Decimal conversion; returns Decimal('0') on garbage."""
	try:
		return Decimal(str(value or "0"))
	except (InvalidOperation, ValueError):
		return Decimal("0")


def _num(value: Any) -> float:
	"""Decimal -> float helper for transport-safe dicts."""
	return float(_dec(value))


class CFDIValidationError(ValueError):
	"""Raised when the XML is not a CFDI we can process."""


class CFDIParseResult:
	"""Small result holder so callers can inspect errors/warnings clearly."""

	def __init__(self, data: Dict[str, Any], errors: List[str], warnings: List[str]):
		self.data = data
		self.errors = errors
		self.warnings = warnings

	@property
	def ok(self) -> bool:
		return not self.errors


def parse_cfdi(xml_bytes: bytes) -> CFDIParseResult:
	"""Parse CFDI 4.0 bytes into a structured dict with validation results.

	Raises CFDIValidationError for XML that is not valid/parseable XML at all.
	All *semantic* findings go to CFDIParseResult.errors/warnings.
	"""
	errors: List[str] = []
	warnings: List[str] = []

	try:
		root = ET.fromstring(xml_bytes)
	except ET.ParseError as e:
		raise CFDIValidationError(f"El archivo no es XML válido: {e}") from e

	if _local(root.tag) != "Comprobante":
		raise CFDIValidationError("No se encontró el elemento cfdi:Comprobante en el XML.")

	attrs = root.attrib
	version = attrs.get("Version", "")
	if version not in SUPPORTED_VERSIONS:
		errors.append(
			f"Versión de CFDI no soportada: {version} (solo se acepta 4.0). "
			"CFDI de Egreso/Traslado y versiones 3.x se manejan en una fase posterior."
		)

	parsed: Dict[str, Any] = {
		"version": version,
		"serie": attrs.get("Serie", ""),
		"folio": attrs.get("Folio", ""),
		"fecha_emision": attrs.get("Fecha", ""),
		"tipo_comprobante": attrs.get("TipoDeComprobante", ""),
		"forma_pago": attrs.get("FormaPago", ""),
		"metodo_pago": attrs.get("MetodoPago", ""),
		"moneda": attrs.get("Moneda", ""),
		"tipo_cambio": _num(attrs.get("TipoCambio", "1")),
		"subtotal": _num(attrs.get("SubTotal", "0")),
		"descuento": _num(attrs.get("Descuento", "0")),
		"total": _num(attrs.get("Total", "0")),
		"lugar_expedicion": attrs.get("LugarExpedicion", ""),
		"emisor": {},
		"receptor": {},
		"conceptos": [],
		"impuestos": {"trasladados": [], "retenciones": []},
		"timbre": {},
	}

	if parsed["tipo_comprobante"] not in SUPPORTED_TYPES:
		errors.append(
			f"Tipo de comprobante '{parsed['tipo_comprobante']}' no soportado. "
			"En v1 solo se importan CFDI de Ingreso (I)."
		)

	if parsed["moneda"] != "MXN":
		warnings.append(
			f"CFDI en moneda {parsed['moneda']} (TC={parsed['tipo_cambio']}). "
			"Se importa con la tasa del XML; revisa el tipo de cambio."
		)

	# --- Parties & concepts (iterate direct children, namespace tolerant) ---
	comprobante_impuestos = {"trasladados": [], "retenciones": []}

	for child in root:
		tag = _local(child.tag)
		if tag == "Emisor":
			parsed["emisor"] = {
				"rfc": child.attrib.get("Rfc", ""),
				"nombre": child.attrib.get("Nombre", ""),
				"regimen": child.attrib.get("RegimenFiscal", ""),
			}
		elif tag == "Receptor":
			parsed["receptor"] = {
				"rfc": child.attrib.get("Rfc", ""),
				"nombre": child.attrib.get("Nombre", ""),
				"regimen": child.attrib.get("RegimenFiscalReceptor", ""),
				"uso_cfdi": child.attrib.get("UsoCFDI", ""),
			}
		elif tag == "Conceptos":
			for concepto in child:
				if _local(concepto.tag) != "Concepto":
					continue
				parsed["conceptos"].append(_parse_concepto(concepto, errors))
		elif tag == "Impuestos":
			comprobante_impuestos = _parse_impuestos(child)
		elif tag == "Complemento":
			for comp in child:
				cns = re.split(r"[{}]", comp.tag)
				ns = cns[1] if len(cns) > 1 else ""
				if ns == TFD_NS and _local(comp.tag) == "TimbreFiscalDigital":
					parsed["timbre"] = {
						"uuid": comp.attrib.get("UUID", ""),
						"fecha_timbrado": comp.attrib.get("FechaTimbrado", ""),
						"rfc_prov_certif": comp.attrib.get("RfcProvCertif", ""),
					}
					break

	parsed["impuestos"] = comprobante_impuestos

	# --- Structural/arithmetic validation ---
	if not parsed["emisor"].get("rfc"):
		errors.append("Falta el RFC del emisor.")
	if not parsed["receptor"].get("rfc"):
		errors.append("Falta el RFC del receptor.")
	if not parsed["conceptos"]:
		errors.append("El CFDI no contiene conceptos.")

	if not parsed["timbre"].get("uuid"):
		warnings.append(
			"No se encontró el UUID (TimbreFiscalDigital). No se podrá detectar duplicados."
		)

	_validate_amounts(parsed, errors, warnings)

	return CFDIParseResult(parsed, errors, warnings)


def _parse_concepto(el: ET.Element, errors: List[str]) -> Dict[str, Any]:
	concepto = {
		"clave_prod_serv": el.attrib.get("ClaveProdServ", ""),
		"no_identificacion": el.attrib.get("NoIdentificacion", ""),
		"cantidad": float(_dec(el.attrib.get("Cantidad", "0"))),
		"clave_unidad": el.attrib.get("ClaveUnidad", ""),
		"unidad": el.attrib.get("Unidad", ""),
		"descripcion": (el.attrib.get("Descripcion", "") or "").strip(),
		"valor_unitario": float(_dec(el.attrib.get("ValorUnitario", "0"))),
		"importe": float(_dec(el.attrib.get("Importe", "0"))),
		"descuento": float(_dec(el.attrib.get("Descuento", "0"))),
		"objeto_imp": el.attrib.get("ObjetoImp", ""),
		"traslados": [],
		"retenciones": [],
	}

	if not concepto["descripcion"]:
		errors.append("Existe un concepto sin descripción.")

	for sub in el:
		if _local(sub.tag) == "Impuestos":
			imp = _parse_impuestos(sub)
			concepto["traslados"] = imp["trasladados"]
			concepto["retenciones"] = imp["retenciones"]

	return concepto


def _parse_impuestos(el: ET.Element) -> Dict[str, Any]:
	impuestos = {"trasladados": [], "retenciones": []}
	for group in el:
		group_tag = _local(group.tag)
		out = []
		for tax in group:
			out.append(
				{
					"base": float(_dec(tax.attrib.get("Base", "0"))),
					"impuesto": tax.attrib.get("Impuesto", ""),
					"tipo_factor": tax.attrib.get("TipoFactor", ""),
					"tasa": float(_dec(tax.attrib.get("TasaOCuota", "0"))),
					"importe": float(_dec(tax.attrib.get("Importe", "0"))),
				}
			)
		if group_tag == "Traslados":
			impuestos["trasladados"] = out
		elif group_tag == "Retenciones":
			impuestos["retenciones"] = out
	return impuestos


def _validate_amounts(parsed: Dict[str, Any], errors: List[str], warnings: List[str]) -> None:
	def near(a, b, tol=0.02):
		return abs(Decimal(str(a)) - Decimal(str(b))) <= Decimal(str(tol))

	subtotal = _dec(parsed["subtotal"])
	descuento = _dec(parsed["descuento"])
	total = _dec(parsed["total"])

	# Per-line: importe == cantidad * valor unitario (before discount)
	for i, c in enumerate(parsed.get("conceptos", []), start=1):
		if c["cantidad"] and c["valor_unitario"] and not near(
			c["importe"], c["cantidad"] * c["valor_unitario"]
		):
			warnings.append(
				f"Concepto {i} ({c['descripcion'][:40]}...): "
				f"importe {c['importe']} != cantidad × valor unitario "
				f"({c['cantidad']} × {c['valor_unitario']}). Revisar descuentos/redondeo."
			)

	# Sum per-line taxes
	line_trasladados = sum(_dec(t["importe"]) for c in parsed.get("conceptos", []) for t in c.get("traslados", []))
	line_retenciones = sum(_dec(t["importe"]) for c in parsed.get("conceptos", []) for t in c.get("retenciones", []))

	global_tras = sum(_dec(t["importe"]) for t in parsed["impuestos"]["trasladados"])
	global_ret = sum(_dec(t["importe"]) for t in parsed["impuestos"]["retenciones"])

	if not near(line_trasladados, global_tras):
		warnings.append(
			f"Suma de IVA por línea ({float(line_trasladados):.2f}) no coincide "
			f"con el total global trasladado ({float(global_tras):.2f})."
		)
	if not near(line_retenciones, global_ret):
		warnings.append(
			f"Suma de retenciones por línea ({float(line_retenciones):.2f}) no coincide "
			f"con el global retenido ({float(global_ret):.2f})."
		)

	# Classic identity: SubTotal - Descuento + Trasladados - Retenciones == Total
	computed_total = subtotal - descuento + line_trasladados - line_retenciones
	if not near(computed_total, total):
		errors.append(
			f"Inconsistencia aritmética: SubTotal({float(subtotal):.2f}) - Descuento({float(descuento):.2f}) "
			f"+ IVA({float(line_trasladados):.2f}) - Retenciones({float(line_retenciones):.2f}) "
			f"= {float(computed_total):.2f}, pero el Total del XML es {float(total):.2f}."
		)

	return CFDIParseResult(parsed, errors, warnings)
