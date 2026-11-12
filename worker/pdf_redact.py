"""Redact a PDF in place with PyMuPDF (with OCR for scanned pages).

The real text under each detected value is deleted (not just covered), a
placeholder such as <PERSON_1> is drawn in its place, and everything else
(layout, fonts, images, page count) is kept. Hidden places where names often
live are cleared too: metadata, comments and other annotations, form fields,
links, bookmarks and attachments. The result is re-read and rejected if any
detected value can still be found in it.
"""

import hashlib
import re

import pymupdf

FILL = (0.92, 0.92, 0.92)
TEXT_COLOR = (0.25, 0.25, 0.25)


# ---------- OCR for scanned pages ----------

OCR_DPI = 200
_OCR_ENGINE = None
_OCR_CACHE = {}  # (sha256 of the file, page index) -> [(text, quad-in-PDF-points)]; memory only


def ocr_available():
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def _engine():
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _OCR_ENGINE = RapidOCR()
    return _OCR_ENGINE


def is_scanned(page):
    """No usable text layer, but there is something to read on the page."""
    return len(page.get_text("text").strip()) < 15 and bool(page.get_images())


def _ocr_page(page, key=None):
    if key is not None and key in _OCR_CACHE:
        return _OCR_CACHE[key]
    pix = page.get_pixmap(dpi=OCR_DPI)
    result, _ = _engine()(pix.tobytes("png"))
    scale = 72 / OCR_DPI
    lines = []
    for quad, text, score in result or []:
        if not text.strip() or float(score) < 0.3:
            continue
        xs = [p[0] * scale for p in quad]
        ys = [p[1] * scale for p in quad]
        lines.append((_respace(clean(text)), pymupdf.Rect(min(xs), min(ys), max(xs), max(ys))))
    lines.sort(key=lambda l: (round(l[1].y0 / 4), l[1].x0))
    if key is not None:
        _OCR_CACHE[key] = lines
    return lines


_GLUED = re.compile(r"(?<=[a-z])(?=[A-Z][a-z])")


def _respace(line):
    """OCR often drops spaces: "TransfertoKarimAoun" -> "Transferto Karim Aoun"."""
    return _GLUED.sub(" ", line)


def _ocr_chars(lines):
    """Same shape as page_chars(): text plus an approximate box per character."""
    text, boxes = [], []
    for n, (line, r) in enumerate(lines, 1):
        # Place each character by real glyph widths (Helvetica as a stand-in
        # for the scanned font), not by an even split of the line.
        total = pymupdf.get_text_length(line, fontname="helv", fontsize=1) or 1
        scale = r.width / total
        x = r.x0
        for ch in line:
            w = pymupdf.get_text_length(ch, fontname="helv", fontsize=1) * scale
            text.append(ch)
            boxes.append((pymupdf.Rect(x, r.y0, x + w, r.y1), 10_000 + n) if ch.strip() else None)
            x += w
        text.append("\n")
        boxes.append(None)
    return "".join(text), boxes


def clear_ocr_cache():
    _OCR_CACHE.clear()


def _need_ocr_error():
    return ValueError(
        "This PDF is scanned (its pages are pictures of text). Install the OCR add-on to redact it: "
        "run  bash setup.sh --ocr  in the redactor folder, then restart Claude Desktop."
    )


def page_texts(data):
    """Text of each page: the text layer, or OCR for scanned pages."""
    digest = hashlib.sha256(data).hexdigest()
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass:
            raise ValueError("This PDF is password protected. Remove the password and try again.")
        out = []
        for i, page in enumerate(doc):
            if is_scanned(page):
                if not ocr_available():
                    raise _need_ocr_error()
                out.append("\n".join(t for t, _ in _ocr_page(page, (digest, i))))
            else:
                out.append(clean(page.get_text("text")))
        return out


def _fontsize(rect, tag):
    by_height = rect.height * 0.75
    by_width = rect.width / (0.52 * max(len(tag), 1))
    return max(min(by_height, by_width, 11), 0)


def _norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()


_ALNUM_BEFORE = r"(?<![^\W_])"
_ALNUM_AFTER = r"(?![^\W_])"


