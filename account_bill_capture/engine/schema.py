# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""The answer the AI must give for an invoice, and its strict validation. Odoo independent.

The answer is DATA: it is parsed with ``json`` (never evaluated), every field is checked for type,
range and length, unknown keys are dropped and anything invalid is reported, not guessed.
"""
import json
import math
import re
from datetime import date

MAX_LINES = 100
MAX_TEXT = 300
LINE_ARTS = ("ware", "dienstleistung", "laufende_kosten")
CONFIDENCE_KEYS = ("supplier", "invoice_number", "invoice_date", "due_date", "currency", "lines",
                   "totals", "payment_reference")

# What the prompt tells the model (also the reference for tests and documentation).
BILL_SCHEMA = {
    "supplier": {"name": "string", "vat": "string|null (UID / VAT number)", "iban": "string|null",
                 "address": "string|null (street, ZIP, city, country on one line)"},
    "invoice_number": "string|null",
    "invoice_date": "YYYY-MM-DD|null",
    "due_date": "YYYY-MM-DD|null",
    "currency": "ISO 4217 code, e.g. EUR|null",
    "lines": [{
        "description": "string (the text of the position)",
        "item_number": "string|null (supplier's article number)",
        "quantity": "number",
        "unit": "string|null (piece, kg, h ...)",
        "unit_price_net": "number (net, before discount)",
        "discount_percent": "number 0-100|null",
        "tax_rate": "number, percent, e.g. 20|null",
        "art": "ware | dienstleistung | laufende_kosten",
    }],
    "total_net": "number|null",
    "total_tax": "number|null",
    "total_gross": "number|null",
    "payment_reference": "string|null",
    "confidence": {key: "number 0-1" for key in CONFIDENCE_KEYS},
}


class BillSchemaError(Exception):
    """The answer is not usable at all (no JSON object)."""


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_NUMBER = re.compile(r"^-?\d+(?:[.,]\d+)?$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def extract_json(text):
    """The JSON object inside the answer (a model may wrap it in a code fence). Never evaluates."""
    text = (text or "").strip()
    if not text:
        raise BillSchemaError("empty answer")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise BillSchemaError("no JSON object in the answer")

    def refuse(constant):
        raise ValueError(constant)

    try:
        data = json.loads(text[start:end + 1], parse_constant=refuse)
    except ValueError as exc:
        raise BillSchemaError("invalid JSON: %s" % str(exc)[:80])
    if not isinstance(data, dict):
        raise BillSchemaError("the answer is not a JSON object")
    return data


def _text(value, limit=MAX_TEXT):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("not a text")
    value = _CTRL.sub("", str(value)).strip()
    return value[:limit] or None


def _number(value, low=None, high=None):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("not a number")
    if isinstance(value, str):
        if not _NUMBER.match(value.strip()):
            raise ValueError("not a number")
        value = float(value.strip().replace(",", "."))
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("not a number")
    value = float(value)
    if (low is not None and value < low) or (high is not None and value > high):
        raise ValueError("out of range")
    return value


def _date(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _DATE.match(value.strip()):
        raise ValueError("not a date YYYY-MM-DD")
    return date.fromisoformat(value.strip()).isoformat()


def _currency(value):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or not _CURRENCY.match(value.strip().upper()):
        raise ValueError("not a currency code")
    return value.strip().upper()


def validate(text):
    """``(clean, problems)`` for the raw answer ``text``. ``clean`` has every key of the schema;
    invalid values are None (and named in ``problems``). Raises BillSchemaError when there is no object."""
    raw = extract_json(text)
    problems = []

    def field(label, func, value, *args):
        try:
            return func(value, *args)
        except (ValueError, TypeError):
            problems.append("%s: invalid value" % label)
            return None

    supplier_raw = raw.get("supplier")
    if supplier_raw is not None and not isinstance(supplier_raw, dict):
        problems.append("supplier: not an object")
        supplier_raw = {}
    supplier_raw = supplier_raw or {}
    clean = {
        "supplier": {
            "name": field("supplier.name", _text, supplier_raw.get("name"), 200),
            "vat": field("supplier.vat", _text, supplier_raw.get("vat"), 40),
            "iban": field("supplier.iban", _text, supplier_raw.get("iban"), 40),
            "address": field("supplier.address", _text, supplier_raw.get("address"), 300),
        },
        "invoice_number": field("invoice_number", _text, raw.get("invoice_number"), 100),
        "invoice_date": field("invoice_date", _date, raw.get("invoice_date")),
        "due_date": field("due_date", _date, raw.get("due_date")),
        "currency": field("currency", _currency, raw.get("currency")),
        "total_net": field("total_net", _number, raw.get("total_net")),
        "total_tax": field("total_tax", _number, raw.get("total_tax")),
        "total_gross": field("total_gross", _number, raw.get("total_gross")),
        "payment_reference": field("payment_reference", _text, raw.get("payment_reference"), 140),
        "lines": [],
        "confidence": {},
    }
    lines = raw.get("lines")
    if lines is None:
        lines = []
    if not isinstance(lines, list):
        problems.append("lines: not a list")
        lines = []
    if len(lines) > MAX_LINES:
        problems.append("lines: more than %d positions, the rest is ignored" % MAX_LINES)
        lines = lines[:MAX_LINES]
    for index, line in enumerate(lines, 1):
        label = "line %d" % index
        if not isinstance(line, dict):
            problems.append("%s: not an object" % label)
            continue
        description = field(label + ".description", _text, line.get("description"), 500)
        price = field(label + ".unit_price_net", _number, line.get("unit_price_net"))
        quantity = field(label + ".quantity", _number, line.get("quantity"))
        if not description or price is None:
            problems.append("%s: position without text or price is ignored" % label)
            continue
        if quantity is None:
            quantity = 1.0
            problems.append("%s: no quantity, 1 assumed" % label)
        art = line.get("art")
        art = art.strip().lower() if isinstance(art, str) else None
        clean["lines"].append({
            "description": description,
            "item_number": field(label + ".item_number", _text, line.get("item_number"), 80),
            "quantity": quantity,
            "unit": field(label + ".unit", _text, line.get("unit"), 30),
            "unit_price_net": price,
            "discount_percent": field(label + ".discount_percent", _number, line.get("discount_percent"), 0, 100) or 0.0,
            "tax_rate": field(label + ".tax_rate", _number, line.get("tax_rate"), 0, 100),
            "art": art if art in LINE_ARTS else None,
        })
    confidence = raw.get("confidence")
    if isinstance(confidence, dict):
        for key, value in list(confidence.items())[:30]:
            if key in CONFIDENCE_KEYS:
                try:
                    number = _number(value, 0, 1)
                except ValueError:
                    continue
                if number is not None:
                    clean["confidence"][key] = number
    if not clean["lines"] and clean["total_gross"] is None and clean["total_net"] is None:
        raise BillSchemaError("the answer contains neither positions nor totals")
    return clean, problems


def line_net(line):
    return line["quantity"] * line["unit_price_net"] * (1 - (line.get("discount_percent") or 0.0) / 100.0)


def check_totals(data, tolerance=0.02):
    """Plausibility of the sums: a list of ``(code, detail)``. Empty = the figures agree.

    Codes: ``no_totals`` (nothing to compare), ``net`` (sum of positions differs from the net total),
    ``gross`` (positions plus tax differ from the gross total), ``sum`` (net + tax differ from gross).
    """
    out = []
    lines = data["lines"]
    net_sum = round(sum(line_net(l) for l in lines), 2)
    gross_sum = round(sum(line_net(l) * (1 + (l["tax_rate"] or 0.0) / 100.0) for l in lines), 2)
    total_net, total_tax, total_gross = data["total_net"], data["total_tax"], data["total_gross"]
    if total_net is None and total_gross is None:
        return [("no_totals", "")]
    if total_net is not None and lines and abs(net_sum - total_net) > tolerance:
        out.append(("net", "%.2f / %.2f" % (net_sum, total_net)))
    if total_gross is not None and lines and all(l["tax_rate"] is not None for l in lines) \
            and abs(gross_sum - total_gross) > tolerance:
        out.append(("gross", "%.2f / %.2f" % (gross_sum, total_gross)))
    if None not in (total_net, total_tax, total_gross) and abs(total_net + total_tax - total_gross) > tolerance:
        out.append(("sum", "%.2f + %.2f / %.2f" % (total_net, total_tax, total_gross)))
    return out


def date_problems(data, today, max_age_days=1100, max_future_days=14):
    """Codes of implausible dates: ``invoice_date`` (far in the past or the future), ``due_date`` (before the invoice)."""
    out = []
    invoice = date.fromisoformat(data["invoice_date"]) if data["invoice_date"] else None
    if invoice and ((today - invoice).days > max_age_days or (invoice - today).days > max_future_days):
        out.append("invoice_date")
    if invoice and data["due_date"] and date.fromisoformat(data["due_date"]) < invoice:
        out.append("due_date")
    return out


STANDARD_RATES = (0.0, 5.0, 7.0, 9.0, 10.0, 12.0, 13.0, 19.0, 20.0, 21.0, 22.0, 25.0)


def _rate_from_totals(net, tax):
    """The standard tax rate that net and tax amount point to (within half a percentage point), else None."""
    if net is None or tax is None or net <= 0 or tax < 0:
        return None
    rate = tax / net * 100.0
    near = min(STANDARD_RATES, key=lambda r: abs(r - rate))
    return near if abs(near - rate) <= 0.5 else None


def repair(data):
    """Fill gaps that the totals of the document make certain. Returns the list of notes (what was done).

    * no tax rate on any position, but net and tax total give a standard rate: the rate is taken over;
    * no position at all, but a total: one position "total of the document" (so the bill is not empty).
    Nothing is invented: both rules need figures read from the document."""
    notes = []
    net, tax, gross = data["total_net"], data["total_tax"], data["total_gross"]
    if net is None and gross is not None and tax is not None:
        net = round(gross - tax, 2)
    if net is None and gross is not None and tax is None and not data["lines"]:
        net = gross
        tax = 0.0
        notes.append("total tax not read, taken as 0 for the position made from the total")
    rate = _rate_from_totals(net, tax)
    if data["lines"] and rate is not None and all(l["tax_rate"] is None for l in data["lines"]):
        for line in data["lines"]:
            line["tax_rate"] = rate
        notes.append("tax rate %g %% derived from the totals" % rate)
    if not data["lines"] and net is not None and net > 0:
        data["lines"].append({
            "description": "Gesamtbetrag laut Beleg", "item_number": None, "quantity": 1.0, "unit": None,
            "unit_price_net": float(net), "discount_percent": 0.0, "tax_rate": rate, "art": None})
        notes.append("no positions were read: one position with the total was made")
    return notes


def retry_reasons(data, problems=()):
    """Why a second reading is worth it: a list of short English texts for the prompt (empty = fine)."""
    out = []
    if not data["lines"]:
        out.append("no positions were returned")
    if not (data["supplier"] or {}).get("name"):
        out.append("the supplier name is missing")
    if not data["invoice_number"]:
        out.append("the invoice number is missing")
    if not data["invoice_date"]:
        out.append("the invoice date is missing (look for 'Date', 'Invoice date', 'Datum', 'Rechnungsdatum'; "
                   "English dates like 'Oct 13, 2026' or '13 October 2026' count)")
    for code, detail in check_totals(data):
        if code == "net":
            out.append("the positions add up to a different net amount than the total (%s)" % detail)
        elif code == "gross":
            out.append("positions plus tax do not give the gross total (%s)" % detail)
        elif code == "sum":
            out.append("net plus tax is not the gross total (%s)" % detail)
    return out


def quality(data):
    """Lower is better: the number of open reasons (used to choose between two readings)."""
    return len(retry_reasons(data))
