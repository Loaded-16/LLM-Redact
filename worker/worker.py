"""Presidio redaction worker.

Runs locally as a child process of the MCP server. Talks over stdin/stdout
using one JSON object per line. Nothing is written to disk and no network
port is opened: the codebook (real value <-> placeholder) lives in memory
only and disappears when the process stops.

Requests:
  {"id": 1, "op": "ping"}
  {"id": 2, "op": "redact", "text": "...", "entities": [...], "custom_terms": [...]}
  {"id": 3, "op": "redact", "file_name": "brief.docx", "file_b64": "...", ...}
  {"id": 4, "op": "restore", "text": "..."}
  {"id": 5, "op": "stats"}
  {"id": 6, "op": "reset"}
"""

import base64
import io
import json
import os
import re
import sys
import traceback

# Keep stdout clean for the protocol: anything a library prints goes to stderr.
_PROTOCOL_OUT = sys.stdout
sys.stdout = sys.stderr

# Stay fully offline: the email recognizer uses tldextract, which by default
# downloads the public suffix list from the internet. Use the bundled copy.
import tldextract  # noqa: E402

tldextract.extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer  # noqa: E402
from presidio_analyzer.predefined_recognizers import PhoneRecognizer  # noqa: E402
from presidio_anonymizer import AnonymizerEngine, OperatorConfig  # noqa: E402
from presidio_anonymizer.operators import Operator, OperatorType  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from names import NameListRecognizer, clean_person_spans, person_tag_for, propagate_names  # noqa: E402
from finance import (  # noqa: E402
    LabelledAccountRecognizer, FlexIbanRecognizer, LongDigitsRecognizer, is_plain_amount, normalize_text, phone_is_amount_cell,
)
import secrets  # noqa: E402

SCORE_THRESHOLD = float(os.environ.get("REDACTOR_SCORE_THRESHOLD", "0.4"))
MAX_TEXT_CHARS = 250_000  # about 100 pages; keeps a redaction well under a minute

DEFAULT_ENTITIES = [
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "LB_PHONE",
    "CREDIT_CARD",
    "IBAN_CODE",
    "IP_ADDRESS",
    "BANK_ACCOUNT",
    "LONG_NUMBER",
]
OPTIONAL_ENTITIES = ["LOCATION", "DATE_TIME", "NRP", "URL"]
ALL_ENTITIES = DEFAULT_ENTITIES + OPTIONAL_ENTITIES + ["CUSTOM"]

ACCOUNT_CONTEXT = [
    "account", "acct", "acc", "bank", "iban", "swift", "bic", "rib", "beneficiary",
    "transfer", "wire", "deposit", "branch", "حساب", "بنك", "مصرف", "تحويل",
]

PHONE_CONTEXT = [
    "phone", "mobile", "mob", "tel", "call", "whatsapp", "number", "cell",
    "contact", "reach", "text", "رقم", "هاتف", "موبايل", "واتساب", "تلفون",
]


class InstanceCounterAnonymizer(Operator):
    """Replace each value with <TYPE_n>, reusing the tag for repeated values.

    Based on Microsoft's pseudonymization sample.
    """

    def operate(self, text, params=None):
        entity_type = params["entity_type"]
        mapping = params["entity_mapping"].setdefault(entity_type, {})
        if text not in mapping:
            mapping[text] = f"<{entity_type}_{len(mapping) + 1}>"
        return mapping[text]

    def validate(self, params=None):
        if "entity_mapping" not in params:
            raise ValueError("entity_mapping is required")

    def operator_name(self):
        return "entity_counter"

    def operator_type(self):
        return OperatorType.Anonymize


