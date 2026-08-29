"""Description normalization + pack-size heuristics (pure Python).

The mapping rule key is computed with normalize_description so that two
descriptions that differ only in case/spacing/punctuation/accents (as they
do across suppliers) collapse to the same key.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional, Tuple

# Token-level noise we strip before comparing.
_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)
_MULTI_SPACE_RE = re.compile(r"\s+")
# "8/360 G", "10/1 KG", "3/980 ML", "12/160 HOJAS"
_PACK_RE = re.compile(r"(?P<count>\d+)\s*/\s*(?P<qty>\d+(?:[.,]\d+)?)\s+(?P<unit>[A-Za-z]{1,4})\b", re.IGNORECASE)
# "90 PZAS", "60 PZAS"
_PIECES_RE = re.compile(r"(?P<count>\d+)\s+PZAS?\b", re.IGNORECASE)

# Maps a SAT/description unit token to a canonical volumetric/weight unit to
# allow pack suggestions like "3 x 980 ML -> 2.94 L".
_MASS_VOL_MULT = {
	"G": 1.0, "GR": 1.0, "KG": 1000.0, "KGS": 1000.0, "KGM": 1000.0,
	"ML": 1.0, "L": 1000.0, "LT": 1000.0, "LTR": 1000.0,
}
_QTYALLY = {"PZA", "PIEZA", "PCS", "PZAS", "PIEZAS", "UNIDAD", "UND"}


def normalize_description(text: str) -> str:
	"""Stable canonical key for a CFDI concept description.

	- NFKC normalizes unicode; accents/marks are stripped
	- everything uppercase, only letters/digits/spaces kept
	- multiple internal spaces collapsed; edges trimmed
	"""
	if not text:
		return ""
	nfkd = unicodedata.normalize("NFKD", text)
	no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
	# split bound digit-letter so "5kg" and "5 KG" collapse to the same key
	no_accents = re.sub(r"(?<=[0-9])(?=[A-Za-z])", " ", no_accents)
	no_accents = re.sub(r"(?<=[A-Za-z])(?=[0-9])", " ", no_accents)
	no_punct = _PUNCT_RE.sub(" ", no_accents)
	upper = no_punct.upper()
	return _MULTI_SPACE_RE.sub(" ", upper).strip()


def pack_info(description: str) -> Optional[dict]:
	"""Return info about a pack embedded in a description, if any.

	Examples:
		"LECHE EVAPORADA 8/360 G CARNATION"  -> {"count": 8, "per": 360.0, "unit": "G"}
		"CREMA PARA BATIR 3/980 ML LYNCOTT"  -> {"count": 3, "per": 980.0, "unit": "ML"}
		"AZUCAR MORENA 10/1 KG ZULKA"        -> {"count": 10, "per": 1.0, "unit": "KG"}
		"HUEVO GALLINA LIBRE 90 PZAS"        -> {"count": 90, "per": None, "unit": "PZAS"}
	"""
	desc = (description or "").upper()
	m = _PACK_RE.search(desc)
	if m:
		return {
			"count": int(m.group("count")),
			"per": float(m.group("qty").replace(",", ".")),
			"unit": m.group("unit").upper(),
		}
	m = _PIECES_RE.search(desc)
	if m:
		return {"count": int(m.group("count")), "per": None, "unit": "PZAS"}
	return None


def suggest_factor(description: str, target_uom_label: Optional[str] = None) -> float:
	"""Suggest a conversion factor (CFDI quantity -> stock units).

	Rules of thumb used to pre-fill the mapping UI:
	  - "N x QTY UNIT" + target is a mass/volume unit in the same class
	    -> total quantity in the target unit, e.g. 3/980 ML -> 2.94 (if L)
	  - "N x QTY UNIT" + target is a piece-like unit -> N
	  - "N PZAS" -> N
	  - otherwise -> 1
	"""
	pk = pack_info(description)
	if not pk:
		return 1.0

	if pk["per"] is None:  # N PZAS
		return float(pk["count"])

	target = (target_uom_label or "").upper()
	if target in _QTYALLY:
		return float(pk["count"])

	# try to convert the pack per-unit quantity into the target unit's scale
	base_uom = pk["unit"]
	if base_uom in _MASS_VOL_MULT:
		target_scale = _scale_for_target(target)
		if target_scale is not None:
			total_in_base = pk["count"] * pk["per"] * _MASS_VOL_MULT[base_uom]
			return _round_ratio(total_in_base, target_scale)

	return float(pk["count"])  # default: one stock unit per CFDI line


def _scale_for_target(target: str) -> Optional[float]:
	if target in ("KG", "KGS", "KGM", "KILO", "KILOGRAMO"):
		return 1000.0
	if target in ("G", "GR", "GRAMO"):
		return 1.0
	if target in ("L", "LT", "LTR", "LITRO"):
		return 1000.0
	if target in ("ML", "MILILITRO"):
		return 1.0
	return None


def _round_ratio(total_in_base: float, target_scale: float) -> float:
	ratio = total_in_base / target_scale
	return round(ratio, 4)