def clean(text):
    """Ordinary spaces, no invisible characters (same rules as detection)."""
    from finance import normalize_text

    return normalize_text(text)


def value_pattern(value, bounded=True):
    """Regex for a value that tolerates any spacing or line breaks inside it.

    bounded: must not sit inside a longer word or number ("Rami" in "Ceramic").
    """
    tokens = value.split()
    if not tokens:
        return None
    # Inside a word: at most one same-line gap between letters (wide letter
    # spacing), none between digits. Between words: any spacing, including a line break. Without
    # this, "5" in one table cell and "0.25" in the next could read as "50.25".
    def word(tok):
        out = re.escape(tok[0])
        for a, b in zip(tok, tok[1:]):
            # Only letters may be letter-spaced; digits must touch.
            out += (r"[ \t]?" if a.isalpha() and b.isalpha() else "") + re.escape(b)
        return out

    body = r"\s*".join(word(tok) for tok in tokens)
    return re.compile((_ALNUM_BEFORE + body + _ALNUM_AFTER) if bounded else body, re.IGNORECASE)


def still_present(value, text):
    """Is value still readable in text? Used to verify the result."""
    pat = value_pattern(value)
    if pat and pat.search(text):
        return True
    compact = re.sub(r"[\W_]", "", value)
    if len(compact) >= 8:  # long values: also catch them glued to other text
        loose = value_pattern(value, bounded=False)
        return bool(loose and loose.search(text))
    return False


def page_chars(page):
    """The page as one string plus, for each character, its box and line.

    Built from single glyphs so a value is found whatever the PDF's word
    spacing: glued to a label, letter-spaced, or broken across lines.
    """
    text, boxes = [], []
    raw = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_TEXT)
    line_no = 0
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            line_no += 1
            prev = None
            for span in line.get("spans", []):
                size = span.get("size", 10)
                for ch in span.get("chars", []):
                    c = clean(ch["c"])
                    r = pymupdf.Rect(ch["bbox"])
                    if prev is not None and c.strip() and r.x0 - prev.x1 > size * 0.25:
                        text.append(" ")
                        boxes.append(None)  # a visual gap with no space glyph
                    if not c:
                        continue
                    for piece in c:
                        text.append(piece)
                        boxes.append((r, line_no) if piece.strip() else None)
                    if c.strip():
                        prev = r
            text.append("\n")
            boxes.append(None)
    return "".join(text), boxes


def find_rects(chars, value, bounded=True):
    """One rectangle per line for every match of value on the page."""
    text, boxes = chars
    pat = value_pattern(value, bounded)
    rects = []
    if not pat:
        return rects
    for m in pat.finditer(text):
        lines = {}
        for i in range(m.start(), m.end()):
            if boxes[i] is None:
                continue
            r, line = boxes[i]
            lines[line] = lines[line] | r if line in lines else pymupdf.Rect(r)
        rects.extend(lines.values())
    return rects