def build_analyzer():
    analyzer = AnalyzerEngine()

    # Wider phone coverage for the region (adds the Gulf). Lebanon is left to
    # LebanesePhoneRecognizer below: as a generic region it turns LBP amounts
    # like "70 000 000" into phone numbers.
    analyzer.registry.remove_recognizer("PhoneRecognizer")
    analyzer.registry.add_recognizer(
        PhoneRecognizer(
            context=PHONE_CONTEXT,
            supported_regions=(
                "SA", "AE", "QA", "KW", "BH", "OM", "JO", "EG",
                "US", "GB", "FR", "DE", "CA",
            ),
        )
    )

    # Lebanese numbers written the local way.
    # Strong: has +961 / 00961 or a leading 0 (03 123 456, 01 234 567).
    # Weak: bare 8-digit mobile (70 123 456) only counts with context words,
    # so amounts like "70 000 000 LBP" are not masked.
    sep = r"[\s.-]?"
    analyzer.registry.add_recognizer(
        PatternRecognizer(
            supported_entity="LB_PHONE",
            name="LebanesePhoneRecognizer",
            context=PHONE_CONTEXT,
            patterns=[
                Pattern(
                    "lb_intl",
                    rf"(?<![\d+])(?:\+|00)961{sep}(?:3|7[016-9]|81|[1-9]){sep}\d{{3}}{sep}\d{{3}}(?!\d)",
                    0.85,
                ),
                Pattern(
                    "lb_local_zero",
                    rf"(?<![\d+])0(?:3|[1-9]){sep}\d{{3}}{sep}\d{{3}}(?!\d)",
                    0.6,
                ),
                Pattern(
                    "lb_intl_no_plus",
                    rf"(?<![\d+])961{sep}(?:3|7[016-9]|81|[1-9]){sep}\d{{3}}{sep}\d{{3}}(?!\d)",
                    0.7,
                ),
                Pattern(
                    "lb_mobile_bare",
                    rf"(?<![\d+])(?:7[016-9]|81){sep}\d{{3}}{sep}\d{{3}}(?!\d)",
                    0.2,
                ),
            ],
        )
    )
    # Gulf and Jordan numbers written with the country code but no + or 00.
    analyzer.registry.add_recognizer(
        PatternRecognizer(
            supported_entity="PHONE_NUMBER",
            name="GulfPhoneNoPlusRecognizer",
            context=PHONE_CONTEXT,
            patterns=[
                Pattern(
                    "gulf_no_plus",
                    rf"(?<![\d+])(?:966|971|974|965|973|968|962){sep}\d{{1,2}}{sep}\d{{3}}{sep}\d{{3,4}}(?!\d)",
                    0.6,
                )
            ],
        )
    )

    # IBANs, labelled account numbers and long numbers (see finance.py).
    analyzer.registry.add_recognizer(FlexIbanRecognizer())
    analyzer.registry.add_recognizer(LabelledAccountRecognizer())
    analyzer.registry.add_recognizer(LongDigitsRecognizer())

    analyzer.registry.add_recognizer(NameListRecognizer())

    # Optional models (off unless switched on by setup.sh flags).
    from models import add_arabic, add_gliner
    add_gliner(analyzer)
    add_arabic(analyzer)
    return analyzer


ANALYZER = build_analyzer()
ANONYMIZER = AnonymizerEngine()
ANONYMIZER.add_anonymizer(InstanceCounterAnonymizer)

# The codebook: {entity_type: {real_value: placeholder}}. Memory only.
CODEBOOK = {}

# Redacted documents, by random id. Kept in a private cache folder so they
# survive an engine restart and are visible to every copy of the server.
# Only REDACTED output is ever written: never the original file, never the
# codebook. Copies are deleted after 24 hours, beyond the newest 8, and on
# "Forget all".
MAX_DOCS = 8
DOC_TTL_SECONDS = 24 * 3600
_DOC_ID = re.compile(r"doc_[0-9a-f]{12}")


