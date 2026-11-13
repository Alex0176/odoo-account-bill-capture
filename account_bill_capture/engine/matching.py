# Copyright 2026 Biricon IT Services GmbH
# License AGPL-3.0 or later (http://www.gnu.org/licenses/agpl).
"""Fuzzy matching of names, texts, tax numbers and bank accounts. Odoo independent."""
import re
import unicodedata
from difflib import SequenceMatcher

LEGAL_WORDS = {"gmbh", "ag", "kg", "og", "eu", "e", "u", "ltd", "llc", "inc", "co", "mbh", "gesmbh", "ges",
               "m", "b", "h", "und", "and", "the", "sa", "srl", "bv", "nv", "oy", "ab", "spa", "se", "cokg"}


def _fold(text):
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).casefold()


def normalize_vat(value):
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def normalize_iban(value):
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def tokens(text, drop_legal=False):
    words = re.findall(r"[a-z0-9]+", _fold(text))
    return [w for w in words if not (drop_legal and w in LEGAL_WORDS)]


def name_key(text):
    return " ".join(tokens(text, drop_legal=True))


def similarity(a, b, drop_legal=True):
    """0..1; the better of the character similarity and the overlap of the words."""
    ta, tb = tokens(a, drop_legal), tokens(b, drop_legal)
    if not ta or not tb:
        return 0.0
    ratio = SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    sa, sb = set(ta), set(tb)
    overlap = 2.0 * len(sa & sb) / (len(sa) + len(sb))
    return max(ratio, overlap)


def best_match(items, text, threshold, margin=0.05, drop_legal=True):
    """``(id, score, ambiguous)`` of the best ``(id, text)`` in ``items`` reaching ``threshold``.
    ``ambiguous`` when another one with a different id is within ``margin``. ``(None, best, False)`` if none."""
    scored = sorted(((similarity(text, t, drop_legal), i) for i, t in items), key=lambda p: -p[0])
    scored = [p for p in scored if p[0] > 0]
    if not scored or scored[0][0] < threshold:
        return None, scored[0][0] if scored else 0.0, False
    top = scored[0]
    others = {i for s, i in scored[1:] if top[0] - s <= margin and s >= threshold and i != top[1]}
    return top[1], top[0], bool(others)


def money(value, precision=2):
    return round(value + 0.0, precision)