def redact_pdf(data, replacements, remove_images=False, flatten=False):
    """Return (pdf_bytes, report).

    replacements: list of (original_value, placeholder), longest values first.
    flatten: turn every page into a picture afterwards (no text layer at all).
    """
    digest = hashlib.sha256(data).hexdigest()
    doc = pymupdf.open(stream=data, filetype="pdf")
    hits = 0
    images_removed = 0
    located = set()
    scanned = [is_scanned(page) for page in doc]
    if any(scanned) and not ocr_available():
        raise _need_ocr_error()
    all_chars = [_ocr_chars(_ocr_page(page, (digest, i))) if scanned[i] else page_chars(page)
                 for i, page in enumerate(doc)]
    # A value detected in this document but not found as a whole word anywhere
    # (Arabic prefixes, text glued together) is matched without word edges.
    loose = {v for v, _ in replacements if not any(find_rects(c, v) for c in all_chars)}
    for page_index, (page, chars) in enumerate(zip(doc, all_chars)):
        # Comments, highlights, sticky notes and form fields can hold names.
        for annot in list(page.annots() or []):
            page.delete_annot(annot)
        for widget in list(page.widgets() or []):
            page.delete_widget(widget)
        for link in page.get_links():
            page.delete_link(link)

        covered = []
        for value, tag in replacements:
            for rect in find_rects(chars, value, bounded=value not in loose):
                located.add(value)
                # Text lines overlap by a point or two: a full-height box would
                # also delete letters on the lines above and below.
                h = rect.height
                if scanned[page_index]:
                    # OCR positions are estimates: pad by about one character.
                    pad = max(h * 0.6, 3)
                    rect = pymupdf.Rect(rect.x0 - pad, rect.y0 - 1.5, rect.x1 + pad, rect.y1 + 1.5)
                else:
                    rect = pymupdf.Rect(rect.x0, rect.y0 + h * 0.18, rect.x1, rect.y1 - h * 0.18)
                if any(rect in c for c in covered):
                    continue  # already inside a longer value's box ("Rami" in "Rami Haddad")
                covered.append(rect)
                fs = _fontsize(rect, tag)
                page.add_redact_annot(
                    rect,
                    text=tag if fs >= 3.5 else "",
                    fontname="helv",
                    fontsize=fs if fs >= 3.5 else 11,
                    align=pymupdf.TEXT_ALIGN_CENTER,
                    fill=FILL,
                    text_color=TEXT_COLOR,
                    cross_out=False,
                )
                hits += 1

        if remove_images:
            for info in page.get_image_info():
                r = pymupdf.Rect(info["bbox"]) & page.rect
                if r.is_empty:
                    continue
                page.add_redact_annot(r, text="image removed", fontname="helv", fontsize=8,
                                      fill=(0.85, 0.85, 0.85), text_color=TEXT_COLOR, cross_out=False)
                images_removed += 1

        # On scanned pages the "text" is pixels: blank the pixels under each box.
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_PIXELS if (remove_images or scanned[page_index]) else pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )

    # Hidden places names live.
    doc.set_metadata({})
    doc.del_xml_metadata()
    doc.set_toc([])
    for name in list(doc.embfile_names()):
        doc.embfile_del(name)

    out = doc.tobytes(garbage=4, deflate=True, clean=True)
    doc.close()
    if flatten:
        out = flatten_pdf(out)

    # Verify: nothing we meant to hide may still be readable in the result,
    # and everything detected must have been found on a page. Scanned and
    # flattened pages are read back with OCR, the way a person would read them.
    with pymupdf.open(stream=out, filetype="pdf") as check:
        texts = []
        for i, p in enumerate(check):
            if (flatten or scanned[i]) and ocr_available():
                texts.append("\n".join(t for t, _ in _ocr_page(p)))
            else:
                texts.append(p.get_text("text"))
        remaining_text = clean("\n".join(texts))
        meta = {k: v for k, v in (check.metadata or {}).items() if v and k not in ("format", "encryption")}
        pages = check.page_count
    leaks = sorted({v for v, _ in replacements if still_present(v, remaining_text)})
    unlocated = sorted({v for v, _ in replacements} - located)
    return out, {
        "pages": pages,
        "boxes": hits,
        "images_removed": images_removed,
        "leaks": leaks,
        "unlocated": unlocated,
        "metadata_left": sorted(meta),
        "flattened": bool(flatten),
        "ocr_pages": sum(scanned),
    }


def flatten_pdf(pdf_bytes, dpi=150):
    """Rebuild the PDF from pictures of its pages: no text, fonts or objects survive."""
    src = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    dst = pymupdf.open()
    for page in src:
        pix = page.get_pixmap(dpi=dpi)
        new = dst.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, stream=pix.tobytes("png"))
    out = dst.tobytes(garbage=4, deflate=True, clean=True)
    src.close()
    dst.close()
    return out


def render_page(pdf_bytes, page_number, dpi=110):
    """PNG of one page of an already redacted PDF (1-based page number)."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        if not 1 <= page_number <= doc.page_count:
            raise ValueError(f"Page {page_number} does not exist (the document has {doc.page_count}).")
        page = doc[page_number - 1]
        return page.get_pixmap(dpi=dpi).tobytes("png"), page.get_text("text")