class DocStore:
    def __init__(self):
        base = os.environ.get("REDACTOR_STORE")
        if not base:
            home = os.path.expanduser("~")
            base = (os.path.join(home, "Library", "Caches", "presidio-redactor") if sys.platform == "darwin"
                    else os.path.join(home, ".cache", "presidio-redactor"))
        self.dir = base
        os.makedirs(self.dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        self.sweep()

    def _paths(self, doc_id):
        if not _DOC_ID.fullmatch(doc_id or ""):
            return None
        return os.path.join(self.dir, doc_id + ".pdf"), os.path.join(self.dir, doc_id + ".json")

    def _write(self, path, data):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)

    def put(self, doc_id, doc):
        pdf_path, meta_path = self._paths(doc_id)
        self._write(pdf_path, doc["pdf"])
        meta = {k: v for k, v in doc.items() if k != "pdf"}
        self._write(meta_path, json.dumps(meta, ensure_ascii=False).encode("utf-8"))
        self.sweep()

    def get(self, doc_id):
        paths = self._paths(doc_id)
        if not paths or not all(os.path.exists(p) for p in paths):
            return None
        with open(paths[1], encoding="utf-8") as f:
            doc = json.load(f)
        with open(paths[0], "rb") as f:
            doc["pdf"] = f.read()
        return doc

    def ids(self):
        out = []
        for name in os.listdir(self.dir):
            if name.endswith(".json") and _DOC_ID.fullmatch(name[:-5]):
                out.append((os.path.getmtime(os.path.join(self.dir, name)), name[:-5]))
        return [i for _, i in sorted(out)]

    def delete(self, doc_id):
        for p in self._paths(doc_id) or ():
            try:
                os.remove(p)
            except OSError:
                pass

    def sweep(self):
        import time
        ids = self.ids()
        now = time.time()
        for n, doc_id in enumerate(ids):
            too_old = now - os.path.getmtime(self._paths(doc_id)[1]) > DOC_TTL_SECONDS
            if too_old or n < len(ids) - MAX_DOCS:
                self.delete(doc_id)

    def clear(self):
        for doc_id in self.ids():
            self.delete(doc_id)


DOCS = DocStore()

# Values the user unticked in the review step: left visible for the rest of
# the session (memory only, cleared on "Forget all").
KEEP = set()

# Last placeholder number used per type (memory only).
COUNTERS = {}


def _key(value):
    return re.sub(r"\s+", " ", value).strip().lower()


CURRENCY_RE = re.compile(
    r"(?i)(?:\b(?:lbp|ll|l\.l\.?|usd|sar|aed|qar|kwd|bhd|omr|jod|egp|eur|gbp|dollars?|"
    r"million|mn|bn|k)\b|[$€£]|ل\.ل|ليرة|دولار|ريال|درهم|دينار)"
)


def looks_like_amount(text, result):
    """True when a number match is really a money amount ("70 000 000 LBP")."""
    # Labelled accounts and IBANs are never amounts ("Account 1234567890 USD").
    if result.entity_type not in ("PHONE_NUMBER", "LB_PHONE", "LONG_NUMBER"):
        return False
    if re.match(r"\s*(?:\+|00)", text[result.start:result.end]):
        return False  # international prefix: definitely a phone number
    after = text[result.end:result.end + 8]
    before = text[max(0, result.start - 8):result.start]
    return bool(CURRENCY_RE.search(after) or CURRENCY_RE.search(before))


ACCOUNT_LABEL_RE = re.compile(
    r"(?i)(?:account|acct|acc|a/c|iban|swift|beneficiary|حساب)\W{0,3}(?:no|nb|num|number|#|رقم)?\W{0,3}$"
)


def relabel_account(text, result):
    """A number right after "account no:" is an account, not a phone number."""
    from presidio_analyzer import RecognizerResult as RR

    if result.entity_type in ("PHONE_NUMBER", "LB_PHONE", "LONG_NUMBER"):
        before = text[max(0, result.start - 25):result.start]
        if ACCOUNT_LABEL_RE.search(before):
            return RR("BANK_ACCOUNT", result.start, result.end, max(result.score, 0.8))
    return result


# ---------- file extraction ----------

