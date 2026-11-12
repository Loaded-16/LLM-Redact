"""IBANs, account numbers and long numbers, told apart from money amounts.

Bank documents are full of numbers. The rules here are deliberately explicit:

  * IBAN: country code + check digits + body, in any grouping, upper or lower
    case, glued to its label ("IBAN:LB62...") or wrapped over two lines.
    Validated with the IBAN checksum; an invalid one still counts when the
    word IBAN stands right before it.
  * Account number: a number that directly follows an account label
    ("Account No:", "A/C", "Beneficiary", "حساب"...). Nearby words alone are
    NOT enough, otherwise every amount in a statement would be hidden.
  * Long number: 11+ digits, or 9+ digits with a leading zero, or a 9-10 digit
    number inside a sentence. A bare 9-10 digit number alone in a table cell
    is treated as an amount unless the cell before it is an ID-style label.
"""

import re

from presidio_analyzer import EntityRecognizer, RecognizerResult

# Unicode spaces that break ordinary patterns (non-breaking, thin, etc.).
_SPACES = dict.fromkeys(map(ord, "               　"), " ")
# Invisible characters PDFs and Word sprinkle inside numbers and names.
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­‎‏‪‫‬‭‮"), None)


def normalize_text(text):
    """Make odd spaces ordinary and drop invisible characters."""
    return text.translate(_SPACES).translate(_INVISIBLE)


IBAN_LENGTHS = {
    "LB": 28, "SA": 24, "AE": 23, "QA": 29, "KW": 30, "BH": 22, "JO": 30, "EG": 29, "OM": 23, "IQ": 23,
    "TR": 26, "CY": 28, "GB": 22, "FR": 27, "DE": 22, "CH": 21, "ES": 24, "IT": 27, "NL": 18, "BE": 16,
    "AT": 20, "IE": 22, "LU": 20, "PT": 25, "GR": 27, "SE": 24, "DK": 18, "NO": 15, "FI": 18, "PL": 28,
    "MT": 31, "MC": 27, "RO": 24, "CZ": 24, "HU": 28, "BG": 22, "HR": 21, "TN": 24, "MA": 28, "PK": 24,
}

_IBAN_START = re.compile(r"(?<![^\W_])([A-Za-z]{2})(\d{2})")
_IBAN_LABEL = re.compile(r"(?i)iban\W{0,4}$")


def _mod97_ok(compact):
    s = compact[4:] + compact[:4]
    try:
        return int("".join(str(int(c, 36)) for c in s)) % 97 == 1
    except ValueError:
        return False


def _collect(text, start, limit=34):
    """Alphanumerics from start, skipping single separators. -> [(char, index)]"""
    out = []
    i = start
    gap = 0
    newlines = 0
    while i < len(text) and len(out) < limit:
        c = text[i]
        if c.isascii() and c.isalnum():
            out.append((c.upper(), i))
            gap = 0
        elif c in " \t-" and gap < 3:
            gap += 1
        elif c == "\n" and newlines < 2 and gap < 3:
            newlines += 1
            gap += 1
        else:
            break
        i += 1
    return out


class FlexIbanRecognizer(EntityRecognizer):
    def __init__(self):
        super().__init__(supported_entities=["IBAN_CODE"], name="FlexibleIbanRecognizer", supported_language="en")

    def load(self):
        pass

    def analyze(self, text, entities, nlp_artifacts=None):
        if "IBAN_CODE" not in entities:
            return []
        results = []
        for m in _IBAN_START.finditer(text):
            chars = _collect(text, m.start())
            compact = "".join(c for c, _ in chars)
            cc = compact[:2]
            want = IBAN_LENGTHS.get(cc)
            end = None
            score = 0.95
            if not want:
                # Unknown country: stop at the first group with no digit, so
                # following words are never swallowed.
                keep = 0
                for tok in re.finditer(r"[A-Za-z0-9]+", text[m.start():chars[-1][1] + 1]):
                    if keep and not any(ch.isdigit() for ch in tok.group(0)):
                        break
                    keep += len(tok.group(0))
                chars = chars[:keep]
                compact = compact[:keep]
            lengths = [want] if want else range(len(compact), 14, -1)
            for n in lengths:
                if n and n <= len(compact) and _mod97_ok(compact[:n]):
                    # Must end at a word edge, not in the middle of a longer token.
                    last = chars[n - 1][1]
                    if last + 1 >= len(text) or not (text[last + 1].isascii() and text[last + 1].isalnum()):
                        end = last + 1
                        break
            if end is None and _IBAN_LABEL.search(text[max(0, m.start() - 8):m.start()]):
                # Says IBAN but fails the checksum (typo, sample, cut off): hide
                # it anyway, up to the country's length or while groups hold digits.
                n = min(want or 34, len(compact))
                if not want:
                    n = 0
                    for tok in re.finditer(r"[A-Za-z0-9]+", text[m.start():chars[-1][1] + 1]):
                        if not any(ch.isdigit() for ch in tok.group(0)):
                            break
                        n += len(tok.group(0))
                if n >= 12:
                    end = chars[n - 1][1] + 1
                    score = 0.9
            if end:
                results.append(RecognizerResult("IBAN_CODE", m.start(), end, score))
        return results