def extract_text(file_name, data):
    ext = os.path.splitext(file_name.lower())[1]
    if ext in (".txt", ".md", ".csv", ".tsv", ".json", ".html", ".htm", ".rtf", ".log"):
        return data.decode("utf-8", errors="replace")
    if ext == ".pdf":
        from pdf_redact import page_texts
        text = "\n\n".join(t.strip() for t in page_texts(data) if t.strip())
        if not text.strip():
            raise ValueError(
                "This PDF has no selectable text (it is probably a scan). "
                "Scanned PDFs need OCR, which this version does not do."
            )
        return text
    if ext == ".docx":
        import docx
        d = docx.Document(io.BytesIO(data))
        parts = [p.text for p in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
        return "\n".join(parts)
    if ext == ".pptx":
        from pptx import Presentation
        prs = Presentation(io.BytesIO(data))
        out = []
        for i, slide in enumerate(prs.slides, 1):
            out.append(f"--- Slide {i} ---")
            for shape in slide.shapes:
                if shape.has_text_frame:
                    out.append(shape.text_frame.text)
                if getattr(shape, "has_table", False) and shape.has_table:
                    for row in shape.table.rows:
                        out.append(" | ".join(c.text for c in row.cells))
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    out.append(f"[Notes] {notes}")
        return "\n".join(out)
    if ext == ".xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            out.append(f"--- Sheet: {ws.title} ---")
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v) for v in row]
                if any(cells):
                    out.append(" | ".join(cells))
        return "\n".join(out)
    raise ValueError(
        f"Unsupported file type '{ext or file_name}'. "
        "Supported: PDF, Word (.docx), PowerPoint (.pptx), Excel (.xlsx), text files."
    )


# ---------- operations ----------

def op_redact(req):
    data = None
    if req.get("file_b64"):
        name = req.get("file_name") or "file.txt"
        data = base64.b64decode(req["file_b64"])
        text = extract_text(name, data)
        source = name
    else:
        text = req.get("text") or ""
        name = None
        source = "pasted text"
    text = normalize_text(text)
    masked, counts, values, new_values, types = detect_and_mask(text, req)
    result = {
        "masked": masked,
        "detections": summarize_detections(values, types),
        "counts": counts,
        "total": sum(counts.values()),
        "new_values": new_values,
        "source": source,
        "chars": len(text),
    }
    if data is not None and name.lower().endswith(".pdf"):
        result["pdf"] = redact_pdf_document(name, data, values, bool(req.get("remove_images")), masked,
                                            flatten=bool(req.get("flatten")))
    return result


def redact_pdf_document(name, data, values, remove_images, masked, flatten=False):
    """Redact the PDF itself and keep only the redacted copy, in memory."""
    from pdf_redact import page_texts, redact_pdf, still_present

    replacements = sorted({(re.sub(r"\s+", " ", v).strip(), tag) for v, tag in values if v.strip()},
                          key=lambda x: len(x[0]), reverse=True)
    out, report = redact_pdf(data, replacements, remove_images, flatten=flatten)

    # The page text Claude receives alongside each page image.
    masked_pages = [mask_with_values(t, replacements) for t in page_texts(data)]
    text_leaks = sorted({v for v, _ in replacements if any(still_present(v, t) for t in masked_pages)})

    problems = sorted(set(report["leaks"]) | set(report["unlocated"]) | set(text_leaks))
    verified = not problems and not report["metadata_left"]
    doc_id = "doc_" + secrets.token_hex(6)
    stem = os.path.splitext(os.path.basename(name))[0]
    DOCS.put(doc_id, {"name": f"{stem} (redacted).pdf", "pdf": out, "pages": report["pages"],
                      "masked_pages": masked_pages, "verified": verified})
    return {
        "doc_id": doc_id,
        "name": f"{stem} (redacted).pdf",
        "pages": report["pages"],
        "boxes": report["boxes"],
        "images_removed": report["images_removed"],
        "flattened": report.get("flattened", False),
        "ocr_pages": report.get("ocr_pages", 0),
        "verified": verified,
        "leak_count": len(problems),
    }


def mask_with_values(text, replacements):
    """Replace known values (longest first) in one page's text, any spacing."""
    from pdf_redact import value_pattern

    for value, tag in replacements:
        pat = value_pattern(value)
        if pat and pat.search(text):
            text = pat.sub(tag, text)
        elif pat:
            loose = value_pattern(value, bounded=False)
            text = loose.sub(tag, text)
    return text


def detect_and_mask(text, req):
    """Find sensitive values, register placeholders, return the masked text.

    Returns (masked_text, counts, [(value, placeholder)], new_values).
    """
    if not text.strip():
        raise ValueError("Nothing to redact: the text is empty.")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(
            f"Text is too long ({len(text):,} characters). Limit is {MAX_TEXT_CHARS:,}."
        )

    entities = [e for e in (req.get("entities") or DEFAULT_ENTITIES) if e in ALL_ENTITIES]
    terms = [t.strip() for t in (req.get("custom_terms") or []) if t and t.strip()]

    # Review step: what the user unticked stays visible, what they added is hidden.
    for v in req.get("keep") or []:
        if v and v.strip():
            KEEP.add(_key(v))
            for bucket in CODEBOOK.values():
                for known in [k for k in bucket if _key(k) == _key(v)]:
                    del bucket[known]
    terms_keys = {_key(t) for t in terms}
    KEEP.difference_update(terms_keys)  # adding something again overrides an earlier "keep"

    ad_hoc = []
    if terms:
        ad_hoc.append(
            PatternRecognizer(
                supported_entity="CUSTOM",
                name="CustomTerms",
                patterns=[
                    Pattern(
                        "custom_terms",
                        r"(?i)(?<!\w)(?:" + "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) + r")(?!\w)",
                        1.0,
                    )
                ],
            )
        )
        if "CUSTOM" not in entities:
            entities.append("CUSTOM")

    results = ANALYZER.analyze(
        text=text,
        language="en",
        entities=entities,
        score_threshold=SCORE_THRESHOLD,
        ad_hoc_recognizers=ad_hoc or None,
    )

    # Label check first: a number right after "Account no" stays hidden even
    # with a currency next to it.
    results = [relabel_account(text, r) for r in results]
    results = [r for r in results if not looks_like_amount(text, r)]
    results = [r for r in results
               if not (r.entity_type in ("PHONE_NUMBER", "LB_PHONE") and phone_is_amount_cell(text, r))]
    results = [r for r in results
               if not (r.entity_type in ("PHONE_NUMBER", "LB_PHONE", "LONG_NUMBER", "PERSON", "DATE_TIME", "NRP", "LOCATION")
                       and is_plain_amount(text, r))]

    if "PERSON" in entities:
        # Hide every other mention of a found name, including names seen in
        # earlier messages and their first or last name on its own.
        results = clean_person_spans(text, results)
        results = propagate_names(text, results, CODEBOOK.get("PERSON", {}).keys())

    if KEEP:
        results = [r for r in results if _key(text[r.start:r.end]) not in KEEP]

    before = sum(len(v) for v in CODEBOOK.values())

    # Pass 1: let the anonymizer resolve overlaps, then register new values in
    # reading order so the first person in the text becomes <PERSON_1>.
    resolved = ANONYMIZER.anonymize(
        text=text,
        analyzer_results=results,
        operators={"DEFAULT": OperatorConfig("custom", {"lambda": lambda x: x})},
    )
    for item in sorted(resolved.items, key=lambda i: i.start):
        bucket = CODEBOOK.setdefault(item.entity_type, {})
        if item.text in bucket:
            continue
        if item.entity_type == "PERSON":
            shared = person_tag_for(item.text, bucket)
        else:
            # Same number or IBAN in another spelling ("LB62 0999" / "lb620999").
            key = re.sub(r"[\s-]", "", item.text).upper()
            shared = next((t for v, t in bucket.items() if re.sub(r"[\s-]", "", v).upper() == key), None)
        if shared:
            bucket[item.text] = shared  # "Rami" and "Rami Haddad" are one person
        else:
            # Numbers are never reused, even after a value is unticked, so an
            # old placeholder in the chat can't restore to a different value.
            COUNTERS[item.entity_type] = COUNTERS.get(item.entity_type, 0) + 1
            bucket[item.text] = f"<{item.entity_type}_{COUNTERS[item.entity_type]}>"

    # Pass 2: replace using the codebook.
    anonymized = ANONYMIZER.anonymize(
        text=text,
        analyzer_results=results,
        operators={"DEFAULT": OperatorConfig("entity_counter", {"entity_mapping": CODEBOOK})},
    )

    counts = {}
    for item in anonymized.items:
        counts[item.entity_type] = counts.get(item.entity_type, 0) + 1
    new_values = sum(len(v) for v in CODEBOOK.values()) - before
    ordered = sorted(resolved.items, key=lambda i: i.start)
    values = [(item.text, CODEBOOK[item.entity_type][item.text]) for item in ordered]
    types = [item.entity_type for item in ordered]
    return anonymized.text, counts, values, new_values, types