_NUM = r"(?:[A-Za-z]{2,4}[ -]?)?\d(?:[ -]?\d){5,29}"
_LABEL = (
    r"(?:account|acct|acc|a/c|iban|beneficiary|customer|client|cif|rib|swift|bic|حساب|الحساب)"
    r"(?:[ \t]*(?:no|nb|nr|num|number|id|code|#|رقم))?\.?"
)
_ACCOUNT_INLINE = re.compile(rf"(?i)(?<![^\W_]){_LABEL}[ \t]*[:#\-]?[ \t]*(?P<num>{_NUM})(?![^\W_])(?![.,]\d)")
_ACCOUNT_NEXT_LINE = re.compile(rf"(?i)(?<![^\W_]){_LABEL}[ \t]*:?[ \t]*\n[ \t]*(?P<num>{_NUM})(?![^\W_])(?![.,]\d)")
_TRANSFER_TO = re.compile(
    r"(?i)\b(?:transfer|wire|pay|payment|send|remit|deposit)\w*[ \t]+(?:it[ \t]+)?to[ \t]+(?:account[ \t]+)?(?P<num>\d(?:[ -]?\d){7,29})(?![^\W_])(?![.,]\d)"
)


def _digits(s):
    return re.sub(r"\D", "", s)


def _amount_shaped(num):
    """Could this bare number be money? (no letters, dashes or leading zero, under 10 digits)"""
    d = _digits(num)
    return num.strip().replace(" ", "").isdigit() and not d.startswith("0") and len(d) < 10


class LabelledAccountRecognizer(EntityRecognizer):
    """Numbers that directly follow an account label."""

    def __init__(self):
        super().__init__(supported_entities=["BANK_ACCOUNT"], name="LabelledAccountRecognizer", supported_language="en")

    def load(self):
        pass

    def analyze(self, text, entities, nlp_artifacts=None):
        if "BANK_ACCOUNT" not in entities:
            return []
        out = []
        for m in _ACCOUNT_INLINE.finditer(text):
            out.append(RecognizerResult("BANK_ACCOUNT", m.start("num"), m.end("num"), 0.85))
        for m in _ACCOUNT_NEXT_LINE.finditer(text):
            # Label in one table cell, value in the next: skip plain amounts
            # ("Savings account" / "1250000").
            if not _amount_shaped(m.group("num")):
                out.append(RecognizerResult("BANK_ACCOUNT", m.start("num"), m.end("num"), 0.85))
        for m in _TRANSFER_TO.finditer(text):
            out.append(RecognizerResult("BANK_ACCOUNT", m.start("num"), m.end("num"), 0.6))
        return out


_LONG = re.compile(r"(?<![^\W_])(?<!\d[.,])\d{9,}(?![^\W_])(?![.,]\d)")
_ID_LABEL_LINE = re.compile(r"(?i)(?:\bid|\bno|number|num|#|ref|reference|code|passport|account|iban|card|رقم)\W{0,3}$")


def _line_bounds(text, start, end):
    ls = text.rfind("\n", 0, start) + 1
    le = text.find("\n", end)
    return ls, len(text) if le < 0 else le


def alone_on_line(text, start, end):
    ls, le = _line_bounds(text, start, end)
    return not text[ls:start].strip() and not text[end:le].strip()


def previous_line(text, start):
    ls = text.rfind("\n", 0, start) + 1
    prev = text[:max(ls - 1, 0)].rstrip("\n \t")
    return prev[prev.rfind("\n") + 1:]


class LongDigitsRecognizer(EntityRecognizer):
    """Safety net for IDs and unlabelled accounts, without eating amounts."""

    def __init__(self):
        super().__init__(supported_entities=["LONG_NUMBER"], name="LongNumberRecognizer", supported_language="en")

    def load(self):
        pass

    def analyze(self, text, entities, nlp_artifacts=None):
        if "LONG_NUMBER" not in entities:
            return []
        out = []
        for m in _LONG.finditer(text):
            num = m.group(0)
            if len(num) < 11 and not num.startswith("0") and alone_on_line(text, m.start(), m.end()):
                # A bare 9-10 digit number in a table cell is usually a balance.
                if not _ID_LABEL_LINE.search(previous_line(text, m.start())):
                    continue
            out.append(RecognizerResult("LONG_NUMBER", m.start(), m.end(), 0.5))
        return out


_PHONE_LABEL_LINE = re.compile(r"(?i)(?:phone|mobile|mob|tel|whatsapp|cell|contact|fax|هاتف|موبايل|واتساب|تلفون)\W{0,3}$")
_GROUPED = re.compile(r"\d{1,3}(?:[ ,]\d{3})+")


def phone_is_amount_cell(text, result):
    """A bare '70 123 456' alone in a table cell with no phone label is an amount."""
    raw = text[result.start:result.end].strip()
    if raw.startswith(("+", "0")) or not _GROUPED.fullmatch(raw):
        return False
    if not alone_on_line(text, result.start, result.end):
        return False
    return not _PHONE_LABEL_LINE.search(previous_line(text, result.start))


_DECIMAL_AMOUNT = re.compile(r"[-+(]?\d{1,3}(?:[, ]?\d{3})*[.,]\d{1,2}\)?")


def is_plain_amount(text, result):
    """'12.50', '1,250.00', '(300.00)': money, never a phone number or an ID.

    Also true for anything "phone-like" with fewer than 7 digits.
    """
    raw = text[result.start:result.end].strip()
    if _DECIMAL_AMOUNT.fullmatch(raw):
        return True
    return result.entity_type in ("PHONE_NUMBER", "LB_PHONE") and len(re.sub(r"\D", "", raw)) < 7