def summarize_detections(values, types):
    """One row per distinct value for the panel's review list, in reading order.

    Real values are included: this goes only to the panel (an app-only tool
    result), never into anything Claude reads.
    """
    rows = {}
    for (value, tag), etype in zip(values, types):
        k = _key(value)
        if k not in rows:
            rows[k] = {"value": re.sub(r"\s+", " ", value).strip(), "tag": tag, "type": etype, "count": 0}
        rows[k]["count"] += 1
    return list(rows.values())


TAG_RE = re.compile(r"<([A-Z_]+)_(\d+)>")


def op_restore(req):
    text = req.get("text") or ""
    # When several spellings share a tag ("Rami", "Rami Haddad"), restore the longest.
    reverse = {}
    for values in CODEBOOK.values():
        for value, tag in values.items():
            if len(value) > len(reverse.get(tag, "")):
                reverse[tag] = value
    unresolved = []

    def swap(m):
        tag = m.group(0)
        if tag in reverse:
            return reverse[tag]
        unresolved.append(tag)
        return tag

    restored = TAG_RE.sub(swap, text)
    return {"restored": restored, "unresolved": sorted(set(unresolved))}


def op_stats(_req):
    return {
        "values_held": sum(len(set(v.values())) for v in CODEBOOK.values()),
        "by_type": {k: len(v) for k, v in CODEBOOK.items()},
    }


def op_doc_page(req):
    from pdf_redact import render_page

    doc = DOCS.get(req.get("doc_id"))
    if not doc:
        raise ValueError("That redacted document is no longer available (copies are kept for 24 hours). Redact the file again.")
    if req.get("require_verified") and not doc["verified"]:
        raise ValueError("This document did not pass the redaction check, so it cannot be read. "
                         "Ask the user to send it as text from the panel instead.")
    n = int(req.get("page", 1))
    png, _ = render_page(doc["pdf"], n, int(req.get("dpi", 110)))
    # Page text comes from the masked original, so every placeholder is present
    # even where a box was too small to print it.
    text = doc["masked_pages"][n - 1] if n - 1 < len(doc["masked_pages"]) else ""
    return {"png_b64": base64.b64encode(png).decode(), "text": text, "pages": doc["pages"], "name": doc["name"]}


def op_doc_info(req):
    doc = DOCS.get(req.get("doc_id"))
    if not doc:
        raise ValueError("That redacted document is no longer available (copies are kept for 24 hours). Redact the file again.")
    if req.get("require_verified") and not doc["verified"]:
        raise ValueError("This document did not pass the redaction check, so it cannot be read. "
                         "Ask the user to send it as text from the panel instead.")
    return {"name": doc["name"], "pages": doc["pages"], "verified": doc["verified"]}


def features():
    from models import STATUS
    from pdf_redact import ocr_available

    return {"ocr": ocr_available(), "gliner": STATUS["gliner"], "arabic": STATUS["arabic"]}


def op_reset(_req):
    from pdf_redact import clear_ocr_cache

    CODEBOOK.clear()
    KEEP.clear()
    COUNTERS.clear()
    clear_ocr_cache()
    DOCS.clear()
    return {"values_held": 0}


OPS = {
    "ping": lambda r: {"ready": True, "default_entities": DEFAULT_ENTITIES, "optional_entities": OPTIONAL_ENTITIES,
                       "features": features()},
    "redact": op_redact,
    "restore": op_restore,
    "stats": op_stats,
    "reset": op_reset,
    "doc_page": op_doc_page,
    "doc_info": op_doc_info,
}


def send(obj):
    _PROTOCOL_OUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
    _PROTOCOL_OUT.flush()


def main():
    send({"event": "ready"})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req_id = None
        try:
            req = json.loads(line)
            req_id = req.get("id")
            op = OPS.get(req.get("op"))
            if not op:
                raise ValueError(f"Unknown op: {req.get('op')}")
            send({"id": req_id, "ok": True, "result": op(req)})
        except Exception as e:  # report, never crash the worker
            traceback.print_exc(file=sys.stderr)
            send({"id": req_id, "ok": False, "error": str(e)})


if __name__ == "__main__":
    main()
